#!/bin/bash
# =============================================================
#  Painel Master — Instalador
#  Repositório: https://github.com/jeanfraga33/painel-master
# =============================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

INSTALL_DIR="/opt/painel-master"
SERVICE_NAME="painel-master"
PANEL_PORT="${PANEL_PORT:-5000}"
REPO_URL="https://github.com/jeanfraga33/painel-master"
PYTHON_MIN="3.8"

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
        aarch64|arm64) ARCH_LABEL="arm64" ;;
        armv7l)        ARCH_LABEL="armv7" ;;
        *)             warn "Arquitetura $ARCH pode não ser suportada" ;;
    esac
    info "Arquitetura: $ARCH_LABEL"
}

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID="$ID"
        OS_VER="$VERSION_ID"
    else
        OS_ID="unknown"
    fi
    info "Sistema: $OS_ID $OS_VER"
}

check_python() {
    banner "Verificando Python..."

    # Try python3 first
    for cmd in python3 python3.11 python3.10 python3.9 python3.8; do
        if command -v $cmd &>/dev/null; then
            VER=$($cmd -c "import sys; print('%d.%d' % sys.version_info[:2])")
            MAJOR=${VER%%.*}
            MINOR=${VER##*.}
            if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 8 ]; then
                PYTHON_CMD=$cmd
                log "Python $VER encontrado: $cmd"
                return
            fi
        fi
    done

    warn "Python 3.8+ não encontrado. Instalando..."
    install_python
}

install_python() {
    case "$OS_ID" in
        ubuntu|debian)
            apt-get update -qq
            apt-get install -y python3 python3-pip python3-venv python3-dev || {
                warn "Tentando instalar Python 3.10 via deadsnakes PPA..."
                apt-get install -y software-properties-common
                add-apt-repository -y ppa:deadsnakes/ppa
                apt-get update -qq
                apt-get install -y python3.10 python3.10-venv python3.10-dev
                update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1
            }
            ;;
        centos|rhel|fedora|rocky|almalinux)
            if command -v dnf &>/dev/null; then
                dnf install -y python3 python3-pip python3-devel
            else
                yum install -y python3 python3-pip python3-devel
            fi
            ;;
        *)
            error "Instale Python 3.8+ manualmente e execute o instalador novamente."
            ;;
    esac

    PYTHON_CMD=$(command -v python3)
    log "Python instalado: $($PYTHON_CMD --version)"
}

install_system_deps() {
    banner "Instalando dependências do sistema..."

    case "$OS_ID" in
        ubuntu|debian)
            apt-get update -qq
            apt-get install -y \
                curl wget git build-essential \
                libssl-dev libffi-dev \
                python3-pip python3-venv python3-dev \
                sqlite3 nginx supervisor \
                perl openssl jq \
                2>/dev/null || warn "Algumas dependências podem ter falhado"
            ;;
        centos|rhel|fedora|rocky|almalinux)
            if command -v dnf &>/dev/null; then
                dnf install -y curl wget git gcc openssl-devel \
                    libffi-devel python3-pip python3-devel \
                    sqlite nginx supervisor perl jq
            else
                yum install -y curl wget git gcc openssl-devel \
                    libffi-devel python3-pip python3-devel \
                    sqlite nginx supervisor perl jq
            fi
            ;;
        *)
            warn "OS não reconhecido. Instalando dependências básicas..."
            apt-get update -qq && apt-get install -y curl wget git python3-pip python3-venv sqlite3 perl openssl jq || true
            ;;
    esac
    log "Dependências do sistema instaladas"
}

install_panel() {
    banner "Instalando Painel Master..."

    # Clone or update repo
    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Atualizando repositório existente..."
        cd "$INSTALL_DIR"
        git pull origin main || warn "Falha ao atualizar repositório"
    else
        if [ -d "$INSTALL_DIR" ]; then
            warn "Diretório $INSTALL_DIR já existe. Fazendo backup..."
            mv "$INSTALL_DIR" "${INSTALL_DIR}.bak.$(date +%s)"
        fi
        git clone "$REPO_URL" "$INSTALL_DIR" || {
            warn "Falha ao clonar. Criando estrutura local..."
            mkdir -p "$INSTALL_DIR"
            cp -r . "$INSTALL_DIR/" 2>/dev/null || true
        }
    fi

    cd "$INSTALL_DIR"

    # Create virtualenv
    info "Criando ambiente virtual Python..."
    $PYTHON_CMD -m venv venv || error "Falha ao criar virtualenv"
    source venv/bin/activate

    # Upgrade pip
    pip install --upgrade pip setuptools wheel -q

    # Install requirements with fallback
    if [ -f requirements.txt ]; then
        pip install -r requirements.txt -q || {
            warn "Instalação em lote falhou. Instalando pacote por pacote..."
            while IFS= read -r pkg; do
                [[ "$pkg" =~ ^#|^$ ]] && continue
                pip install "$pkg" -q || warn "Falha ao instalar: $pkg"
            done < requirements.txt
        }
    fi

    deactivate
    log "Ambiente Python configurado"
}

configure_service() {
    banner "Configurando serviço systemd..."

    cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=Painel Master SSH Manager
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=PORT=${PANEL_PORT}
Environment=SECRET_KEY=$(openssl rand -hex 32)
ExecStart=${INSTALL_DIR}/venv/bin/python app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable ${SERVICE_NAME}
    systemctl restart ${SERVICE_NAME}

    sleep 2
    if systemctl is-active --quiet ${SERVICE_NAME}; then
        log "Serviço ${SERVICE_NAME} iniciado com sucesso"
    else
        warn "Serviço pode não ter iniciado. Verificando logs..."
        journalctl -u ${SERVICE_NAME} -n 20 --no-pager
    fi
}

configure_nginx() {
    banner "Configurando Nginx (proxy reverso)..."

    read -rp "Configurar Nginx como proxy? [s/N]: " ans
    [[ ! "$ans" =~ ^[Ss]$ ]] && { info "Nginx ignorado"; return; }

    read -rp "Domínio ou IP para o painel (ex: painel.meusite.com): " PANEL_DOMAIN
    PANEL_DOMAIN="${PANEL_DOMAIN:-_}"

    cat > /etc/nginx/sites-available/${SERVICE_NAME} << EOF
server {
    listen 80;
    server_name ${PANEL_DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${PANEL_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
        # SSE support
        proxy_buffering off;
        proxy_cache off;
    }
}
EOF

    ln -sf /etc/nginx/sites-available/${SERVICE_NAME} /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
    nginx -t && systemctl reload nginx
    log "Nginx configurado para ${PANEL_DOMAIN}"
}

open_firewall() {
    banner "Abrindo porta ${PANEL_PORT} no firewall..."
    if command -v ufw &>/dev/null; then
        ufw allow ${PANEL_PORT}/tcp 2>/dev/null || true
        log "Porta ${PANEL_PORT} liberada no UFW"
    fi
    if command -v firewall-cmd &>/dev/null; then
        firewall-cmd --permanent --add-port=${PANEL_PORT}/tcp 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        log "Porta ${PANEL_PORT} liberada no firewalld"
    fi
    # iptables fallback
    iptables -I INPUT -p tcp --dport ${PANEL_PORT} -j ACCEPT 2>/dev/null || true
}

print_info() {
    SERVER_IP=$(curl -s --max-time 5 https://ipv4.icanhazip.com 2>/dev/null || hostname -I | awk '{print $1}')
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║       PAINEL MASTER — INSTALADO COM SUCESSO!     ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${BOLD}URL do Painel:${NC}    http://${SERVER_IP}:${PANEL_PORT}"
    echo -e "  ${BOLD}Admin padrão:${NC}     admin / admin123"
    echo -e "  ${BOLD}Diretório:${NC}        ${INSTALL_DIR}"
    echo ""
    echo -e "  ${YELLOW}⚠  Altere a senha admin após o primeiro login!${NC}"
    echo ""
    echo -e "  ${BOLD}Comandos úteis:${NC}"
    echo -e "    Logs:      journalctl -u ${SERVICE_NAME} -f"
    echo -e "    Reiniciar: systemctl restart ${SERVICE_NAME}"
    echo -e "    Parar:     systemctl stop ${SERVICE_NAME}"
    echo ""
}

# ── Main ──────────────────────────────────────────────────────
clear
banner "╔══════════════════════════════════════╗"
banner "║      PAINEL MASTER — INSTALADOR      ║"
banner "╚══════════════════════════════════════╝"
echo ""

check_root
detect_arch
detect_os
install_system_deps
check_python
install_panel
configure_service
open_firewall
configure_nginx
print_info
