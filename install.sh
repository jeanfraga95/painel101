#!/bin/bash
# =============================================================
#  Painel Master — Instalador
#  Repositório: https://github.com/jeanfraga95/painel101
#
#  Uso:
#    bash install.sh            # instala / reinstala
#    bash install.sh update     # só atualiza o código do GitHub
# =============================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

REPO_URL="https://github.com/jeanfraga95/painel101"
INSTALL_DIR="/opt/painel-master"
SERVICE_NAME="painel-master"
PANEL_PORT=2083

log()    { echo -e "${GREEN}[✔]${NC} $1"; }
warn()   { echo -e "${YELLOW}[!]${NC} $1"; }
error()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info()   { echo -e "${BLUE}[i]${NC} $1"; }
banner() { echo -e "${CYAN}${BOLD}$1${NC}"; }

# ── Root check ─────────────────────────────────────────────────────────────────
check_root() { [ "$EUID" -eq 0 ] || error "Execute como root: sudo bash install.sh"; }

# ── Detect OS ──────────────────────────────────────────────────────────────────
detect_os() {
    [ -f /etc/os-release ] && . /etc/os-release || true
    OS_ID="${ID:-unknown}"
    info "Sistema: ${OS_ID} ${VERSION_ID:-}"
}

# ── Get PID listening on a port (compatível com awk POSIX/gawk/mawk) ───────────
# Evita match(str, regex, array) que não existe no mawk (Debian/Ubuntu padrão)
pid_on_port() {
    local port="$1"
    ss -tlnp 2>/dev/null \
        | grep ":${port} " \
        | grep -oP 'pid=\K[0-9]+' \
        | head -1
}

# ── Stop whatever is using port 80 (apache, lighttpd, old nginx, etc.) ─────────
stop_conflicting_webservers() {
    info "Liberando porta 80..."
    for svc in apache2 apache httpd lighttpd h2o caddy; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            warn "Parando $svc (ocupa porta 80)..."
            systemctl stop    "$svc" 2>/dev/null || true
            systemctl disable "$svc" 2>/dev/null || true
        fi
    done

    local nginx_pid
    nginx_pid=$(systemctl show -p MainPID nginx 2>/dev/null | cut -d= -f2)
    local pid80
    pid80=$(pid_on_port 80)
    if [ -n "$pid80" ] && [ "$pid80" != "$nginx_pid" ] && [ "$pid80" != "0" ]; then
        warn "Processo $pid80 na porta 80 — encerrando..."
        kill -9 "$pid80" 2>/dev/null || true
        sleep 1
    fi
    log "Porta 80 livre"
}

# ── Free panel port if something else is using it ──────────────────────────────
free_panel_port() {
    local pid
    pid=$(pid_on_port "$PANEL_PORT")
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
        warn "Porta ${PANEL_PORT} em uso pelo PID $pid — encerrando..."
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi
}

# ── Remove previous installation ───────────────────────────────────────────────
uninstall_existing() {
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null || \
       systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "Removendo instalação anterior..."
        systemctl stop    "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    fi
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload 2>/dev/null || true
}

# ── System dependencies ────────────────────────────────────────────────────────
install_system_deps() {
    banner "Instalando dependências..."
    case "${OS_ID}" in
        ubuntu|debian|linuxmint|raspbian)
            apt-get update -qq 2>/dev/null
            DEBIAN_FRONTEND=noninteractive apt-get install -y \
                curl wget git build-essential libssl-dev libffi-dev \
                python3 python3-venv python3-dev python3-pip \
                sqlite3 openssl nginx iptables 2>/dev/null || true
            ;;
        fedora|centos|rhel|rocky|almalinux)
            dnf install -y curl wget git gcc openssl-devel libffi-devel \
                python3 python3-pip sqlite openssl nginx iptables 2>/dev/null || true
            ;;
        *)
            warn "Distro não reconhecida — tentando apt-get..."
            apt-get install -y python3 python3-venv python3-pip nginx iptables 2>/dev/null || true
            ;;
    esac
    log "Dependências OK"
}

# ── Find python3 with sqlite3 ──────────────────────────────────────────────────
check_python() {
    PYTHON_CMD=""
    for cmd in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
        if command -v "$cmd" &>/dev/null && "$cmd" -c "import sqlite3" 2>/dev/null; then
            PYTHON_CMD="$cmd"; break
        fi
    done
    [ -n "$PYTHON_CMD" ] || error "Python3 com sqlite3 não encontrado."
    log "Python: $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"
}

# ── Clone / force-sync from GitHub ─────────────────────────────────────────────
copy_files() {
    info "Sincronizando código com GitHub..."
    if [ -d "${INSTALL_DIR}/.git" ]; then
        cd "$INSTALL_DIR"
        git remote set-url origin "$REPO_URL" 2>/dev/null || \
            git remote add origin "$REPO_URL" 2>/dev/null || true
        git fetch --all --prune 2>/dev/null || true
        git reset --hard origin/main 2>/dev/null || \
            git reset --hard origin/master 2>/dev/null || \
            git reset --hard FETCH_HEAD 2>/dev/null || true
        git clean -fd 2>/dev/null || true
        log "Código atualizado do GitHub"
    else
        [ -d "$INSTALL_DIR" ] && rm -rf "$INSTALL_DIR"
        info "Clonando repositório..."
        git clone --depth=1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null || \
            git clone "$REPO_URL" "$INSTALL_DIR" 2>/dev/null || \
            error "Falha ao clonar. Verifique a conexão com a internet."
        log "Repositório clonado"
    fi
}

# ── Python virtualenv + deps ───────────────────────────────────────────────────
install_python_deps() {
    cd "$INSTALL_DIR"
    info "Criando virtualenv..."
    $PYTHON_CMD -m venv venv 2>/dev/null || error "Falha ao criar virtualenv"

    source venv/bin/activate
    pip install --upgrade pip setuptools wheel -q 2>/dev/null

    info "Instalando pacotes Python..."
    if ! pip install -r requirements.txt -q 2>/dev/null; then
        warn "Instalação em lote falhou — instalando um por um..."
        while IFS= read -r pkg; do
            [[ -z "$pkg" || "$pkg" =~ ^# ]] && continue
            pip install "${pkg}" -q 2>/dev/null && \
                info "  OK: ${pkg%%[><=!]*}" || \
                warn "  FALHOU: ${pkg%%[><=!]*}"
        done < requirements.txt
    fi
    deactivate
    log "Pacotes Python instalados"
}

# ── Configure nginx ─────────────────────────────────────────────────────────────
configure_nginx() {
    local domain="$1"

    mkdir -p /etc/nginx/snippets /var/www/letsencrypt/.well-known/acme-challenge

    cat > /etc/nginx/snippets/proxy_params.conf << 'SNIPPET'
proxy_http_version 1.1;
proxy_set_header   Host              $http_host;
proxy_set_header   X-Real-IP         $remote_addr;
proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header   X-Forwarded-Proto $scheme;
proxy_set_header   Upgrade           $http_upgrade;
proxy_set_header   Connection        "upgrade";
proxy_read_timeout 120s;
client_max_body_size 110M;
SNIPPET

    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

    if [ ! -f /etc/nginx/ssl/pmg-selfsigned.crt ]; then
        mkdir -p /etc/nginx/ssl
        openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
            -keyout /etc/nginx/ssl/pmg-selfsigned.key \
            -out    /etc/nginx/ssl/pmg-selfsigned.crt \
            -subj "/CN=painel-master/O=PMG/C=BR" 2>/dev/null
    fi

    local server_name="_"
    [ -n "$domain" ] && server_name="${domain} www.${domain} _"

    # ATENÇÃO: heredoc sem aspas no delimitador para expandir variáveis corretamente
    cat > /etc/nginx/sites-available/painel-master << NGCONF
# Painel Master — gerado automaticamente

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${server_name};

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
    real_ip_header CF-Connecting-IP;

    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
    }

    location / {
        proxy_pass         http://127.0.0.1:${PANEL_PORT};
        include            /etc/nginx/snippets/proxy_params.conf;
    }
}

server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name ${server_name};

    ssl_certificate     /etc/nginx/ssl/pmg-selfsigned.crt;
    ssl_certificate_key /etc/nginx/ssl/pmg-selfsigned.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    set_real_ip_from 103.21.244.0/22;
    set_real_ip_from 104.16.0.0/13;
    set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 162.158.0.0/15;
    set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 173.245.48.0/20;
    real_ip_header CF-Connecting-IP;

    location / {
        proxy_pass         http://127.0.0.1:${PANEL_PORT};
        include            /etc/nginx/snippets/proxy_params.conf;
        proxy_set_header   X-Forwarded-Proto https;
    }
}
NGCONF

    ln -sf /etc/nginx/sites-available/painel-master \
           /etc/nginx/sites-enabled/painel-master 2>/dev/null || true

    systemctl enable nginx 2>/dev/null || true
    if nginx -t 2>/dev/null; then
        systemctl is-active --quiet nginx 2>/dev/null && \
            systemctl reload nginx || systemctl start nginx
        log "nginx configurado e rodando"
    else
        warn "nginx -t falhou:"
        nginx -t 2>&1
    fi
}

# ── Systemd service ─────────────────────────────────────────────────────────────
configure_service() {
    local secret_key
    secret_key=$(openssl rand -hex 32)

    # NOTA: StartLimitIntervalSec e StartLimitBurst pertencem à seção [Unit] no
    # systemd < 230 (Debian 9/Ubuntu 16). Movidos para [Unit] para compatibilidade.
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Painel Master
After=network.target network-online.target nginx.service
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="PORT=${PANEL_PORT}"
Environment="SECRET_KEY=${secret_key}"

ExecStartPre=/sbin/iptables -F
ExecStartPre=/sbin/iptables -t nat -F
ExecStartPre=/sbin/iptables -t mangle -F

ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/app.py
Restart=always
RestartSec=5
TimeoutStartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    log "Serviço configurado (auto-start no boot)"
}

# ── Set admin password directly in the database ────────────────────────────────
# Não depende de db.py para o hash — gera o SHA-256 e faz UPDATE direto no SQLite.
# Isso evita falha silenciosa caso db.py ou migrate_schema() tenha erro.
set_admin_password() {
    local plain_pass="$1"

    info "Inicializando banco e configurando senha admin..."

    # 1) Inicializa o banco via app/db (cria tabelas se não existirem)
    "${INSTALL_DIR}/venv/bin/python" << 'PYINIT' 2>/dev/null
import sys
sys.path.insert(0, '/opt/painel-master')
try:
    import db
    db.init_db()
    for fn in ('migrate_schema', 'migrate_schema_v2', 'migrate_schema_v3'):
        try:
            getattr(db, fn)()
        except Exception:
            pass
    print("DB inicializado")
except Exception as e:
    print(f"Aviso DB init: {e}")
PYINIT

    # 2) Gera hash e aplica — variáveis do shell expandidas aqui (sem aspas no heredoc)
    local pw_hash
    pw_hash=$("${INSTALL_DIR}/venv/bin/python" -c \
        "import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" \
        "${plain_pass}" 2>/dev/null)

    if [ -z "$pw_hash" ]; then
        warn "Não foi possível gerar hash da senha — usando sqlite3 direto"
        pw_hash=$(echo -n "${plain_pass}" | openssl dgst -sha256 | awk '{print $2}')
    fi

    if [ -n "$pw_hash" ]; then
        # UPDATE direto no SQLite — não depende de nenhum import do app
        sqlite3 "${INSTALL_DIR}/painel.db" \
            "UPDATE panel_users SET password_hash='${pw_hash}' WHERE username='admin';" \
            2>/dev/null && log "Senha admin definida no banco" || \
            warn "sqlite3 UPDATE falhou — verifique manualmente"

        # Confirma linha afetada
        local rows
        rows=$(sqlite3 "${INSTALL_DIR}/painel.db" \
            "SELECT COUNT(*) FROM panel_users WHERE username='admin' AND password_hash='${pw_hash}';" \
            2>/dev/null || echo "0")
        if [ "$rows" = "1" ]; then
            log "Senha admin confirmada no banco ✔"
        else
            warn "Linha admin não encontrada após UPDATE — o app pode criar na 1ª inicialização"
        fi
    else
        warn "Hash vazio — senha admin pode não ter sido definida"
    fi
}

# ── Start service and show logs ─────────────────────────────────────────────────
start_service() {
    info "Iniciando serviço..."
    systemctl start "$SERVICE_NAME"

    local started=0
    for i in $(seq 1 20); do
        sleep 1
        if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
            log "Serviço ativo após ${i}s"
            started=1
            break
        fi
        printf "."
    done
    echo ""

    echo ""
    banner "── Logs de inicialização ──────────────────────────────────"
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null || true
    echo ""

    if [ "$started" -eq 0 ]; then
        warn "Serviço não respondeu em 20s."
        warn "Verifique: journalctl -u ${SERVICE_NAME} -f"
    fi
}

# ── Firewall ────────────────────────────────────────────────────────────────────
open_firewall() {
    iptables -F 2>/dev/null || true
    iptables -t nat -F 2>/dev/null || true

    for port in 80 443 "${PANEL_PORT}"; do
        iptables -I INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || true
    done

    command -v ufw &>/dev/null && {
        ufw allow 80/tcp   2>/dev/null || true
        ufw allow 443/tcp  2>/dev/null || true
        ufw allow "${PANEL_PORT}/tcp" 2>/dev/null || true
    }
    command -v firewall-cmd &>/dev/null && {
        firewall-cmd --permanent --add-port=80/tcp        2>/dev/null || true
        firewall-cmd --permanent --add-port=443/tcp       2>/dev/null || true
        firewall-cmd --permanent --add-port=${PANEL_PORT}/tcp 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
    }
    log "Firewall configurado"
}

# ── nginx watchdog ─────────────────────────────────────────────────────────────
setup_watchdog() {
    cat > /etc/systemd/system/pmg-watchdog.service << 'WD'
[Unit]
Description=Painel Master — watchdog
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c '\
    systemctl is-active nginx         2>/dev/null || systemctl start nginx         2>/dev/null; \
    systemctl is-active painel-master 2>/dev/null || systemctl start painel-master 2>/dev/null; \
    iptables -F 2>/dev/null || true'
WD

    cat > /etc/systemd/system/pmg-watchdog.timer << 'WDT'
[Unit]
Description=Painel Master — watchdog timer (a cada 5 minutos)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=pmg-watchdog.service

[Install]
WantedBy=timers.target
WDT

    systemctl daemon-reload
    systemctl enable --now pmg-watchdog.timer 2>/dev/null || true
    log "Watchdog ativo (verifica a cada 5 min + 2min após boot)"
}

# ── Print final info ────────────────────────────────────────────────────────────
print_info() {
    local domain="${1}"
    local admin_pass="${2}"
    local SERVER_IP
    SERVER_IP=$(curl -s --max-time 5 https://ipv4.icanhazip.com 2>/dev/null || \
                curl -s --max-time 5 https://api.ipify.org      2>/dev/null || \
                hostname -I 2>/dev/null | awk '{print $1}')

    local svc_status
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        svc_status="${GREEN}✔ Rodando${NC}"
    else
        svc_status="${RED}✗ Erro — veja os logs acima${NC}"
    fi

    local nginx_status
    if systemctl is-active --quiet nginx 2>/dev/null; then
        nginx_status="${GREEN}✔ Rodando${NC}"
    else
        nginx_status="${RED}✗ Parado${NC}"
    fi

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║          PAINEL MASTER — INSTALAÇÃO CONCLUÍDA        ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Serviço:   $(echo -e "$svc_status")"
    echo -e "  nginx:     $(echo -e "$nginx_status")"
    echo ""
    echo -e "  ${BOLD}Modos de acesso:${NC}"
    echo -e "    Direto (IP + porta): ${BOLD}http://${SERVER_IP}:${PANEL_PORT}${NC}"
    echo -e "    nginx  HTTP:         ${BOLD}http://${SERVER_IP}${NC}"
    echo -e "    nginx  HTTPS:        ${BOLD}https://${SERVER_IP}${NC}  (cert autoassinado)"
    if [ -n "$domain" ]; then
        echo -e "    Domínio:             ${BOLD}https://${domain}${NC}  (via Cloudflare)"
    fi
    echo ""
    echo -e "  Login padrão:  ${BOLD}admin / ${admin_pass}${NC}"
    echo -e "  Diretório:     ${INSTALL_DIR}"
    echo ""
    if [ -n "$domain" ]; then
        echo -e "  ${BOLD}Para o domínio funcionar:${NC}"
        echo -e "  ┌──────────────────────────────────────────────────────────┐"
        echo -e "  │  1) No DNS: crie registro A  →  ${domain} = ${SERVER_IP}"
        echo -e "  │  2) Cloudflare: ative o proxy (nuvem 🟠 laranja)         │"
        echo -e "  │     A Cloudflare entrega HTTPS automaticamente           │"
        echo -e "  │  3) Aguarde propagação DNS (geralmente < 5 min)          │"
        echo -e "  └──────────────────────────────────────────────────────────┘"
        echo ""
    fi
    echo -e "  ${YELLOW}Comandos úteis:${NC}"
    echo -e "    journalctl -u ${SERVICE_NAME} -f          # logs ao vivo"
    echo -e "    journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    echo -e "    systemctl restart ${SERVICE_NAME}         # reiniciar"
    echo -e "    systemctl status  ${SERVICE_NAME}         # status"
    echo -e "    nginx -t && systemctl reload nginx         # recarregar nginx"
    echo -e "    bash install.sh update                     # atualizar do GitHub"
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

check_root
detect_os

MODE="${1:-install}"

# ── Update mode ────────────────────────────────────────────────────────────────
if [ "$MODE" = "update" ]; then
    banner "Modo ATUALIZAÇÃO — sincronizando código do GitHub..."
    check_python

    # Backup db
    if [ -f "${INSTALL_DIR}/painel.db" ]; then
        cp "${INSTALL_DIR}/painel.db" "/tmp/pmg_backup_$(date +%s).db"
        info "Banco salvo em /tmp/"
    fi

    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    copy_files

    # Restaura db se foi apagado pelo git clean
    local_db=$(ls -t /tmp/pmg_backup_*.db 2>/dev/null | head -1)
    if [ -n "$local_db" ] && [ ! -f "${INSTALL_DIR}/painel.db" ]; then
        cp "$local_db" "${INSTALL_DIR}/painel.db"
    fi

    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q 2>/dev/null || true

    systemctl start "$SERVICE_NAME"
    sleep 3
    echo ""
    banner "── Logs pós-atualização ──────────────────────────────────"
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager 2>/dev/null || true
    echo ""
    systemctl is-active --quiet "$SERVICE_NAME" && \
        log "Atualizado e rodando!" || \
        warn "Verifique: journalctl -u ${SERVICE_NAME} -f"
    exit 0
fi

# ── Full install ───────────────────────────────────────────────────────────────
echo ""
read -rp "  Domínio para o painel (ex: painel.seusite.com.br) [Enter para pular]: " DOMAIN
DOMAIN="${DOMAIN// /}"

read -rp "  Senha do admin [padrão: admin123]: " ADMIN_PASS
ADMIN_PASS="${ADMIN_PASS:-admin123}"

echo ""

install_system_deps
check_python
stop_conflicting_webservers
free_panel_port
uninstall_existing
copy_files
install_python_deps

# Inicia o serviço uma vez para criar o banco antes de definir a senha
configure_service
configure_nginx "$DOMAIN"
open_firewall
setup_watchdog

# Sobe o app para criar/migrar o banco de dados
info "Iniciando serviço para criar o banco de dados..."
systemctl start "$SERVICE_NAME"
sleep 4
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
sleep 1

# Agora define a senha com o banco já criado
set_admin_password "$ADMIN_PASS"

# Sobe definitivamente
start_service
print_info "$DOMAIN" "$ADMIN_PASS"
