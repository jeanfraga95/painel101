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
REPO_URL="https://github.com/jeanfraga95/painel101"
# Repositorio PRIVADO: o clone/fetch exige autenticacao. Informe o token
# (PAT do GitHub com escopo "repo" ou um deploy key) no ambiente:
#     export PAINEL_TOKEN=github_pat_xxxx
# Se nao for informado e o repo exigir, o instalador pergunta no terminal.
# O token NAO fica gravado no .git/config (vai no header HTTP do git).
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
    # Libera SOMENTE a porta do painel. A versao anterior matava tudo que
    # estivesse em 2083/5001/5050/5052/8000/8080/8888 - e 8888 e porta
    # comum de proxy SSH do cliente: o update derrubava um servico que nao
    # tinha nada a ver com o painel.
    kill_port "$PANEL_PORT"
    # apenas o processo do proprio painel (caminho completo do app.py)
    pkill -9 -f "${INSTALL_DIR}/app\.py" 2>/dev/null || true
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

# Autentica o git no repo privado SEM gravar o token no .git/config
# (o token vai no header Authorization e fica so no processo atual).
git_auth() {
    if [ -n "${PAINEL_TOKEN:-}" ]; then
        local basic
        basic="$(printf 'x-access-token:%s' "$PAINEL_TOKEN" | base64 | tr -d '\n')"
        git -c "http.extraheader=Authorization: Basic ${basic}" "$@"
    else
        git "$@"
    fi
}

copy_files() {
    # Sempre forca sincronizacao com GitHub — nunca usa arquivos locais
    # Isso garante que push --force no GitHub seja refletido na instalacao

    info "Sincronizando com repositorio GitHub..."

    if [ -d "$INSTALL_DIR/.git" ]; then
        banner "Repositorio local detectado — forcando alinhamento com GitHub..."
        cd "$INSTALL_DIR" || true
        # Tenta primeiro o remote que ja existe (pode estar por SSH com uma
        # chave do cliente, ou por HTTPS com token salvo no credential
        # helper). So substitui pelo REPO_URL se o fetch com ele falhar.
        if ! git_auth fetch --all --prune 2>/dev/null && ! git_auth fetch origin 2>/dev/null; then
            git remote set-url origin "$REPO_URL" 2>/dev/null || \
            git remote add origin "$REPO_URL" 2>/dev/null || true
        fi
        # Busca tudo incluindo rewrites causados por push --force
        if ! git_auth fetch --all --prune 2>/dev/null && ! git_auth fetch origin 2>/dev/null; then
            warn "Nao foi possivel baixar do GitHub (repositorio privado sem token?)."
            if [ -z "${PAINEL_TOKEN:-}" ] && [ -t 0 ]; then
                read -rp "  Token de acesso do GitHub (PAT com escopo repo): " PAINEL_TOKEN
                export PAINEL_TOKEN
            fi
            if ! git_auth fetch --all --prune 2>/dev/null && ! git_auth fetch origin 2>/dev/null; then
                error "Atualizacao abortada: nao da para alinhar com o GitHub.\nO codigo atual seria mantido (sem atualizacao). Use PAINEL_TOKEN=seu_token bash install.sh update"
            fi
        fi
        # Reset hard: descarta QUALQUER alteracao local e alinha com o GitHub
        if ! git reset --hard origin/main 2>/dev/null && ! git reset --hard origin/master 2>/dev/null; then
            error "Atualizacao abortada: nao foi possivel alinhar o codigo com o GitHub."
        fi
        # Remove arquivos nao rastreados (pyc, caches etc)
        # -x NAO e usado: arquivos ignorados (.gitignore) como painel.db,
        # .secret_key e venv/ sao preservados (e o .gitignore e o que protege
        # o banco e a chave de cifragem das senhas SSH).
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
    if git_auth clone --depth=1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
        log "Clone concluido com sucesso"
        _apply_hotfixes
        return 0
    fi

    warn "Clone com --depth=1 falhou. Tentando clone completo..."
    if git_auth clone "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
        log "Clone completo concluido"
        _apply_hotfixes
        return 0
    fi

    # Repo privado sem credencial: pergunta o token e tenta de novo.
    if [ -z "${PAINEL_TOKEN:-}" ] && [ -t 0 ]; then
        warn "Nao foi possivel clonar (repo privado? token ausente)."
        read -rp "  Token de acesso do GitHub (PAT com escopo repo): " PAINEL_TOKEN
        export PAINEL_TOKEN
        if [ -n "$PAINEL_TOKEN" ] && git_auth clone --depth=1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
            log "Clone concluido com sucesso (autenticado)"
            _apply_hotfixes
            return 0
        fi
    fi

    error "Nao foi possivel clonar: $REPO_URL\nVerifique a conexao com a internet e, se o repositorio for privado, export PAINEL_TOKEN=seu_token"
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

# ── Vhost do domínio (porta 80) ───────────────────────────────────
# Usado na instalação e na atualização: assim um cliente que já tinha
# instalado o painel recebe o mesmo hardening (HTTPS real, CSP, rate
# limit) sem precisar reinstalar.
write_domain_vhost() {
    local dom="$1"
    [ -z "$dom" ] && return 1
    cat > "/etc/nginx/sites-available/${dom}" << DOMCNF
server {
    listen 80;
    listen [::]:80;
    server_name ${dom};
${cf_ips}

    add_header Strict-Transport-Security "max-age=31536000" always;

    # Cliente via Cloudflare em http:// -> https:// (senha de login em claro).
    if (\$painel_https_target) {
        return 301 \$painel_https_target;
    }

    # Instalador distribuido pelo proprio painel: funciona mesmo com o
    # codigo-fonte em repositorio privado (o raw.githubusercontent nao
    # serviria mais).
    #   curl -sSL https://SEU-DOMINIO/install.sh | bash
    location = /install.sh {
        alias /opt/painel-master/install.sh;
        default_type application/x-shellscript;
    }

    include /etc/nginx/snippets/painel-security.conf;
    include /etc/nginx/snippets/vnc-location.conf;

    location = /login {
        limit_req zone=painel_login burst=30 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location = /user/login {
        limit_req zone=painel_login burst=30 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location ~ ^/check(user)?(/|\$) {
        limit_req zone=painel_checkuser burst=100 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location / {
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }
}
DOMCNF
    # O domínio canônico e conhecido aqui: grava no redirect do mapa para
    # que o Location aponte sempre para o domínio oficial (nunca para o Host
    # enviado pelo cliente - isso viraria open redirect).
    sed -i "s|__PANEL_DOMAIN__|${dom}|g" /etc/nginx/conf.d/painel-scheme.conf 2>/dev/null || true
    ln -sf "/etc/nginx/sites-available/${dom}" "/etc/nginx/sites-enabled/${dom}" 2>/dev/null || true
}

# ── Descobre o domínio já configurado ──────────────────────────────
detect_domain() {
    local f d
    for f in /etc/nginx/sites-enabled/*; do
        [ -f "$f" ] || continue
        d="$(grep -m1 -oE 'server_name[[:space:]]+[a-zA-Z0-9.-]+' "$f" 2>/dev/null | awk '{print $2}')"
        if [ -n "$d" ] && [ "$d" != "_" ]; then
            echo "$d"
            return 0
        fi
    done
    # fallback: URL do checkuser guardada no proprio painel
    if [ -f "$INSTALL_DIR/painel.db" ]; then
        d="$(python3 - "$INSTALL_DIR/painel.db" << 'PYDB' 2>/dev/null
import re, sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    for (v,) in c.execute("SELECT value FROM settings WHERE key='checkuser_url'"):
        m = re.search(r'https?://([^/]+)', v or '')
        if m:
            print(m.group(1)); break
except Exception:
    pass
PYDB
)"
        [ -n "$d" ] && { echo "$d"; return 0; }
    fi
    return 1
}

# ── Reaplica o hardening do nginx (usado na atualização) ───────────
apply_nginx_hardening() {
    banner "Reaplicando configuracao segura do nginx..."
    # Backup do estado atual: se o nginx -t falhar, a configuracao anterior
    # volta (o painel nunca fica com o nginx quebrado no disco).
    local bkp
    bkp="$(mktemp -d)"
    cp -a /etc/nginx/conf.d "$bkp/" 2>/dev/null || true
    cp -a /etc/nginx/snippets "$bkp/" 2>/dev/null || true
    cp -a /etc/nginx/sites-available "$bkp/" 2>/dev/null || true
    cp -a /etc/nginx/sites-enabled "$bkp/" 2>/dev/null || true

    setup_nginx_base || { warn "Falha ao (re)aplicar o nginx"; return 0; }
    local dom
    if dom="$(detect_domain)"; then
        write_domain_vhost "$dom"
        log "Vhost do dominio atualizado: $dom"
    else
        disable_https_redirect_if_no_domain
    fi
    if nginx -t 2>/dev/null; then
        systemctl reload nginx 2>/dev/null && log "nginx recarregado (HTTPS real, CSP, rate limit)"
    else
        warn "nginx -t falhou: restaurando a configuracao anterior"
        for d in conf.d snippets sites-available sites-enabled; do
            rm -rf "/etc/nginx/$d"
            mkdir -p "/etc/nginx/$d"
            if [ -d "$bkp/$d" ]; then
                cp -a "$bkp/$d/." "/etc/nginx/$d/" 2>/dev/null || true
            fi
        done
        if nginx -t 2>/dev/null; then
            systemctl reload nginx 2>/dev/null && log "nginx restaurado e recarregado"
        else
            error "nginx -t falhou mesmo com o rollback: o nginx precisa de atencao manual"
        fi
    fi
    rm -rf "$bkp"
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
    # (global de proposito: write_domain_vhost() tambem usa)
    cf_ips="
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

    # ── Esquema HTTPS real atrás do Cloudflare ─────────────────────────────
    # O Cloudflare termina o TLS do cliente mas fala HTTP com a origem. Sem
    # isto o nginx sobrescreve o X-Forwarded-Proto com $scheme (http) e o app
    # acha que a requisição é HTTP: o cookie de sessão sai sem "Secure" e o
    # webhook do Mercado Pago é montado com http://.
    # O geo (e não o map) é usado porque só ele casa CIDR com fonte variável.
    cat > /etc/nginx/conf.d/painel-scheme.conf << 'SCHEMECNF'
geo $realip_remote_addr $painel_cf_client {
    default            0;
    103.21.244.0/22    1;
    103.22.200.0/22    1;
    103.31.4.0/22      1;
    104.16.0.0/13      1;
    104.24.0.0/14      1;
    108.162.192.0/18   1;
    131.0.72.0/22      1;
    141.101.64.0/18    1;
    162.158.0.0/15     1;
    172.64.0.0/13      1;
    173.245.48.0/20    1;
    188.114.96.0/20    1;
    190.93.240.0/20    1;
    197.234.240.0/22   1;
    198.41.128.0/17    1;
    2400:cb00::/32     1;
    2606:4700::/32     1;
    2803:f800::/32     1;
    2405:b500::/32     1;
    2405:8100::/32     1;
    2a06:98c0::/29     1;
    2c0f:f248::/32     1;
}

map "$painel_cf_client:$http_x_forwarded_proto" $painel_proto {
    default     $scheme;
    "1:https"   https;
}

# Redireciona para HTTPS o que veio do Cloudflare em http://.
# Exceção: /check* (apps de VPN não seguem redirect) e o host é fixo no
# domínio canônico para não virar open redirect.
map "$painel_cf_client:$http_x_forwarded_proto:$uri" $painel_https_target {
    default                       "";
    "~^1:https:"                  "";
    "~^1:http:/check"             "";
    "~^1:http:"                   "https://__PANEL_DOMAIN__$uri";
}
SCHEMECNF

    # Snippet de proxy_params reutilizável
    cat > /etc/nginx/snippets/proxy_params.conf << 'SNIPPET'
proxy_http_version 1.1;
proxy_set_header   Host              $http_host;
proxy_set_header   X-Real-IP         $remote_addr;
proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header   X-Forwarded-Proto $painel_proto;
proxy_set_header   CF-Connecting-IP  $http_cf_connecting_ip;
proxy_read_timeout 120s;
client_max_body_size 110M;
SNIPPET

    # ── Proxy do console VNC (noVNC) ─────────────────────────────────────
    # O painel reescreve estes dois arquivos quando inicia e a cada servidor
    # cadastrado. O instalador tambem os cria: sem o snippet, o mapa com
    # $vnc_sid fica sem origem e o "nginx -t" falha com
    # "unknown vnc_sid variable" — o que derrubaria o proxy no update.
    mkdir -p /etc/nginx/snippets /etc/nginx/conf.d \
             /etc/nginx/sites-available /etc/nginx/sites-enabled
    if [ ! -f /etc/nginx/conf.d/vnc-upstreams.conf ]; then
        cat > /etc/nginx/conf.d/vnc-upstreams.conf << 'VNCUPCNF'
# Mapa de servidores VNC: id do servidor -> backend websockify.
# Gerado pelo painel (regenerado quando a lista de servidores muda).
# default aponta para porta descartavel: um id desconhecido nao deve
# acabar conectado em algum servidor real.
map $vnc_sid $vnc_backend {
    default        "127.0.0.1:1";
}
VNCUPCNF
    fi
    if [ ! -f /etc/nginx/snippets/vnc-location.conf ]; then
        cat > /etc/nginx/snippets/vnc-location.conf << 'VNCSNIPCNF'
# ---- Console VNC (NoVNC) ---------------------------------------------------
# O navegador nunca fala com o servidor VNC direto: passa pelo painel. O nginx
# valida a sessao do Flask com auth_request antes de encaminhar, entao o
# console fica inacessivel a quem nao estiver logado como admin - e a porta do
# websockify no servidor so aceita o IP do painel.
location ~ ^/servers/vnc/(?<vnc_sid>\\d+)/(?<vnc_rest>.*)$ {
    auth_request /_vnc_authz;
    error_page 401 = @vnc_unauth;
    error_page 403 = @vnc_denied;

    proxy_pass http://$vnc_backend/$vnc_rest;

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header Upgrade           $http_upgrade;
    proxy_set_header Connection        "upgrade";
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $painel_proto;
    proxy_set_header CF-Connecting-IP  $http_cf_connecting_ip;

    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}

location @vnc_unauth {
    default_type application/json;
    return 401 '{"success":false,"message":"Sessao expirada. Faca login novamente."}';
}

location @vnc_denied {
    default_type application/json;
    return 403 '{"success":false,"message":"Acesso negado: requer admin."}';
}

# Subrequisicao do auth_request. "internal" impede que alguem chame pela
# internet e use isto para sondar sessao de terceiros.
location = /_vnc_authz {
    internal;
    proxy_pass http://127.0.0.1:__PANEL_PORT__/_vnc_authz;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie           $http_cookie;
    proxy_set_header X-Original-URI   $request_uri;
}
VNCSNIPCNF
        sed -i "s|__PANEL_PORT__|${PANEL_PORT:-2083}|g" /etc/nginx/snippets/vnc-location.conf
    fi

    # ── Zonas de limite de requisicao (contexto http) ──────────────────────
    # Defense in depth: o app tambem limita (por usuario/IP), mas o nginx
    # barra o ataque bruto mesmo que o app nao veja o IP real.
    cat > /etc/nginx/conf.d/painel-limit.conf << 'LIMITCNF'
limit_req_zone $binary_remote_addr zone=painel_login:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=painel_checkuser:10m rate=30r/s;
LIMITCNF

    # ── Headers de seguranca e esconder a versao do nginx ───────────────────
    cat > /etc/nginx/snippets/painel-security.conf << 'SECCNF'
server_tokens off;
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "same-origin" always;
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;

# Content-Security-Policy: trava as origens de onde o painel pode carregar
# codigo/estilo/fonte. O que o app usa de fora: Bootstrap e FullCalendar no
# cdn.jsdelivr.net e o Font Awesome no cdnjs.cloudflare.com - nada mais.
# O QR do PIX e um data: URI gerado no servidor (sem servico externo).
# "unsafe-inline" em script/style e necessario porque os templates tem
# <script> e <style> inline; o ganho e bloquear origem desconhecida,
# frame/object/base e o base-uri hijacking.
add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; font-src 'self' data: https://cdnjs.cloudflare.com; img-src 'self' data: blob:; connect-src 'self' ws: wss:; frame-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'" always;
SECCNF

    # ── Bloco catch-all porta 80 (IP direto + Cloudflare HTTP Flexible) ──────
    cat > /etc/nginx/sites-available/painel-ip << IPCNF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
${cf_ips}

    # Instalador distribuido pelo proprio painel: funciona mesmo com o
    # codigo-fonte em repositorio privado (o raw.githubusercontent nao
    # serviria mais).
    #   curl -sSL https://SEU-DOMINIO/install.sh | bash
    location = /install.sh {
        alias /opt/painel-master/install.sh;
        default_type application/x-shellscript;
    }

    include /etc/nginx/snippets/painel-security.conf;
    include /etc/nginx/snippets/vnc-location.conf;

    # HSTS tambem no vhost HTTP: quem chega pelo Cloudflare recebe o header
    # na resposta HTTPS da borda e passa a exigir HTTPS no navegador.
    add_header Strict-Transport-Security "max-age=31536000" always;

    # Cliente via Cloudflare em http:// -> https:// (senha de login em claro).
    if (\$painel_https_target) {
        return 301 \$painel_https_target;
    }

    # Limite de requisicoes nas rotas de autenticacao/enumeracao.
    location = /login {
        limit_req zone=painel_login burst=30 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location = /user/login {
        limit_req zone=painel_login burst=30 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location ~ ^/check(user)?(/|\$) {
        limit_req zone=painel_checkuser burst=100 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location / {
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
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

    # Instalador distribuido pelo proprio painel: funciona mesmo com o
    # codigo-fonte em repositorio privado (o raw.githubusercontent nao
    # serviria mais).
    #   curl -sSL https://SEU-DOMINIO/install.sh | bash
    location = /install.sh {
        alias /opt/painel-master/install.sh;
        default_type application/x-shellscript;
    }

    include /etc/nginx/snippets/painel-security.conf;
    include /etc/nginx/snippets/vnc-location.conf;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location = /login {
        limit_req zone=painel_login burst=30 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location = /user/login {
        limit_req zone=painel_login burst=30 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location ~ ^/check(user)?(/|\$) {
        limit_req zone=painel_checkuser burst=100 nodelay;
        proxy_pass http://127.0.0.1:${PANEL_PORT:-2083};
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header Server;
    }

    location / {
        proxy_pass         http://127.0.0.1:${PANEL_PORT:-2083};
        include            /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header  Server;
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

    # (usa o cf_ips global de setup_nginx_base: lista completa de faixas do
    #  Cloudflare - com a lista curta o IP real do cliente nao era recuperado
    #  em algumas bordas e o rate-limit contava o IP da borda)

    # Config nginx para o domínio (Let's Encrypt vai adicionar bloco 443)
    write_domain_vhost "$DOMAIN"

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
    include /etc/nginx/snippets/painel-security.conf;
    add_header Strict-Transport-Security "max-age=31536000" always;
    location / {
        proxy_pass         http://127.0.0.1:${PANEL_PORT:-2083};
        include            /etc/nginx/snippets/proxy_params.conf;
        proxy_hide_header  Server;
    }
}
EXTRA2083
        nginx -t && systemctl restart nginx
        log "Let's Encrypt OK: https://${DOMAIN}"
        log "Porta 2083 também disponível"
        SSL_DOMAIN="${DOMAIN}"
    } || warn "Certbot falhou. DNS ${DOMAIN} aponta para este IP? Verifique e tente: certbot --nginx -d ${DOMAIN}"
}

# Sem domínio configurado não há redirect para HTTPS (o Cloudflare não é usado
# nesse modo). Desliga a regra para nunca emitir um Location quebrado.
disable_https_redirect_if_no_domain() {
    local f=/etc/nginx/conf.d/painel-scheme.conf
    [[ -f "$f" ]] || return 0
    if grep -q "__PANEL_DOMAIN__" "$f"; then
        sed -i 's|"~^1:http:"[[:space:]]*"https://__PANEL_DOMAIN__\$uri";|"~^1:http:" "";|' "$f"
        warn "Domínio não configurado: redirect HTTP->HTTPS desativado"
        nginx -t && systemctl reload nginx 2>/dev/null || true
    fi
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


# ── Remover painel instalado ──────────────────────────────────
remove_panel() {
    banner "Remover painel instalado"

    # Lista serviços painel-master relacionados
    local svcs
    svcs=$(systemctl list-units --all 2>/dev/null | grep -E 'painel-(master|demo)' | awk '{print $1}' | sed 's/\.service$//' | grep -v -- '-reset' | sort -u)
    # Também checa arquivos de serviço
    for f in /etc/systemd/system/painel-master*.service /etc/systemd/system/painel-demo*.service; do
        [ -f "$f" ] || continue
        local sn
        sn=$(basename "$f" .service)
        echo "$svcs" | grep -q "^$sn$" || svcs="$svcs $sn"
    done
    # Lista diretórios
    local dirs
    for d in /opt/painel-master /opt/painel-master-* /opt/painel-demo-*; do
        [ -d "$d" ] || continue
        dirs="$dirs $d"
    done

    if [ -z "$(echo $svcs | tr -d ' ')" ] && [ -z "$(echo $dirs | tr -d ' ')" ]; then
        warn "Nenhum painel instalado encontrado."
        exit 0
    fi

    echo ""
    echo -e "  ${BOLD}Instalações encontradas:${NC}"
    local idx=0
    local list_items=()
    for sn in $svcs; do
        [ -z "$sn" ] && continue
        local d="/opt/$sn"
        [ ! -d "$d" ] && d="$INSTALL_DIR"
        [ -d "$d" ] || continue
        idx=$((idx+1))
        list_items+=("$sn|$d")
        local status
        if systemctl is-active --quiet "$sn" 2>/dev/null; then status="${GREEN}ativo${NC}"; else status="${YELLOW}inativo${NC}"; fi
        echo -e "    [$idx] $sn ($d) - $status"
    done
    # se nenhum serviço mapeado mas tem diretório
    if [ $idx -eq 0 ]; then
        for d in $dirs; do
            [ -z "$d" ] && continue
            local sn2
            sn2=$(basename "$d")
            idx=$((idx+1))
            list_items+=("$sn2|$d")
            local status
            if systemctl is-active --quiet "$sn2" 2>/dev/null; then status="${GREEN}ativo${NC}"; else status="${YELLOW}inativo${NC}"; fi
            echo -e "    [$idx] $sn2 ($d) - $status"
        done
    fi

    echo ""
    read -rp "  Selecione qual remover [1-$idx] ou 0 para cancelar: " sel < /dev/tty
    [ "$sel" = "0" ] || [ -z "$sel" ] && { warn "Cancelado"; exit 0; }
    local item="${list_items[$((sel-1))]}"
    local r_svc
    local r_dir
    r_svc=$(echo "$item" | cut -d'|' -f1)
    r_dir=$(echo "$item" | cut -d'|' -f2)
    [ -z "$r_svc" ] && error "Seleção inválida"
    [ -z "$r_dir" ] && r_dir="/opt/$r_svc"

    echo ""
    warn "Você está prestes a REMOVER: $r_svc ($r_dir)"
    warn "Isso apagará o painel, vhosts nginx relacionados e o banco de dados."
    read -rp "  Digite CONFIRMAR para continuar: " conf < /dev/tty
    [ "$conf" != "CONFIRMAR" ] && { warn "Cancelado"; exit 0; }

    banner "Removendo $r_svc..."
    systemctl stop "$r_svc" 2>/dev/null || true
    systemctl disable "$r_svc" 2>/dev/null || true
    rm -f "/etc/systemd/system/${r_svc}.service"
    # Se era um demo, remove tambem o timer/script do reset
    if [[ "$r_svc" == painel-demo* ]]; then
        systemctl disable --now "${r_svc}-reset.timer" 2>/dev/null || true
        rm -f "/etc/systemd/system/${r_svc}-reset.timer" "/etc/systemd/system/${r_svc}-reset.service"
    fi
    systemctl daemon-reload 2>/dev/null

    pkill -9 -f "${r_dir}/app\.py" 2>/dev/null || true
    sleep 2

    # Remove vhosts nginx relacionados ao diretório/serviço
    for vf in /etc/nginx/sites-available/${r_svc}* /etc/nginx/sites-enabled/${r_svc}*; do
        [ -e "$vf" ] || continue
        rm -f "$vf"
    done
    # Tenta remover vhosts com referência ao diretório
    [ -d /etc/nginx/sites-available ] && grep -l "$r_dir" /etc/nginx/sites-available/* 2>/dev/null | xargs -r rm -f
    [ -d /etc/nginx/sites-enabled ] && grep -l "$r_dir" /etc/nginx/sites-enabled/* 2>/dev/null | xargs -r rm -f

    if [ -d "$r_dir" ]; then
        if [ -f "$r_dir/painel.db" ]; then
            local bkp="/tmp/${r_svc}_backup_$(date +%s).db"
            cp "$r_dir/painel.db" "$bkp"
            warn "Backup do banco salvo em: $bkp"
        fi
        rm -rf "$r_dir"
        log "Diretório removido: $r_dir"
    fi

    nginx -t && systemctl reload nginx 2>/dev/null || true
    log "Remoção concluída!"
    exit 0
}

# ── Desativar/remover painel DEMO ────────────────────────────────────────────
disable_demo() {
    banner "Desativar painel DEMO"

    local demos
    demos=""
    for d in /opt/painel-demo-*; do
        [ -d "$d" ] && demos="$demos $d"
    done
    # Servicos demo sem diretorio
    for f in /etc/systemd/system/painel-demo*.service; do
        [ -f "$f" ] || continue
        local sn
        sn=$(basename "$f" .service)
        [[ "$sn" == *-reset ]] && continue
        local dd="/opt/$sn"
        [ -d "$dd" ] || demos="$demos $dd"
    done

    [ -z "$(echo $demos | tr -d ' ')" ] && { warn "Nenhum painel DEMO instalado."; exit 0; }

    echo ""
    echo -e "  ${BOLD}Demos instalados:${NC}"
    echo "$demos" | sed 's/^ *//' | nl
    echo ""
    warn "Isto remove o demo por completo: servico, timer de reset, banco e vhost."
    read -rp "  Digite CONFIRMAR para desativar/limpar TODOS os demos acima: " conf < /dev/tty
    [ "$conf" != "CONFIRMAR" ] && { warn "Cancelado"; exit 0; }

    local d
    for d in $demos; do
        local svc
        svc=$(basename "$d")
        systemctl disable --now "${svc}-reset.timer" 2>/dev/null || true
        systemctl stop "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
        rm -f "/etc/systemd/system/${svc}.service" \
              "/etc/systemd/system/${svc}-reset.service" \
              "/etc/systemd/system/${svc}-reset.timer"
        rm -f /usr/local/sbin/painel-demo-reset
        rm -f /etc/nginx/sites-available/${svc}* /etc/nginx/sites-enabled/${svc}*
        [ -d /etc/nginx/sites-available ] && grep -l "$d" /etc/nginx/sites-available/* 2>/dev/null | xargs -r rm -f
        [ -d /etc/nginx/sites-enabled ] && grep -l "$d" /etc/nginx/sites-enabled/* 2>/dev/null | xargs -r rm -f
        rm -rf "$d"
        log "Demo removido: $d"
    done
    systemctl daemon-reload 2>/dev/null
    nginx -t 2>/dev/null && systemctl reload nginx 2>/dev/null || true
    log "Demos desativados por completo!"
    exit 0
}

# ── Instalar painel adicional (mesma VPS, outro domínio/porta) ────────────────
install_additional() {
    local is_demo=0
    [ "$1" = "demo" ] && is_demo=1
    if [ "$is_demo" = 1 ]; then
        banner "Instalar versao DEMO (reseta sozinha a cada 20 min)"
    else
        banner "Instalar PAINEL ADICIONAL na mesma VPS"
    fi

    if [ -z "$DEMO_DOMAIN" ]; then
        read -rp "  Informe o DOMÍNIO ou SUBDOMÍNIO para este painel (ex: painel2.seudominio.com): " ADD_DOMAIN < /dev/tty
        ADD_DOMAIN=$(echo "$ADD_DOMAIN" | tr -d ' ' | tr '[:upper:]' '[:lower:]')
        [ -z "$ADD_DOMAIN" ] && error "Domínio inválido"
    else
        ADD_DOMAIN=$(echo "$DEMO_DOMAIN" | tr -d ' ' | tr '[:upper:]' '[:lower:]')
        [ "$is_demo" = 1 ] || warn "DEMO_DOMAIN definido: instalando ${ADD_DOMAIN} sem perguntar"
    fi

    # Detecta porta livre
    local port
    for p in 2084 2085 2086 2087 2090 2091 2092 2093 2094 2095; do
        if ! ss -tlnp 2>/dev/null | grep -q ":$p " && ! lsof -i :$p 2>/dev/null | grep -q LISTEN; then
            port=$p; break
        fi
    done
    [ -z "$port" ] && port=2084
    if [ -z "$DEMO_DOMAIN" ]; then
        read -rp "  Porta da aplicação [$port]: " ADD_PORT < /dev/tty
    fi
    ADD_PORT="${ADD_PORT:-$port}"
    ADD_PORT=$(echo "$ADD_PORT" | tr -d ' ')
    # Garante porta livre
    if ss -tlnp 2>/dev/null | grep -q ":$ADD_PORT " || lsof -i :$ADD_PORT 2>/dev/null | grep -q LISTEN; then
        error "Porta $ADD_PORT já está em uso"
    fi

    local add_prefix="painel-master"
    [ "$is_demo" = 1 ] && add_prefix="painel-demo"
    local add_name="${add_prefix}-$(echo $ADD_DOMAIN | tr '.' '-' | cut -c1-15)"
    # Garante nome único
    if [ -d "/opt/$add_name" ] || systemctl list-unit-files 2>/dev/null | grep -q "^${add_name}\.service"; then
        add_name="${add_prefix}-$(date +%s | tail -c 5)"
    fi
    local ADD_DIR="/opt/$add_name"
    local ADD_SVC="$add_name"

    info "Instalando em: $ADD_DIR"
    info "Serviço: $ADD_SVC"
    info "Porta: $ADD_PORT"
    info "Domínio: $ADD_DOMAIN"

    echo ""
    warn "IMPORTANTE: O domínio $ADD_DOMAIN deve estar apontado para o IP desta VPS (A/AAAA)."
    warn "Quando acessado por este domínio, o Nginx irá direcionar APENAS para este painel."
    if [ -z "$DEMO_DOMAIN" ]; then
        read -rp "  Continuar? [s/N]: " cont < /dev/tty
        [[ "$cont" =~ ^[sS]$ ]] || { warn "Cancelado"; exit 0; }
    fi

    check_python
    install_system_deps

    # Clone/baixa código
    banner "Baixando código..."
    if [ -d "$ADD_DIR/.git" ]; then
        rm -rf "$ADD_DIR"
    fi
    mkdir -p "$ADD_DIR"
    cd "$ADD_DIR" || error "Falha ao acessar $ADD_DIR"

    if git_auth clone --depth=1 "$REPO_URL" "$ADD_DIR" 2>/dev/null; then
        log "Clonado via git"
    else
        # Fallback: baixa via instalador servido pelo painel (funciona com repo privado)
        local tmpzip
        tmpzip="/tmp/${add_name}_src.tar.gz"
        if curl -sL "https://raw.githubusercontent.com/jeanfraga95/painel101/main/README.md" >/dev/null 2>&1; then
            git_auth clone --depth=1 "$REPO_URL" /tmp/${add_name}_src 2>/dev/null && mv /tmp/${add_name}_src/.git "$ADD_DIR/" && (cd "$ADD_DIR" && git_auth reset --hard origin/main) && rm -rf /tmp/${add_name}_src
        else
            curl -sSL "https://${ADD_DOMAIN}/install.sh" -o /dev/null 2>&1 || true
            git_auth clone --depth=1 "$REPO_URL" /tmp/${add_name}_src 2>&1 | tail -2
            [ -d /tmp/${add_name}_src ] && { cp -r /tmp/${add_name}_src/* "$ADD_DIR/"; cp -r /tmp/${add_name}_src/.* "$ADD_DIR/" 2>/dev/null; rm -rf /tmp/${add_name}_src; }
        fi
    fi

    # Venv
    banner "Criando ambiente virtual..."
    "$PYTHON_CMD" -m venv "$ADD_DIR/venv"
    "$ADD_DIR/venv/bin/pip" install --upgrade pip -q
    "$ADD_DIR/venv/bin/pip" install -r "$ADD_DIR/requirements.txt" -q
    "$ADD_DIR/venv/bin/pip" install gunicorn -q 2>/dev/null || true

    # Inicializa banco e chave
    banner "Inicializando banco de dados..."
    if [ -f "/opt/painel-master/.secret_key" ] && [ ! -f "$ADD_DIR/.secret_key" ]; then
        cp "/opt/painel-master/.secret_key" "$ADD_DIR/.secret_key"
        chmod 600 "$ADD_DIR/.secret_key"
    fi
    if [ ! -f "$ADD_DIR/.secret_key" ]; then
        python3 -c "import os; open('$ADD_DIR/.secret_key','wb').write(os.urandom(32))" 2>/dev/null || head -c 32 /dev/urandom > "$ADD_DIR/.secret_key"
        chmod 600 "$ADD_DIR/.secret_key"
    fi
    if [ ! -f "$ADD_DIR/painel.db" ]; then
        (cd "$ADD_DIR" && "$ADD_DIR/venv/bin/python" -c "import db; db.init_db()" 2>&1 | tail -5) || true
    fi
    chown -R root:root "$ADD_DIR" 2>/dev/null || true

    # Serviço systemd separado
    banner "Configurando serviço..."
    cat > "/etc/systemd/system/${ADD_SVC}.service" << EOF
[Unit]
Description=$([ "$is_demo" = 1 ] && echo "Painel DEMO (${ADD_DOMAIN}) — reset 20min" || echo "Painel Master (${ADD_DOMAIN})")
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${ADD_DIR}
Environment="PATH=${ADD_DIR}/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PORT=${ADD_PORT}"
Environment="DOMAIN=${ADD_DOMAIN}"
$([ "$is_demo" = 1 ] && echo 'Environment="PANEL_DEMO=1"')
ExecStart=${ADD_DIR}/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$ADD_SVC"
    sleep 5
    systemctl status "$ADD_SVC" --no-pager 2>&1 | tail -15

    if [ "$is_demo" = 1 ]; then
        banner "Configurando RESET automatico (a cada 20 minutos)..."
        cat > /usr/local/sbin/painel-demo-reset << 'RSTEOF'
#!/bin/bash
# Reset total do painel demo: apaga banco + chave e deixa o app recriar
# tudo do zero (admin/admin123, tabelas novas). Gerado pelo instalador.
DIR="$1"; SVC="$2"
systemctl stop "$SVC" 2>/dev/null
rm -f "$DIR/painel.db" "$DIR/painel.db-journal" "$DIR/.secret_key"
systemctl start "$SVC" 2>/dev/null
RSTEOF
        chmod 700 /usr/local/sbin/painel-demo-reset
        cat > "/etc/systemd/system/${ADD_SVC}-reset.service" << EOF
[Unit]
Description=Reset total do painel demo (${ADD_DOMAIN})
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/painel-demo-reset ${ADD_DIR} ${ADD_SVC}
EOF
        cat > "/etc/systemd/system/${ADD_SVC}-reset.timer" << EOF
[Unit]
Description=Timer de reset do painel demo (${ADD_DOMAIN})
[Timer]
OnCalendar=*:0/20
AccuracySec=30s
Persistent=true
Unit=${ADD_SVC}-reset.service
[Install]
WantedBy=timers.target
EOF
        systemctl daemon-reload
        systemctl enable --now "${ADD_SVC}-reset.timer"
        log "Reset automatico ativo: banco zerado a cada 20 minutos"
    fi

    # Cert snakeoil: base de 443 (Cloudflare modo Full SSL usa este caminho)
    if [ ! -f /etc/ssl/certs/ssl-cert-snakeoil.pem ]; then
        command -v make-ssl-cert >/dev/null 2>&1 || apt-get install -y ssl-cert 2>/dev/null
        make-ssl-cert generate-default-snakeoil 2>/dev/null || true
    fi

    # Nginx vhost APENAS para este domínio apontando para esta porta
    banner "Configurando Nginx para ${ADD_DOMAIN}..."
    mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets /etc/nginx/conf.d
    # Garante snippets básicos
    [ -f /etc/nginx/snippets/painel-security.conf ] || cat > /etc/nginx/snippets/painel-security.conf << 'SEOF'
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options SAMEORIGIN always;
add_header Referrer-Policy strict-origin-when-cross-origin always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
client_max_body_size 110M;
SEOF
    [ -f /etc/nginx/snippets/proxy_params.conf ] || cat > /etc/nginx/snippets/proxy_params.conf << 'PEOF'
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
PEOF
    [ -f /etc/nginx/conf.d/painel-scheme.conf ] || echo 'map $http_x_forwarded_proto $painel_proto { default $scheme; https https; }' > /etc/nginx/conf.d/painel-scheme.conf
    [ -f /etc/nginx/snippets/vnc-location.conf ] || true
    [ -f /etc/nginx/conf.d/vnc-upstreams.conf ] || echo 'map $vnc_sid $vnc_backend { default "127.0.0.1:1"; }' > /etc/nginx/conf.d/vnc-upstreams.conf

    cat > "/etc/nginx/sites-available/${ADD_SVC}" << EOF
# Vhost exclusivo para ${ADD_DOMAIN} -> ${ADD_SVC} (porta ${ADD_PORT})
server {
    listen 80;
    server_name ${ADD_DOMAIN};
    include /etc/nginx/snippets/painel-security.conf;
    include /etc/nginx/snippets/vnc-location.conf;
    location / {
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_pass http://127.0.0.1:${ADD_PORT};
    }
}
server {
    listen 443 ssl;
    server_name ${ADD_DOMAIN};
    ssl_certificate /etc/ssl/certs/ssl-cert-snakeoil.pem;
    ssl_certificate_key /etc/ssl/private/ssl-cert-snakeoil.key;
    include /etc/nginx/snippets/painel-security.conf;
    include /etc/nginx/snippets/vnc-location.conf;
    add_header Strict-Transport-Security "max-age=31536000" always;
    location / {
        include /etc/nginx/snippets/proxy_params.conf;
        proxy_set_header X-Forwarded-Proto https;
        proxy_pass http://127.0.0.1:${ADD_PORT};
    }
}
EOF
    ln -sf "/etc/nginx/sites-available/${ADD_SVC}" "/etc/nginx/sites-enabled/${ADD_SVC}"
    nginx -t && systemctl reload nginx
    sleep 2
    systemctl restart "$ADD_SVC" 2>/dev/null || true
    sleep 3
    curl -sI "http://127.0.0.1:${ADD_PORT}/" 2>&1 | head -2

    echo ""
    echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}${BOLD}║     PAINEL ADICIONAL INSTALADO COM SUCESSO      ║${NC}"
    echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    if [ "$is_demo" = 1 ]; then
        echo -e "  ${YELLOW}VERSAO DEMO: tudo que for criado aqui e PERDIDO a cada 20 minutos${NC}"
        echo -e "  ${YELLOW}Banco, usuarios, servidores e senhas zerados no reset automatico.${NC}"
        echo -e "  Reset manual:  systemctl start ${ADD_SVC}-reset.service"
        echo -e "  Desativar:     systemctl disable --now ${ADD_SVC}-reset.timer"
    fi
    echo -e "  Domínio:        ${BOLD}http://${ADD_DOMAIN}${NC} / ${BOLD}https://${ADD_DOMAIN}${NC}"
    echo -e "  Porta interna:  ${ADD_PORT}"
    echo -e "  Diretório:      ${ADD_DIR}"
    echo -e "  Serviço:        ${ADD_SVC}"
    echo -e "  Login padrão:   admin / admin123"
    echo ""
    echo -e "  ${YELLOW}Acesse pelo DOMÍNIO informado para usar este painel.${NC}"
    echo -e "  ${YELLOW}Troque a senha do admin após o primeiro login.${NC}"
    echo ""
    echo -e "  ${BOLD}HTTPS com Cloudflare (proxy laranja ativo):${NC}"
    echo -e "   - SSL/TLS: modo ${BOLD}Flexible${NC} (Cloudflare->origem HTTP/80) ou"
    echo -e "     ${BOLD}Full${NC} (origem HTTPS/443 com cert autoassinado) - ambos funcionam."
    echo -e "   - A entry aponta o A/AAAA de ${ADD_DOMAIN} para o IP desta VPS."
    echo ""
    exit 0
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
MODE="${1:-install}"   # install (default) | update | demo (exige DEMO_DOMAIN)

# Menu interativo quando rodado sem argumento. Le do /dev/tty para tambem
# aparecer quando o instalador roda via "curl ... | bash" (stdin e o pipe).
if [ "$#" -eq 0 ] && [ -e /dev/tty ]; then
    echo -e "  ${BOLD}Selecione uma opção:${NC}"
    echo ""
    echo -e "    [1] Instalar painel (padrão)"
    echo -e "    [2] Atualizar painel existente"
    echo -e "    [3] Remover painel instalado"
    echo -e "    [4] Instalar painel ADICIONAL (mesma VPS, outro domínio/porta)"
    echo -e "    [5] Instalar versão DEMO (banco resetado a cada 20 minutos)"
    echo -e "    [6] Desativar/limpar painel DEMO"
    echo ""
    read -rp "  Opção [1]: " MENU_OPT < /dev/tty
    MENU_OPT="${MENU_OPT:-1}"
    case "$MENU_OPT" in
        2) MODE="update" ;;
        3) remove_panel ;;
        4) install_additional ;;
        5) install_additional demo ;;
        6) disable_demo ;;
        *) MODE="install" ;;
    esac
    echo ""
fi

check_root
detect_arch
detect_os

if [ "$MODE" = "demo" ]; then
    # ── Instalacao DEMO nao-interativa: DEMO_DOMAIN=dom bash install.sh demo ──
    [ -z "$DEMO_DOMAIN" ] && error "Modo demo nao-interativo exige: DEMO_DOMAIN=demo.seudominio.com bash install.sh demo"
    install_additional demo
fi

if [ "$MODE" = "update" ]; then
    # ── Modo atualização: só sincroniza código e reinicia, sem apagar banco ──
    banner "Modo ATUALIZAÇÃO — sincronizando código do GitHub..."
    check_python
    free_ports

    # Salva banco e chave de cifragem antes de qualquer operação
    if [ -f "$INSTALL_DIR/painel.db" ]; then
        DB_BACKUP="/tmp/painel_backup_$(date +%s).db"
        cp "$INSTALL_DIR/painel.db" "$DB_BACKUP"
        warn "Banco salvo em: $DB_BACKUP"
    fi
    # A chave Fernet das senhas SSH e irrecuperavel: se ela se perder, todas
    # as senhas de usuario SSH ficam indecifraveis. Ela esta no .gitignore
    # (o git clean nao apaga), mas fica um backup por seguranca.
    KEY_BACKUP=""
    if [ -f "$INSTALL_DIR/.secret_key" ]; then
        KEY_BACKUP="/tmp/painel_key_$(date +%s).key"
        cp "$INSTALL_DIR/.secret_key" "$KEY_BACKUP"
        chmod 600 "$KEY_BACKUP"
    fi

    systemctl stop "$SERVICE_NAME" 2>/dev/null || true

    # Força alinhamento com GitHub (usa a mesma lógica de copy_files)
    copy_files

    # Restaura banco
    if [ -n "$DB_BACKUP" ] && [ -f "$DB_BACKUP" ]; then
        cp "$DB_BACKUP" "$INSTALL_DIR/painel.db"
        log "Banco restaurado"
    fi
    # Garante que a chave de cifragem continua a mesma
    if [ -n "$KEY_BACKUP" ] && [ -f "$KEY_BACKUP" ] && [ ! -f "$INSTALL_DIR/.secret_key" ]; then
        cp "$KEY_BACKUP" "$INSTALL_DIR/.secret_key"
        chmod 600 "$INSTALL_DIR/.secret_key"
        warn "Chave de cifragem restaurada do backup"
    fi

    # Atualiza dependências Python sem recriar o venv
    if [ -f "$INSTALL_DIR/venv/bin/pip" ]; then
        info "Atualizando dependencias Python..."
        "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q 2>/dev/null || true
    fi

    # Reaplica o hardening do nginx (HTTPS real atras do Cloudflare, CSP,
    # rate limit, HSTS). Sem isso o cliente ficaria com o nginx antigo:
    # o codigo do app atualiza, mas a borda continua sem cookie Secure.
    apply_nginx_hardening

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
    disable_https_redirect_if_no_domain
    print_info
fi
