#!/bin/bash
# =============================================================
#  Painel Master — Instalador v4
#  Repositório: https://github.com/jeanfraga33/painel-master
# =============================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

INSTALL_DIR="/opt/painel-master"
SERVICE_NAME="painel-master"
# Default port 2083; protect against empty-string override
PANEL_PORT="${PANEL_PORT:-2083}"
[ -z "$PANEL_PORT" ] && PANEL_PORT="2083"
REPO_URL="https://github.com/jeanfraga33/painel-master"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_BACKUP=""

log()    { echo -e "${GREEN}[✔]${NC} $1"; }
warn()   { echo -e "${YELLOW}[!]${NC} $1"; }
error()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info()   { echo -e "${BLUE}[i]${NC} $1"; }
banner() { echo -e "${CYAN}${BOLD}$1${NC}"; }

check_root() {
    [ "$EUID" -eq 0 ] || error "Execute como root: sudo bash install.sh"
}

detect_arch() {
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64|amd64)  ARCH_LABEL="x86_64" ;;
        aarch64|arm64) ARCH_LABEL="arm64"   ;;
        armv7l)        ARCH_LABEL="armv7"   ;;
        *)             ARCH_LABEL="$ARCH"   ;;
    esac
    info "Arquitetura: $ARCH_LABEL"
}

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID="$ID"; OS_VER="$VERSION_ID"; OS_CODENAME="${VERSION_CODENAME:-}"
    else
        OS_ID="unknown"; OS_VER="unknown"; OS_CODENAME=""
    fi
    info "Sistema: $OS_ID $OS_VER${OS_CODENAME:+ ($OS_CODENAME)}"
}

# ── Desinstala versão anterior ─────────────────────────────────
uninstall_existing() {
    local found=0
    systemctl is-active  --quiet "$SERVICE_NAME" 2>/dev/null && found=1
    systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null && found=1
    [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]     && found=1
    [ -d "$INSTALL_DIR" ]                                    && found=1
    [ "$found" -eq 0 ] && return

    banner "Instalacao anterior detectada. Removendo tudo..."

    systemctl stop    "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload 2>/dev/null

    pkill -9 -f "python.*app\.py" 2>/dev/null || true
    pkill -9 -f "painel-master"   2>/dev/null || true
    sleep 2

    if [ -f "$INSTALL_DIR/painel.db" ]; then
        DB_BACKUP="/tmp/painel_backup_$(date +%s).db"
        cp "$INSTALL_DIR/painel.db" "$DB_BACKUP"
        warn "Banco de dados salvo em: $DB_BACKUP"
    fi

    rm -rf "$INSTALL_DIR"
    log "Instalacao anterior removida"
}

# ── Mata processos em uma porta ────────────────────────────────
kill_port() {
    local port="$1"
    # ss
    if command -v ss &>/dev/null; then
        local pids
        pids=$(ss -tlnp "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u)
        for pid in $pids; do kill -9 "$pid" 2>/dev/null || true; done
    fi
    # fuser
    command -v fuser &>/dev/null && fuser -k "${port}/tcp" 2>/dev/null || true
    # lsof
    if command -v lsof &>/dev/null; then
        local lpids
        lpids=$(lsof -ti tcp:"${port}" 2>/dev/null || true)
        [ -n "$lpids" ] && kill -9 $lpids 2>/dev/null || true
    fi
}

free_ports() {
    banner "Liberando portas..."
    kill_port "$PANEL_PORT"
    for p in 5000 5001 5050 5052 8000 8080 8888; do
        [ "$p" -ne "$PANEL_PORT" ] && kill_port "$p"
    done
    pkill -9 -f "python.*app\.py" 2>/dev/null || true
    sleep 1
    log "Portas liberadas"
}

# ── Dependências do sistema ────────────────────────────────────
install_system_deps() {
    banner "Instalando dependencias do sistema..."
    case "$OS_ID" in
        ubuntu|debian)
            apt-get update -qq 2>/dev/null
            DEBIAN_FRONTEND=noninteractive apt-get install -y \
                curl wget git build-essential libssl-dev libffi-dev \
                python3 python3-pip python3-venv python3-dev \
                libsqlite3-dev sqlite3 perl openssl jq ufw 2>/dev/null \
                || warn "Algumas dependencias podem ter falhado"

            # Instala o pacote venv específico da versão do Python disponível
            # Necessário no Debian onde python3.X-venv não é instalado como
            # dependência automática do python3-venv genérico
            local _py_ver
            _py_ver=$(python3 -c \
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" \
                2>/dev/null || echo "")
            if [ -n "$_py_ver" ]; then
                DEBIAN_FRONTEND=noninteractive apt-get install -y \
                    "python${_py_ver}-venv" "python${_py_ver}-dev" 2>/dev/null || true
            fi
            ;;
        centos|rhel|fedora|rocky|almalinux)
            if command -v dnf &>/dev/null; then
                dnf install -y curl wget git gcc openssl-devel libffi-devel \
                    python3 python3-pip python3-devel sqlite-devel sqlite \
                    perl jq 2>/dev/null
            else
                yum install -y curl wget git gcc openssl-devel libffi-devel \
                    python3 python3-pip python3-devel sqlite-devel sqlite \
                    perl jq 2>/dev/null
            fi
            ;;
        *)
            apt-get update -qq 2>/dev/null
            apt-get install -y curl wget git python3 python3-pip python3-venv \
                libsqlite3-dev sqlite3 perl openssl jq 2>/dev/null || true
            ;;
    esac
    log "Dependencias do sistema instaladas"
}

# ── Seleciona Python funcional com sqlite3 ────────────────────
check_python() {
    banner "Verificando Python..."

    # Testa se um interpretador tem sqlite3 funcionando
    python_has_sqlite() {
        "$1" -c "import sqlite3; sqlite3.connect(':memory:').close()" 2>/dev/null
    }

    python_version_ok() {
        local ver
        ver=$("$1" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null)
        local major=${ver%%.*}
        local minor=${ver##*.}
        [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 8 ]
    }

    # Preferir Python do sistema (apt/yum) que normalmente tem sqlite3 compilado
    # /usr/bin primeiro, depois /usr/local/bin (compilações manuais que podem estar quebradas)
    local candidates=(
        /usr/bin/python3.12
        /usr/bin/python3.11
        /usr/bin/python3.10
        /usr/bin/python3.9
        /usr/bin/python3.8
        /usr/bin/python3
        /usr/local/bin/python3.12
        /usr/local/bin/python3.11
        /usr/local/bin/python3.10
        /usr/local/bin/python3.9
        /usr/local/bin/python3.8
        /usr/local/bin/python3
    )

    for cmd in "${candidates[@]}"; do
        [ -x "$cmd" ] || continue
        if python_version_ok "$cmd" && python_has_sqlite "$cmd"; then
            PYTHON_CMD="$cmd"
            local ver
            ver=$("$cmd" -c "import sys; print('%d.%d' % sys.version_info[:2])")
            log "Python $ver com sqlite3 OK: $cmd"
            return 0
        else
            if [ -x "$cmd" ]; then
                local ver
                ver=$("$cmd" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null || echo "?")
                warn "  $cmd (Python $ver) — sqlite3 ausente ou versao < 3.8, ignorando"
            fi
        fi
    done

    # Nenhum funcional encontrado — tenta instalar Python do sistema
    warn "Nenhum Python 3.8+ com sqlite3 encontrado. Tentando instalar..."
    case "$OS_ID" in
        ubuntu|debian)
            # Seleciona versões a tentar com base no OS e versão
            # Ubuntu: python3.10 disponível na maioria das versões suportadas
            # Debian 12 (Bookworm): python3.11 padrão
            # Debian 11 (Bullseye): python3.9 padrão — python3.10 NÃO existe no apt oficial
            local _try_vers=()
            if [ "$OS_ID" = "debian" ]; then
                local _dver="${OS_VER%%.*}"
                if [ "${_dver:-0}" -ge 12 ] 2>/dev/null; then
                    _try_vers=(python3.11 python3.12 python3.10 python3.9)
                else
                    # Debian 11 e anteriores
                    _try_vers=(python3.9 python3.11 python3.10)
                fi
            else
                # Ubuntu
                _try_vers=(python3.10 python3.11 python3.12 python3.8)
            fi

            for _pyv in "${_try_vers[@]}"; do
                DEBIAN_FRONTEND=noninteractive apt-get install -y \
                    libsqlite3-dev "${_pyv}" "${_pyv}-venv" "${_pyv}-dev" \
                    2>/dev/null || true
                if [ -x "/usr/bin/${_pyv}" ] && python_has_sqlite "/usr/bin/${_pyv}"; then
                    PYTHON_CMD="/usr/bin/${_pyv}"
                    log "${_pyv} instalado com sqlite3 OK"
                    return 0
                fi
            done

            # Último recurso: python3 genérico do sistema
            if [ -x /usr/bin/python3 ] && python_has_sqlite /usr/bin/python3; then
                PYTHON_CMD=/usr/bin/python3
                log "Usando python3 genérico do sistema com sqlite3 OK"
                return 0
            fi
            ;;
    esac

    error "Nenhum Python 3.8+ com suporte a sqlite3 encontrado.\
\nO Python em /usr/local/bin/ parece ter sido compilado sem --enable-loadable-sqlite-extensions.\
\n\nPara corrigir manualmente, execute:\
\n  apt-get install -y libsqlite3-dev python3.9 python3.9-venv   # Debian 11\
\n  apt-get install -y libsqlite3-dev python3.11 python3.11-venv  # Debian 12\
\n  apt-get install -y libsqlite3-dev python3.10 python3.10-venv  # Ubuntu\
\nE reexecute o instalador."
}

# ── Remove conflitos git ───────────────────────────────────────
remove_git_conflicts() {
    local file="$1"
    if grep -qP '^(<{7}|={7}|>{7})' "$file" 2>/dev/null; then
        warn "Conflito git em $file — removendo marcadores..."
        python3 - "$file" << 'PYEOF'
import sys
path = sys.argv[1]
with open(path, 'r', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()
result = []
in_conflict = False
keep = True
for line in lines:
    if line.startswith('<<<<<<<'):
        in_conflict = True; keep = True; continue
    elif line.startswith('=======') and in_conflict:
        keep = False; continue
    elif line.startswith('>>>>>>>') and in_conflict:
        in_conflict = False; keep = True; continue
    if not (in_conflict and not keep):
        result.append(line)
with open(path, 'w', encoding='utf-8') as f:
    f.writelines(result)
print(f"  Conflitos removidos em {path}")
PYEOF
    fi
}

# ── Verifica sintaxe Python ────────────────────────────────────
check_syntax() {
    local file="$1"
    remove_git_conflicts "$file"
    if ! $PYTHON_CMD -m py_compile "$file" 2>/dev/null; then
        $PYTHON_CMD -m py_compile "$file"
        error "Erro de sintaxe em $file"
    fi
}

copy_files() {
    # Sempre forca sincronizacao com GitHub — nunca usa arquivos locais
    # Isso garante que push --force no GitHub seja refletido na instalacao

    info "Sincronizando com repositorio GitHub..."

    if [ -d "$INSTALL_DIR/.git" ]; then
        banner "Repositorio local detectado — forcando alinhamento com GitHub..."
        cd "$INSTALL_DIR" || true
        git remote set-url origin "$REPO_URL" 2>/dev/null || \
            git remote add origin "$REPO_URL" 2>/dev/null || true
        # Busca tudo incluindo rewrites causados por push --force
        git fetch --all --prune 2>/dev/null || git fetch origin 2>/dev/null || true
        # Reset hard: descarta QUALQUER alteracao local e alinha com o GitHub
        git reset --hard origin/main 2>/dev/null || \
            git reset --hard origin/master 2>/dev/null || \
            git reset --hard FETCH_HEAD 2>/dev/null || true
        # Remove arquivos nao rastreados (pyc, caches etc)
        git clean -fd 2>/dev/null || true
        log "Codigo forcado para versao atual do GitHub"
        _apply_hotfixes
        return 0
    fi

    # Diretorio existe mas sem .git — remove e clona limpo
    if [ -d "$INSTALL_DIR" ]; then
        warn "Diretorio $INSTALL_DIR sem git. Removendo para clonar limpo..."
        rm -rf "$INSTALL_DIR"
    fi

    info "Clonando: $REPO_URL"
    if git clone --depth=1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
        log "Clone concluido com sucesso"
        _apply_hotfixes
        return 0
    fi

    warn "Clone com --depth=1 falhou. Tentando clone completo..."
    if git clone "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
        log "Clone completo concluido"
        _apply_hotfixes
        return 0
    fi

    error "Nao foi possivel clonar: $REPO_URL\nVerifique a conexao com a internet e tente novamente."
}


# ── Corrige bugs conhecidos no app.py após cópia/clone ─────────
_apply_hotfixes() {
    local f="$INSTALL_DIR/app.py"
    [ -f "$f" ] || return

    # Fix 1: PORT vazio causa crash — usar fallback com "or"
    sed -i 's/int(os\.environ\.get(.PORT., [0-9]\+))/int(os.environ.get("PORT") or 2083)/g' "$f" 2>/dev/null || true

    # Fix 2: payer.email com domínio local é rejeitado pelo Mercado Pago
    # Garante que _safe_user é definido ANTES do dict pix_payload
    python3 - "$f" << 'PYEOF'
import sys, re

path = sys.argv[1]
with open(path) as fh:
    src = fh.read()

# Substituir email de dominio local por dominio valido
src = re.sub(
    r"""f["']\{[^}]*username[^}]*\}@renovacao\.local["']""",
    'f"{_safe_user}@email.com"',
    src
)

# Garantir que _safe_user esta definido antes de pix_payload
if '_safe_user' not in src:
    src = src.replace(
        'pix_payload = {',
        "_safe_user = ''.join(c for c in u['username'] if c.isalnum() or c in '-_') or 'pagador'\n        pix_payload = {",
        1
    )

with open(path, 'w') as fh:
    fh.write(src)
PYEOF

    python3 -m py_compile "$f" 2>/dev/null && log "Hotfixes aplicados com sucesso" || warn "Verifique app.py manualmente"
}

# ── Instala painel ─────────────────────────────────────────────
install_panel() {
    banner "Instalando Painel Master..."
    copy_files

    cd "$INSTALL_DIR" || error "Nao foi possivel entrar em $INSTALL_DIR"

    for f in app.py db.py requirements.txt; do
        [ -f "$f" ] || error "Arquivo essencial ausente: $f"
    done
    [ -d "templates" ]            || error "Pasta templates/ ausente"
    [ -f "templates/login.html" ] || error "templates/login.html ausente"
    log "Estrutura de arquivos OK"

    info "Verificando sintaxe dos arquivos Python..."
    for pyfile in app.py db.py server_comm.py backup.py; do
        [ -f "$pyfile" ] && check_syntax "$pyfile"
    done
    log "Sintaxe Python OK"

    # Restaura banco
    if [ -n "$DB_BACKUP" ] && [ -f "$DB_BACKUP" ]; then
        cp "$DB_BACKUP" "$INSTALL_DIR/painel.db"
        log "Banco de dados restaurado"
    fi

    info "Criando ambiente virtual com: $PYTHON_CMD"
    $PYTHON_CMD -m venv venv 2>/dev/null || error "Falha ao criar virtualenv"

    # Valida que o venv tem sqlite3 também
    if ! venv/bin/python -c "import sqlite3" 2>/dev/null; then
        error "O virtualenv tambem nao tem sqlite3.
O Python $PYTHON_CMD foi compilado sem suporte a SQLite.

Solucao: instale python3.8 do sistema:
  apt-get install python3.8 python3.8-venv libsqlite3-dev
E reexecute o instalador."
    fi
    log "sqlite3 no virtualenv: OK"

    source venv/bin/activate
    pip install --upgrade pip setuptools wheel -q 2>/dev/null
    info "Instalando dependencias Python..."
    if pip install -r requirements.txt -q 2>/dev/null; then
        log "Dependencias instaladas"
    else
        warn "Instalacao em lote falhou. Instalando pacote a pacote..."
        while IFS= read -r pkg; do
            [[ -z "$pkg" || "$pkg" =~ ^# ]] && continue
            pkg_name="${pkg%%[><=!]*}"
            pip install "$pkg" -q 2>/dev/null \
                && info "  OK: $pkg_name" \
                || warn "  FALHA: $pkg_name"
        done < requirements.txt
    fi
    deactivate
    log "Ambiente Python pronto: $INSTALL_DIR/venv"
}

# ── Serviço systemd ────────────────────────────────────────────
configure_service() {
    banner "Configurando servico systemd..."
    kill_port "$PANEL_PORT"
    sleep 1

    SECRET_KEY=$(openssl rand -hex 32)

    cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Painel Master SSH Manager
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="PORT=${PANEL_PORT:-2083}"
Environment="SECRET_KEY=${SECRET_KEY}"
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" 2>/dev/null
    systemctl start  "$SERVICE_NAME"

    info "Aguardando servico inicializar..."
    for i in $(seq 1 10); do
        sleep 1
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            log "Servico ativo apos ${i}s"
            return 0
        fi
        printf "."
    done
    echo ""
    warn "Servico nao iniciou. Ultimos logs:"
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager
}

# ── Firewall ───────────────────────────────────────────────────
open_firewall() {
    banner "Liberando portas..."
    local ports=("${PANEL_PORT}" 80 443 2053 2083 2087 2096 8443)
    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        for p in "${ports[@]}"; do ufw allow "${p}/tcp" 2>/dev/null || true; done
        log "UFW: portas liberadas"
    fi
    if command -v firewall-cmd &>/dev/null && firewall-cmd --state 2>/dev/null | grep -q running; then
        for p in "${ports[@]}"; do firewall-cmd --permanent --add-port="${p}/tcp" 2>/dev/null || true; done
        firewall-cmd --permanent --add-service=http  2>/dev/null || true
        firewall-cmd --permanent --add-service=https 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
    fi
    for p in "${ports[@]}"; do
        iptables -I INPUT -p tcp --dport "${p}" -j ACCEPT 2>/dev/null || true
    done
}

# ── Nginx base: sempre instala com suporte a IP direto + Cloudflare ──────────
setup_nginx_base() {
    banner "Instalando nginx..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y nginx 2>/dev/null || true
    elif command -v dnf &>/dev/null; then
        dnf install -y nginx 2>/dev/null || true
    fi

    # Garante que o módulo real_ip do nginx está ativo
    local cf_ips="
    # Cloudflare IPv4
    set_real_ip_from 103.21.244.0/22;
    set_real_ip_from 103.22.200.0/22;
    set_real_ip_from 103.31.4.0/22;
    set_real_ip_from 104.16.0.0/13;
    set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 108.162.192.0/18;
    set_real_ip_from 131.0.72.0/22;
    set_real_ip_from 141.101.64.0/18;
    set_real_ip_from 162.158.0.0/15;
    set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 173.245.48.0/20;
    set_real_ip_from 188.114.96.0/20;
    set_real_ip_from 190.93.240.0/20;
    set_real_ip_from 197.234.240.0/22;
    set_real_ip_from 198.41.128.0/17;
    # Cloudflare IPv6
    set_real_ip_from 2400:cb00::/32;
    set_real_ip_from 2606:4700::/32;
    set_real_ip_from 2803:f800::/32;
    set_real_ip_from 2405:b500::/32;
    set_real_ip_from 2405:8100::/32;
    set_real_ip_from 2a06:98c0::/29;
    set_real_ip_from 2c0f:f248::/32;
    real_ip_header CF-Connecting-IP;"

    # Snippet de proxy_params reutilizável
    cat > /etc/nginx/snippets/proxy_params.conf << 'SNIPPET'
proxy_http_version 1.1;
proxy_set_header   Host              $http_host;
proxy_set_header   X-Real-IP         $remote_addr;
proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header   X-Forwarded-Proto $scheme;
proxy_set_header   CF-Connecting-IP  $http_cf_connecting_ip;
proxy_read_timeout 120s;
client_max_body_size 110M;
SNIPPET

    # ── Bloco catch-all porta 80 (IP direto + Cloudflare HTTP Flexible) ──────
    cat > /etc/nginx/sites-available/painel-ip << IPCNF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
${cf_ips}

    location / {
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
    }
}
IPCNF

    # ── Bloco catch-all porta 443 com cert autoassinado ───────────────────────
    # Necessário para Cloudflare Full SSL sem domínio configurado
    if [ ! -f /etc/nginx/ssl/painel-selfsigned.crt ]; then
        mkdir -p /etc/nginx/ssl
        openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
            -keyout /etc/nginx/ssl/painel-selfsigned.key \
            -out    /etc/nginx/ssl/painel-selfsigned.crt \
            -subj "/CN=painel-master/O=PainelMaster/C=BR" 2>/dev/null
    fi

    cat > /etc/nginx/sites-available/painel-ip-ssl << SSLCNF
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_certificate     /etc/nginx/ssl/painel-selfsigned.crt;
    ssl_certificate_key /etc/nginx/ssl/painel-selfsigned.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
${cf_ips}

    location / {
        proxy_pass         http://127.0.0.1:${PANEL_PORT:-2083};
        include            /etc/nginx/snippets/proxy_params.conf;
        proxy_set_header   X-Forwarded-Proto https;
    }
}
SSLCNF

    # Ativa os blocos base e remove o default do nginx
    ln -sf /etc/nginx/sites-available/painel-ip     /etc/nginx/sites-enabled/painel-ip     2>/dev/null || true
    ln -sf /etc/nginx/sites-available/painel-ip-ssl /etc/nginx/sites-enabled/painel-ip-ssl 2>/dev/null || true
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

    # ── Override systemd: garante que nginx reinicia automaticamente se cair ──
    mkdir -p /etc/systemd/system/nginx.service.d/
    cat > /etc/systemd/system/nginx.service.d/restart.conf << 'NGINXOVER'
[Service]
Restart=always
RestartSec=5
NGINXOVER
    systemctl daemon-reload

    # ── Watchdog cron: reinicia nginx a cada 5 min se estiver fora ────────────
    (crontab -l 2>/dev/null | grep -v 'nginx.*watchdog'; \
     echo "*/5 * * * * systemctl is-active --quiet nginx || (systemctl restart nginx 2>/dev/null; logger 'nginx-watchdog: reiniciado automaticamente')") | crontab -

    # ── Hook de renovação certbot: recarrega nginx após renovar certificado ───
    mkdir -p /etc/letsencrypt/renewal-hooks/post/
    cat > /etc/letsencrypt/renewal-hooks/post/reload-nginx.sh << 'HOOK'
#!/bin/bash
# Recarrega nginx após renovação automática do Let's Encrypt
nginx -t 2>/dev/null \
    && systemctl reload nginx 2>/dev/null \
    || systemctl restart nginx 2>/dev/null
logger "certbot-hook: nginx recarregado após renovação SSL"
HOOK
    chmod +x /etc/letsencrypt/renewal-hooks/post/reload-nginx.sh

    nginx -t && systemctl enable nginx && systemctl restart nginx
    log "nginx configurado: HTTP porta 80, HTTPS porta 443 (self-signed), watchdog ativo"
}

# ── SSL com domínio (nginx + certbot Let's Encrypt) ───────────────────────────
setup_ssl() {
    setup_nginx_base  # Garante nginx base + IP direto sempre funcionando

    banner "Configuração SSL/HTTPS com domínio"
    echo ""
    echo -e "  ${YELLOW}Modos suportados:${NC}"
    echo -e "  1) ${BOLD}IP direto${NC}:          http://IP:${PANEL_PORT}  ou  http://IP  (porta 80)"
    echo -e "  2) ${BOLD}Cloudflare HTTP${NC}:    Flexible SSL — origem HTTP, Cloudflare entrega HTTPS"
    echo -e "  3) ${BOLD}Cloudflare HTTPS${NC}:   Full SSL — já funciona com cert autoassinado (porta 443)"
    echo -e "  4) ${BOLD}Domínio + Let's Encrypt${NC}: HTTPS real com certbot (requer DNS apontando para este IP)"
    echo ""
    read -rp "  Deseja configurar domínio + Let's Encrypt agora? [s/N]: " DO_SSL
    [[ "$DO_SSL" =~ ^[Ss]$ ]] || {
        warn "Let's Encrypt pulado. Modos 1, 2 e 3 já estão funcionando."
        return
    }

    read -rp "  Digite o domínio (ex: painel.seusite.com.br): " DOMAIN
    [[ -z "$DOMAIN" ]] && { warn "Domínio em branco — certbot cancelado."; return; }

    read -rp "  E-mail para notificações do Let's Encrypt: " LE_EMAIL
    [[ -z "$LE_EMAIL" ]] && { warn "E-mail em branco — certbot cancelado."; return; }

    # ── Instala certbot conforme o sistema operacional ─────────────────────
    if command -v apt-get &>/dev/null; then
        if [ "$OS_ID" = "debian" ]; then
            # Debian: certbot via repositório apt oficial
            # snap NÃO é recomendado no Debian — não vem instalado por padrão
            # e requer reboot para funcionar corretamente
            info "Debian detectado — instalando certbot via apt..."
            DEBIAN_FRONTEND=noninteractive apt-get install -y \
                certbot python3-certbot-nginx 2>/dev/null || {
                warn "certbot via apt falhou. Tentando via pip no venv..."
                "$INSTALL_DIR/venv/bin/pip" install certbot certbot-nginx -q 2>/dev/null || true
            }
        else
            # Ubuntu: snap é o método recomendado pela EFF/Let's Encrypt
            apt-get remove -y certbot python3-certbot-nginx 2>/dev/null || true
            if ! command -v snap &>/dev/null; then
                apt-get install -y snapd 2>/dev/null || true
                export PATH="$PATH:/snap/bin"
                systemctl enable snapd.socket 2>/dev/null || true
                systemctl start  snapd.socket 2>/dev/null || true
                sleep 3
            fi
            snap install --classic certbot 2>/dev/null \
                || DEBIAN_FRONTEND=noninteractive apt-get install -y \
                    certbot python3-certbot-nginx 2>/dev/null \
                || true
            ln -sf /snap/bin/certbot /usr/bin/certbot 2>/dev/null || true
        fi
    elif command -v dnf &>/dev/null; then
        dnf install -y certbot python3-certbot-nginx 2>/dev/null || true
    fi

    # Verificação final — fallback pip se certbot ainda não estiver disponível
    if ! command -v certbot &>/dev/null; then
        warn "certbot não encontrado no PATH. Tentando via pip..."
        pip3 install certbot certbot-nginx --break-system-packages 2>/dev/null \
            || "$INSTALL_DIR/venv/bin/pip" install certbot certbot-nginx -q 2>/dev/null \
            || true
    fi

    local cf_ips="
    set_real_ip_from 103.21.244.0/22; set_real_ip_from 103.22.200.0/22;
    set_real_ip_from 104.16.0.0/13;   set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 162.158.0.0/15;  set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 173.245.48.0/20; set_real_ip_from 188.114.96.0/20;
    set_real_ip_from 197.234.240.0/22; set_real_ip_from 198.41.128.0/17;
    real_ip_header CF-Connecting-IP;"

    # Config nginx para o domínio (Let's Encrypt vai adicionar bloco 443)
    cat > "/etc/nginx/sites-available/${DOMAIN}" << DOMCNF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
${cf_ips}

    location / {
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
    }
}
DOMCNF

    ln -sf "/etc/nginx/sites-available/${DOMAIN}" "/etc/nginx/sites-enabled/${DOMAIN}" 2>/dev/null || true
    nginx -t && systemctl reload nginx

    certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${LE_EMAIL}" && {
        # ── Certbot OK: desativa cert autoassinado para evitar conflito na porta 443 ──
        # O painel-ip-ssl (default_server 443 self-signed) conflita com o bloco Let's
        # Encrypt que o certbot criou. Mantemos o arquivo em sites-available como
        # fallback, mas o removemos do sites-enabled.
        rm -f /etc/nginx/sites-enabled/painel-ip-ssl 2>/dev/null || true
        log "Bloco SSL autoassinado desativado — Let's Encrypt assume a porta 443"

        # Porta 2083 (Cloudflare HTTPS alternativa)
        cat >> "/etc/nginx/sites-available/${DOMAIN}" << EXTRA2083

server {
    listen 2083 ssl;
    listen [::]:2083 ssl;
    server_name ${DOMAIN};
    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
${cf_ips}
    location / {
        proxy_pass         http://127.0.0.1:${PANEL_PORT:-2083};
        include            /etc/nginx/snippets/proxy_params.conf;
        proxy_set_header   X-Forwarded-Proto https;
    }
}
EXTRA2083
        nginx -t && systemctl restart nginx
        log "Let's Encrypt OK: https://${DOMAIN}"
        log "Porta 2083 também disponível"
        SSL_DOMAIN="${DOMAIN}"
    } || warn "Certbot falhou. DNS ${DOMAIN} aponta para este IP? Verifique e tente: certbot --nginx -d ${DOMAIN}"
}


# ── Resultado ──────────────────────────────────────────────────
print_info() {
    SERVER_IP=$(curl -s --max-time 5 https://ipv4.icanhazip.com 2>/dev/null \
        || curl -s --max-time 5 https://api.ipify.org 2>/dev/null \
        || hostname -I | awk '{print $1}')

    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        ST="${GREEN}✔ Rodando${NC}"
    else
        ST="${RED}✗ Erro — execute: journalctl -u $SERVICE_NAME -n 50${NC}"
    fi

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║       PAINEL MASTER — INSTALACAO CONCLUIDA       ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Status:        $(echo -e $ST)"
    echo ""
    echo -e "  ${BOLD}— Modos de acesso disponíveis —${NC}"
    echo -e "  IP direto:          ${BOLD}http://${SERVER_IP}:${PANEL_PORT}${NC}"
    echo -e "  nginx HTTP (80):    ${BOLD}http://${SERVER_IP}${NC}"
    echo -e "  nginx HTTPS (443):  ${BOLD}https://${SERVER_IP}${NC}  ${YELLOW}(cert autoassinado)${NC}"
    echo -e "  Cloudflare HTTP:    ative o proxy (laranja) — origin HTTP, modo Flexible"
    echo -e "  Cloudflare HTTPS:   ative o proxy (laranja) — modo Full SSL (cert autoassinado OK)"
    if [ -n "${SSL_DOMAIN:-}" ]; then
        echo -e "  Domínio HTTPS:      ${BOLD}https://${SSL_DOMAIN}${NC}"
        echo -e "  Porta 2083 HTTPS:   ${BOLD}https://${SSL_DOMAIN}:2083${NC}"
        echo -e "  Webhook Telegram:   ${BOLD}https://${SSL_DOMAIN}/telegram/webhook${NC}"
    fi
    echo ""
    echo -e "  Login:         admin / admin123"
    echo -e "  Python usado:  ${PYTHON_CMD}"
    echo -e "  Diretorio:     ${INSTALL_DIR}"
    echo ""
    echo -e "  ${YELLOW}⚠  Troque a senha do admin após o primeiro login!${NC}"
    echo ""
    echo -e "  Comandos úteis:"
    echo -e "    journalctl -u ${SERVICE_NAME} -f          # logs em tempo real"
    echo -e "    journalctl -u ${SERVICE_NAME} -n 50       # últimas 50 linhas"
    echo -e "    systemctl restart ${SERVICE_NAME}         # reiniciar painel"
    echo -e "    nginx -t && systemctl reload nginx        # recarregar nginx"
    echo ""
}

# ══════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════
clear
banner "╔══════════════════════════════════════╗"
banner "║      PAINEL MASTER — INSTALADOR      ║"
banner "╚══════════════════════════════════════╝"
echo ""

SSL_DOMAIN=""
MODE="${1:-install}"   # install (default) | update

check_root
detect_arch
detect_os

if [ "$MODE" = "update" ]; then
    # ── Modo atualização: só sincroniza código e reinicia, sem apagar banco ──
    banner "Modo ATUALIZAÇÃO — sincronizando código do GitHub..."
    check_python
    free_ports

    # Salva banco antes de qualquer operação
    if [ -f "$INSTALL_DIR/painel.db" ]; then
        DB_BACKUP="/tmp/painel_backup_$(date +%s).db"
        cp "$INSTALL_DIR/painel.db" "$DB_BACKUP"
        warn "Banco salvo em: $DB_BACKUP"
    fi

    systemctl stop "$SERVICE_NAME" 2>/dev/null || true

    # Força alinhamento com GitHub (usa a mesma lógica de copy_files)
    copy_files

    # Restaura banco
    if [ -n "$DB_BACKUP" ] && [ -f "$DB_BACKUP" ]; then
        cp "$DB_BACKUP" "$INSTALL_DIR/painel.db"
        log "Banco restaurado"
    fi

    # Atualiza dependências Python sem recriar o venv
    if [ -f "$INSTALL_DIR/venv/bin/pip" ]; then
        info "Atualizando dependencias Python..."
        "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q 2>/dev/null || true
    fi

    systemctl daemon-reload
    systemctl start "$SERVICE_NAME"
    sleep 3
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log "Painel atualizado e rodando!"
    else
        warn "Verifique: journalctl -u $SERVICE_NAME -n 50"
    fi
    print_info
else
    # ── Modo instalação completa ──────────────────────────────────────────
    uninstall_existing
    free_ports
    install_system_deps
    check_python
    install_panel
    configure_service
    open_firewall
    setup_ssl
    print_info
fi
