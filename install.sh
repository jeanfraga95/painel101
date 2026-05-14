#!/bin/bash
# =============================================================
#  Painel Master — Instalador v4
#  Repositório: https://github.com/jeanfraga33/painel-master
# =============================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

INSTALL_DIR="/opt/painel-master"
SERVICE_NAME="painel-master"
PANEL_PORT="${PANEL_PORT:-5000}"
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
        OS_ID="$ID"; OS_VER="$VERSION_ID"
    else
        OS_ID="unknown"; OS_VER="unknown"
    fi
    info "Sistema: $OS_ID $OS_VER"
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
                libsqlite3-dev sqlite3 perl openssl jq 2>/dev/null \
                || warn "Algumas dependencias podem ter falhado"
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

    # Nenhum funcional encontrado — tenta instalar Python 3.10 do sistema
    warn "Nenhum Python 3.8+ com sqlite3 encontrado. Tentando instalar..."
    case "$OS_ID" in
        ubuntu|debian)
            # Garante libsqlite3-dev e tenta reinstalar python3
            apt-get install -y libsqlite3-dev python3.10 python3.10-venv python3.10-dev 2>/dev/null || true
            # Verifica se o python3.10 do apt agora tem sqlite3
            if [ -x /usr/bin/python3.10 ] && python_has_sqlite /usr/bin/python3.10; then
                PYTHON_CMD=/usr/bin/python3.10
                log "Python 3.10 do sistema instalado com sqlite3 OK"
                return 0
            fi
            # Fallback: python3.8 do sistema
            if [ -x /usr/bin/python3.8 ] && python_has_sqlite /usr/bin/python3.8; then
                PYTHON_CMD=/usr/bin/python3.8
                log "Usando Python 3.8 do sistema com sqlite3 OK"
                return 0
            fi
            ;;
    esac

    error "Nenhum Python 3.8+ com suporte a sqlite3 encontrado.
O Python em /usr/local/bin/ parece ter sido compilado sem --enable-loadable-sqlite-extensions.

Para corrigir manualmente, execute:
  apt-get install -y libsqlite3-dev python3.8 python3.8-venv
E reexecute o instalador."
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

# ── Copia arquivos ─────────────────────────────────────────────
copy_files() {
    for src in "$SCRIPT_DIR" "$(pwd)"; do
        if [ -f "$src/app.py" ] && [ -d "$src/templates" ] && [ -f "$src/templates/login.html" ]; then
            info "Copiando de: $src"
            mkdir -p "$INSTALL_DIR"
            if command -v rsync &>/dev/null; then
                rsync -a --exclude='.git' --exclude='__pycache__' \
                      --exclude='*.pyc' --exclude='venv' --exclude='painel.db' \
                      "$src/" "$INSTALL_DIR/"
            else
                cp -r "$src/." "$INSTALL_DIR/"
                rm -rf "$INSTALL_DIR/.git" "$INSTALL_DIR/__pycache__" "$INSTALL_DIR/venv" 2>/dev/null || true
            fi
            log "Arquivos copiados com sucesso"
            return 0
        fi
    done

    info "Tentando clonar repositorio..."
    if git clone --depth=1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
        log "Repositorio clonado"
        return 0
    fi

    error "Arquivos do painel nao encontrados.
Execute: cd painel-master && bash install.sh"
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
Environment="PORT=${PANEL_PORT}"
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
    banner "Liberando porta ${PANEL_PORT}..."
    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow "${PANEL_PORT}/tcp" 2>/dev/null || true
        log "UFW: porta ${PANEL_PORT} liberada"
    fi
    if command -v firewall-cmd &>/dev/null && firewall-cmd --state 2>/dev/null | grep -q running; then
        firewall-cmd --permanent --add-port="${PANEL_PORT}/tcp" 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        log "firewalld: porta ${PANEL_PORT} liberada"
    fi
    iptables -I INPUT -p tcp --dport "${PANEL_PORT}" -j ACCEPT 2>/dev/null || true
    log "iptables: porta ${PANEL_PORT} liberada"
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
    echo -e "  URL:           ${BOLD}http://${SERVER_IP}:${PANEL_PORT}${NC}"
    echo -e "  Login:         admin  /  admin123"
    echo -e "  Python usado:  ${PYTHON_CMD}"
    echo -e "  Diretorio:     ${INSTALL_DIR}"
    echo ""
    echo -e "  ${YELLOW}⚠  Troque a senha do admin apos o primeiro login!${NC}"
    echo ""
    echo -e "  Comandos:"
    echo -e "    journalctl -u ${SERVICE_NAME} -f"
    echo -e "    systemctl restart ${SERVICE_NAME}"
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
detect_arch
detect_os
uninstall_existing
free_ports
install_system_deps
check_python
install_panel
configure_service
open_firewall
print_info
