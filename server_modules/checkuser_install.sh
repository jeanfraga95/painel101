#!/bin/bash
# checkuser_install.sh — Install CheckUser on SSH server (DTunnel compatible)

get_arch() {
    case "$(uname -m)" in
        x86_64|x64|amd64) echo 'amd64' ;;
        armv8|arm64|aarch64) echo 'arm64' ;;
        *) echo 'unsupported' ;;
    esac
}

check_url_access() {
    local test_url=$1
    echo -e "\n🔍 Testando acesso externo: $test_url"
    if curl -s --max-time 5 "$test_url" >/dev/null; then
        echo -e "\e[1;32m✅ URL acessível externamente.\e[0m"
        return
    fi
    echo -e "\e[1;31m❌ URL não acessível externamente.\e[0m"
    echo -ne "\e[1;33mAbrir porta no iptables automaticamente? [s/N]: \e[0m"
    read answer
    if [[ "$answer" =~ ^[Ss]$ ]]; then
        local port=$(echo "$test_url" | grep -oE ':[0-9]+' | tr -d ':')
        sudo iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
        sudo iptables-save > /etc/iptables.rules 2>/dev/null || true
        echo -e "\e[1;32m✔ Porta $port liberada.\e[0m"
    fi
}

install_checkuser() {
    local latest_release=$(curl -s https://api.github.com/repos/DTunnel0/CheckUser-Go/releases/latest | grep "tag_name" | cut -d'"' -f4)
    local arch=$(get_arch)

    if [ "$arch" = "unsupported" ]; then
        echo -e "\e[1;31mArquitetura não suportada!\e[0m"
        exit 1
    fi

    local name="checkuser-linux-$arch"
    echo "⬇️  Baixando $name ($latest_release)..."
    wget -q "https://github.com/DTunnel0/CheckUser-Go/releases/download/$latest_release/$name" -O /usr/local/bin/checkuser
    chmod +x /usr/local/bin/checkuser

    local addr=$(curl -s https://ipv4.icanhazip.com)
    local domain_json=$(curl -s https://dns.dtunnel.com.br/api/v1/dns/create -X POST --data '{"content": "'"$addr"'", "proxied": true}' 2>/dev/null || echo '{}')
    local url=$(echo "$domain_json" | grep -o '"domain": *"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"')

    local port="2052"
    local sslEnabled=""
    local final_url="http://$addr:$port"

    if [[ -n "$url" ]]; then
        port="2053"
        sslEnabled="--ssl"
        final_url="https://$url:$port"
    fi

    if systemctl is-active --quiet checkuser 2>/dev/null; then
        systemctl stop checkuser
        systemctl disable checkuser
        rm -f /etc/systemd/system/checkuser.service
        systemctl daemon-reload
    fi

    cat > /etc/systemd/system/checkuser.service << EOF
[Unit]
Description=CheckUser Service
After=network.target nss-lookup.target

[Service]
User=root
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ExecStart=/usr/local/bin/checkuser --start --port $port $sslEnabled
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl start checkuser
    systemctl enable checkuser

    echo -e "\n\e[1;32m✅ CheckUser instalado com sucesso!\e[0m"
    echo -e "\e[1;34m🌐 URL Base: \e[1;36m$final_url\e[0m"
    echo ""
    echo -e "\e[1;32m📋 URLs para configurar no Painel Master (Configurações → CheckUser URL):\e[0m"
    echo -e "\e[1;33m  DTunnel app:  ${final_url}/checkuser/dtunnel.php?user=\e[0m"
    echo -e "\e[1;33m  Navegador:    ${final_url}/checkuser/<username>\e[0m"
    echo ""
    echo -e "\e[1;36m⚠  Use a URL do Painel Master, não a do CheckUser externo!\e[0m"
    echo -e "\e[1;36m   O Painel Master já tem endpoint compatível com DTunnel embutido.\e[0m"
    check_url_access "$final_url"

    echo -e "\nPressione Enter para continuar..."
    read
}

main() {
    clear
    echo '-------------------------------------'
    echo -ne '     \e[1;33mCHECKUSER\e[0m'
    if [[ -x /usr/local/bin/checkuser ]]; then
        echo -e ' \e[1;32mv'$(/usr/local/bin/checkuser --version 2>/dev/null | cut -d' ' -f2)'\e[0m'
    else
        echo -e ' \e[1;31m[DESINSTALADO]\e[0m'
    fi
    echo '-------------------------------------'
    echo -e '\e[1;32m[01]\e[0m Instalar CheckUser'
    echo -e '\e[1;32m[02]\e[0m Reinstalar CheckUser'
    echo -e '\e[1;32m[03]\e[0m Desinstalar CheckUser'
    echo -e '\e[1;32m[00]\e[0m Sair'
    echo '-------------------------------------'
    echo -ne '\e[1;32mOpção: \e[0m'
    read option

    case $option in
        1|01) install_checkuser; main ;;
        2|02)
            systemctl stop checkuser 2>/dev/null || true
            systemctl disable checkuser 2>/dev/null || true
            rm -f /usr/local/bin/checkuser /etc/systemd/system/checkuser.service
            systemctl daemon-reload
            install_checkuser
            main ;;
        3|03)
            systemctl stop checkuser 2>/dev/null || true
            systemctl disable checkuser 2>/dev/null || true
            rm -f /usr/local/bin/checkuser /etc/systemd/system/checkuser.service
            systemctl daemon-reload
            echo -e "\e[1;31m✔ CheckUser removido.\e[0m"
            echo "Pressione Enter..."; read
            main ;;
        0|00) echo "Saindo."; exit 0 ;;
        *) echo "Opção inválida."; sleep 1; main ;;
    esac
}

main
