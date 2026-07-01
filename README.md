# Painel Master — Gerenciador SSH

Sistema web completo para gerenciamento de usuários SSH/V2Ray com suporte a revendas, Mercado Pago, backup Telegram e múltiplos servidores.

## 🚀 Instalação rápida

```bash
curl -sSL https://raw.githubusercontent.com/jeanfraga95/painel101/main/install.sh | bash
```

Ou manualmente:
```bash
git clone https://github.com/jeanfraga33/painel-master
cd painel-master
bash install.sh
```

## 📋 Requisitos

- Ubuntu 20.04+ ou Debian 11+ (ARM64 ou x86_64)
- Python 3.8+
- Root access

## 🔑 Acesso padrão

| Campo | Valor |
|-------|-------|
| URL | `http://SEU-IP:5000` |
| Usuário | `admin` |
| Senha | `admin123` |

> ⚠️ **Altere a senha imediatamente após o primeiro login!**

## ✨ Funcionalidades

### Administrador
- ✅ Dashboard com estatísticas em tempo real
- ✅ Criar/listar/deletar/suspender usuários SSH
- ✅ Criar testes por horas (auto-deletados ao vencer)
- ✅ Suporte a V2Ray/Xray (UUID automático)
- ✅ Limite de conexões simultâneas por usuário
- ✅ Gerenciar múltiplos servidores SSH
- ✅ Instalar módulos nos servidores via SSH
- ✅ Sincronizar usuários com servidores
- ✅ Monitoramento de CPU/RAM em tempo real (SSE)
- ✅ Usuários online por servidor
- ✅ Executar comandos nos servidores (iptables -F, reboot, etc.)
- ✅ Criar e gerenciar revendas/sub-revendas
- ✅ Backup automático para Telegram a cada 6h
- ✅ Restauração de backup
- ✅ Configuração de aparência (nome, cor, tema)
- ✅ Configurar link do aplicativo VPN
- ✅ API CheckUser compatível com DTunnel

### Revendas / Sub-revendas
- ✅ Hierarquia ilimitada de revendas
- ✅ Limite de contas controlado pelo pai
- ✅ Criação de usuários SSH e testes
- ✅ Modal com dados do usuário + botão copiar
- ✅ Visualizar usuários dos sub-revendas
- ✅ Configuração individual do Mercado Pago

### Usuários SSH
- ✅ Portal de verificação de validade
- ✅ QR Code para renovação via Mercado Pago
- ✅ Renovação automática após pagamento aprovado

## 🖥️ Módulos do Servidor SSH

Após adicionar um servidor no painel, clique em **"Instalar Módulos"** para enviar automaticamente via SSH:

| Arquivo | Função |
|---------|--------|
| `modulo.py` | API HTTP para receber comandos do painel |
| `dragonmodule` | Script de criação/remoção de usuários |
| `sincronizar.py` | Sincronização em lote |
| `delete.py` | Remoção em lote |
| `verificador.py` | Watchdog do modulo.py (cron) |

### Instalação manual do CheckUser

No servidor SSH, execute:
```bash
bash <(curl -sSL https://raw.githubusercontent.com/jeanfraga33/painel-master/main/server_modules/checkuser_install.sh)
```

## ⚙️ API CheckUser

Endpoint compatível com DTunnel e outros apps VPN:

```
GET /checkuser/<username>
```

Resposta:
```json
{
  "username": "usuario1",
  "count_connections": 2,
  "expiry_date": "2025-12-31",
  "expiry_days": 180,
  "limit_connections": 3
}
```

## 🔧 Comandos de manutenção

```bash
# Ver logs
journalctl -u painel-master -f

# Reiniciar painel
systemctl restart painel-master

# Backup manual
cd /opt/painel-master && source venv/bin/activate
python3 -c "import backup; backup.create_backup_archive()"
```

## 📁 Estrutura

```
painel-master/
├── app.py              # Aplicação Flask principal
├── db.py               # Banco de dados SQLite3
├── server_comm.py      # Comunicação com servidores SSH
├── backup.py           # Backup/restauração
├── requirements.txt    # Dependências Python
├── install.sh          # Instalador
├── templates/          # Templates HTML
│   ├── base.html
│   ├── login.html
│   ├── user_login.html
│   ├── admin/
│   ├── reseller/
│   └── shared/
└── server_modules/     # Arquivos enviados aos servidores SSH
    ├── modulo.py
    ├── dragonmodule
    ├── sincronizar.py
    ├── delete.py
    ├── verificador.py
    └── checkuser_install.sh
```
