#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_comm.py — Communicate with modulo.py running on SSH servers
"""

import requests
import paramiko
import io
import os


TIMEOUT = 15
SYNC_TIMEOUT = 125  # tempo maior para sincronização com muitos usuários
SYNC_BATCH_SIZE = 50  # usuários por lote na sincronização


def send_command(ip: str, port: int, auth_token: str, command: str,
                 timeout: int = None) -> tuple:
    """Send a shell command to modulo.py on the SSH server.
    Returns (success: bool, output: str).
    """
    url = f"http://{ip}:{port}"
    _timeout = timeout if timeout is not None else TIMEOUT
    try:
        resp = requests.post(
            url,
            headers={'Senha': auth_token},
            data={'comando': command},
            timeout=_timeout
        )
        if resp.status_code == 200:
            return True, resp.text.strip()
        return False, f"HTTP {resp.status_code}: {resp.text.strip()}"
    except Exception as e:
        return False, str(e)


def get_cpu_mem(ip: str, port: int, auth_token: str) -> dict:
    """Return CPU%, MEM used, MEM total from the server."""
    cmd = (
        "echo CPU:$(top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | tr -d '%us,') ; "
        "free -m | awk '/^Mem/{print \"MEM:\" $3 \":\" $2}'"
    )
    ok, out = send_command(ip, port, auth_token, cmd)
    result = {'cpu': 0.0, 'mem_used': 0, 'mem_total': 0, 'error': None}
    if not ok:
        result['error'] = out
        return result
    for line in out.splitlines():
        if line.startswith('CPU:'):
            try:
                result['cpu'] = float(line.split(':')[1])
            except Exception:
                pass
        elif line.startswith('MEM:'):
            parts = line.split(':')
            try:
                result['mem_used'] = int(parts[1])
                result['mem_total'] = int(parts[2])
            except Exception:
                pass
    return result


def get_online_users(ip: str, port: int, auth_token: str) -> list:
    """Return list of currently online SSH users."""
    cmd = "who | awk '{print $1}' | sort -u"
    ok, out = send_command(ip, port, auth_token, cmd)
    if not ok or not out.strip():
        return []
    return [u.strip() for u in out.splitlines() if u.strip()]


def get_user_connections(ip: str, port: int, auth_token: str, username: str) -> int:
    """Count active sessions for a given SSH user.

    `who` não serve aqui: os usuários são túneis puros (ForceCommand
    /bin/false) e não registram utmp, então sempre retornava 0.
    """
    ok, out = send_command(ip, port, auth_token, _sessions_cmd_for_user(username))
    try:
        return max(0, int(out.strip()))
    except Exception:
        return 0


def create_ssh_user_on_server(ip: str, port: int, auth_token: str,
                               username: str, password: str, days: int,
                               limit: int, uuid: str = None) -> tuple:
    if uuid:
        cmd = f"/root/pmaster_agent v2rayadd {uuid} {username} {password} {days} {limit}"
    else:
        cmd = f"/root/pmaster_agent createssh {username} {password} {days} {limit}"
    ok, out = send_command(ip, port, auth_token, cmd)
    if ok and 'CRIADOCOMSUCESSO' not in (out or ''):
        ok = False
    return ok, out


def delete_user_on_server(ip: str, port: int, auth_token: str,
                           username: str, uuid: str = None) -> tuple:
    if uuid:
        cmd = f"/root/pmaster_agent v2raydel {uuid} {username}"
    else:
        cmd = f"/root/pmaster_agent removessh {username}"
    return send_command(ip, port, auth_token, cmd)


def renew_user_on_server(ip: str, port: int, auth_token: str,
                          username: str, days: int) -> tuple:
    cmd = f"/root/pmaster_agent timedata {username} {days}"
    ok, out = send_command(ip, port, auth_token, cmd)
    # HTTP 200 só significa que o painel conseguiu falar com a VPS.
    # É preciso conferir o texto de retorno para saber se o comando
    # realmente funcionou (chage pode falhar mesmo com HTTP 200).
    if ok and 'RENOVADOCOMSUCESSO' not in (out or ''):
        ok = False
    return ok, out


def create_test_user_on_server(ip: str, port: int, auth_token: str,
                               username: str, password: str, hours: int,
                               limit: int, uuid: str = None) -> tuple:
    """Create test user with exact minutes matching the panel's expires_at.
    Uses createsshteste (minutes) so server expiry is in sync with panel.
    """
    minutes = max(1, int(hours)) * 60  # hours → minutes
    if uuid:
        cmd = (f"/root/pmaster_agent v2rayaddteste {uuid} {username} {password} {minutes} {limit} "
               f"2>/dev/null || /root/dragonmodule v2rayaddteste {uuid} {username} {password} {minutes} {limit}")
    else:
        cmd = (f"/root/pmaster_agent createsshteste {username} {password} {minutes} {limit} "
               f"2>/dev/null || /root/dragonmodule createsshteste {username} {password} {minutes} {limit}")
    return send_command(ip, port, auth_token, cmd)


def get_server_existing_state(ip: str, port: int, auth_token: str) -> dict:
    """Retorna o estado atual do servidor:
    - 'xray_uuids': set de UUIDs já presentes no config.json do Xray
    - 'ssh_users':  set de usernames já criados no sistema Linux
    Usado para evitar duplicidade na sincronização.
    """
    # Um único comando: extrai UUIDs do config.json + usuários do sistema
    cmd = (
        # UUIDs do Xray (config.json)
        "python3 -c \""
        "import json,sys\n"
        "try:\n"
        "    cfg=json.load(open('/usr/local/etc/xray/config.json'))\n"
        "    ids=[c.get('id','') for i in cfg.get('inbounds',[]) "
        "for c in i.get('settings',{}).get('clients',[])]\n"
        "    [print('UUID:'+x) for x in ids if x]\n"
        "except:pass\n"
        "\" 2>/dev/null ; "
        # Usuários do sistema (exclui contas de sistema, uid >= 1000)
        "awk -F: '($3>=1000){print \"USER:\"$1}' /etc/passwd 2>/dev/null"
    )
    ok, out = send_command(ip, port, auth_token, cmd, timeout=SYNC_TIMEOUT)
    xray_uuids = set()
    ssh_users = set()
    if ok and out.strip():
        for line in out.strip().splitlines():
            line = line.strip()
            if line.startswith('UUID:'):
                uid = line[5:].strip()
                if uid:
                    xray_uuids.add(uid.lower())
            elif line.startswith('USER:'):
                usr = line[5:].strip()
                if usr:
                    ssh_users.add(usr.lower())
    return {'xray_uuids': xray_uuids, 'ssh_users': ssh_users}


def sync_users_to_server(ip: str, port: int, auth_token: str, users: list, force: bool = False) -> tuple:
    """Sync active users to server in batches, skipping users already present.

    force=True: ignora a verificacao de duplicatas — envia todos os usuarios sem
    consultar o estado atual. Ideal para servidores novos/recem-adicionados.

    Antes de enviar qualquer dado (quando force=False), consulta o servidor:
      - UUIDs já no /usr/local/etc/xray/config.json → pula usuário Xray duplicado
      - Usuários Linux já em /etc/passwd             → pula usuário SSH duplicado
    Só envia quem realmente precisa ser criado.
    """
    from datetime import datetime as _dt2

    if not users:
        return True, 'Nenhum usuário ativo associado a este servidor'

    # ── 1. Busca estado atual do servidor (pula se force=True) ───────────────
    if force:
        xray_uuids: set = set()
        ssh_users:  set = set()
    else:
        existing   = get_server_existing_state(ip, port, auth_token)
        xray_uuids = existing['xray_uuids']   # set lowercase
        ssh_users  = existing['ssh_users']    # set lowercase

    # ── 2. Monta lista filtrando duplicatas ──────────────────────────────────
    lines   = []
    skipped = 0
    for u in users:
        uuid        = (u.get('v2ray_uuid') or '').strip()
        username_lc = u['username'].lower()

        if not force:
            if uuid:
                # Xray: pula se UUID já está no config.json
                if uuid.lower() in xray_uuids:
                    skipped += 1
                    continue
            else:
                # SSH puro: pula se usuário Linux já existe
                if username_lc in ssh_users:
                    skipped += 1
                    continue

        try:
            exp       = _dt2.fromisoformat(u['expires_at'])
            days_left = max(1, (exp - _dt2.now()).days)
        except Exception:
            days_left = 30

        if uuid:
            # pmaster_agent v2rayadd espera: uuid username password days limit
            lines.append(
                f"{uuid} {u['username']} {u['password']} {days_left} {u['connection_limit']}")
        else:
            # SSH puro: username password days limit (4 campos)
            lines.append(
                f"{u['username']} {u['password']} {days_left} {u['connection_limit']}")

    skip_info = f' ({skipped} já existiam no servidor, pulados)' if skipped else ''
    force_info = ' [sincronização forçada]' if force else ''

    if not lines:
        return True, f'Nenhum usuário novo para sincronizar{skip_info}'

    # ── 3. Envia em lotes ────────────────────────────────────────────────────
    batches = [lines[i:i + SYNC_BATCH_SIZE]
               for i in range(0, len(lines), SYNC_BATCH_SIZE)]
    total  = len(lines)
    synced = 0
    errors = []

    for idx, batch in enumerate(batches):
        is_last = (idx == len(batches) - 1)
        content = '\n'.join(batch)
        escaped = content.replace('\\', '\\\\').replace("'", "'\\''")

        if is_last:
            # Último lote: executa o script completo (inclui restart dos serviços)
            cmd = (
                f"printf '%s\\n' '{escaped}' > /tmp/pmg_sync.txt && "
                f"(python3 /root/pmaster_sync.py /tmp/pmg_sync.txt 2>/dev/null || "
                f"python3 /root/sincronizar.py /tmp/pmg_sync.txt 2>/dev/null) && "
                f"echo 'PMG_OK:{len(batch)}'"
            )
        else:
            # Lotes intermediários: cria usuários sem reiniciar os serviços
            cmd = (
                f"printf '%s\\n' '{escaped}' > /tmp/pmg_sync_batch.txt && "
                f"python3 - << 'PYEOF'\n"
                f"import os\n"
                f"import re\n"
                f"with open('/tmp/pmg_sync_batch.txt') as f:\n"
                f"    lines = [l.strip() for l in f if l.strip()]\n"
                f"for linha in lines:\n"
                f"    cols = linha.split()\n"
                f"    if len(cols) >= 5:\n"
                f"        # Verifica se o primeiro campo parece um UUID (usuário V2Ray)\n"
                f"        is_v2ray = bool(re.match(r'[0-9a-f]{{8}}-([0-9a-f]{{4}}-){{3}}[0-9a-f]{{12}}', cols[0].lower()))\n"
                f"        if is_v2ray:\n"
                f"            # V2Ray: uuid, username, password, days, limit\n"
                f"            os.system('/root/pmaster_agent v2rayadd {{}} {{}} {{}} {{}} {{}} 2>/dev/null || /root/dragonmodule v2rayadd {{}} {{}} {{}} {{}} {{}} 2>/dev/null'.format(*cols[:5], *cols[:5]))\n"
                f"        else:\n"
                f"            # SSH: username, password, days, limit\n"
                f"            os.system('/root/pmaster_agent createssh {{}} {{}} {{}} {{}} 2>/dev/null || /root/dragonmodule createssh {{}} {{}} {{}} {{}} 2>/dev/null'.format(*cols[:4], *cols[:4]))\n"
                f"    elif len(cols) >= 4:\n"
                f"        # SSH puro (fallback)\n"
                f"        os.system('/root/pmaster_agent createssh {{}} {{}} {{}} {{}} 2>/dev/null || /root/dragonmodule createssh {{}} {{}} {{}} {{}} 2>/dev/null'.format(*cols[:4], *cols[:4]))\n"
                f"os.remove('/tmp/pmg_sync_batch.txt')\n"
                f"print('BATCH_OK:{len(batch)}')\n"
                f"PYEOF"
            )

        ok, out = send_command(ip, port, auth_token, cmd, timeout=SYNC_TIMEOUT)

        if ok and ('PMG_OK' in out or 'BATCH_OK' in out):
            synced += len(batch)
        else:
            errors.append(f"lote {idx + 1}/{len(batches)}: {out or 'sem resposta'}")

    if errors:
        return False, f'{synced}/{total} criados{skip_info}{force_info}. Erros: {"; ".join(errors)}'
    return True, f'{synced} usuários sincronizados{skip_info}{force_info}'



def install_modules_ssh(ip: str, root_user: str, root_password: str, auth_token: str) -> tuple:
    """Use paramiko to upload module files to the SSH server."""
    modules_dir = os.path.join(os.path.dirname(__file__), 'server_modules')
    files_to_upload = [
        ('pmaster_module.py',  '/root/pmaster_module.py'),
        ('pmaster_agent',      '/root/pmaster_agent'),
        ('pmaster_sync.py',    '/root/pmaster_sync.py'),
        ('sincronizar.py',     '/root/sincronizar.py'),      # legacy compat
        ('pmaster_delete.py',  '/root/pmaster_delete.py'),
        ('pmaster_watchdog.py','/root/pmaster_watchdog.py'),
        ('pmaster-agent.service', '/etc/systemd/system/pmaster-agent.service'),
        ('reap_sessions.py',    '/root/reap_sessions.py'),
        ('reap-sessions.service', '/etc/systemd/system/reap-sessions.service'),
        ('reap-sessions.timer',   '/etc/systemd/system/reap-sessions.timer'),
        ('sshd-limits.conf',      '/etc/systemd/system/sshd.service.d/limits.conf'),
    ]

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=root_user, password=root_password, timeout=20)
        sftp = ssh.open_sftp()

        # Ensure /opt exists
        ssh.exec_command("mkdir -p /opt 2>/dev/null")

        for local_name, remote_path in files_to_upload:
            local_path = os.path.join(modules_dir, local_name)
            if not os.path.exists(local_path):
                continue
            if local_name == 'pmaster_module.py':
                with open(local_path, 'r') as f:
                    content = f.read()
                content = content.replace('REPLACE_AUTH_TOKEN', auth_token)
                sftp.putfo(io.BytesIO(content.encode()), remote_path)
            else:
                sftp.put(local_path, remote_path)

        commands = [
            "chmod +x /root/pmaster_agent",
            "chmod +x /root/pmaster_module.py",
            "chmod +x /root/reap_sessions.py 2>/dev/null || true",
            "mkdir -p /etc/SSHPlus/senha /etc/DragonPanel /var/lib/pmg-reap",
            "mkdir -p /etc/systemd/system/sshd.service.d",
            "touch /root/usuarios.db",
            # ── Segurança: bloqueia acesso ao terminal para usuários VPN ──────
            # Cria grupo que identifica usuários do painel
            "groupadd pmg_vpn 2>/dev/null || true",
            # Corrige usuários migrados de outros painéis (GestorSSH, DragonCore etc.)
            # que possam ter /bin/bash: força /bin/false e adiciona ao grupo
            (
                "awk -F: '($3>=1000 && $1!=\"nobody\" && $1!=\"nfsnobody\"){"
                "print $1}' /etc/passwd | "
                "xargs -I{} bash -c '"
                "usermod -s /bin/false {} 2>/dev/null; "
                "usermod -aG pmg_vpn {} 2>/dev/null' 2>/dev/null || true"
            ),
            # Adiciona bloco no sshd_config (somente uma vez, marcado com PMG_VPN_BLOCK)
            (
                "grep -q 'PMG_VPN_BLOCK' /etc/ssh/sshd_config || "
                "printf '\\n# PMG_VPN_BLOCK - Gerado pelo Painel Master\\n"
                "Match Group pmg_vpn\\n"
                "    PermitTTY no\\n"
                "    X11Forwarding no\\n"
                "    AllowAgentForwarding no\\n"
                "    ForceCommand /bin/false\\n' "
                ">> /etc/ssh/sshd_config"
            ),
            # ─────────────────────────────────────────────────────────────────
            # Reaper de sessões zumbis.
            #
            # O tráfego chega ao sshd por um proxy local (VPNManager/bin/proxy
            # --to 22) que mantém o socket do backend aberto mesmo depois do
            # cliente sumir, então o sshd nunca descarta a sessão velha.
            # ClientAliveInterval não resolve: quem responde ao keepalive é o
            # proxy. Medido: 1670 sessões, 71% sem CPU, 1.192 com +24h.
            #
            # ClientAlive* dentro de Match: o sshd recusa LoginGraceTime aí,
            # então esse vai para a seção global, antes do primeiro Match.
            (
                "sed -i 's/^#\\s*LoginGraceTime.*/LoginGraceTime 30\\n# PMG_VPN_REAPER/' "
                "/etc/ssh/sshd_config; "
                "grep -q '^# PMG_VPN_REAPER' /etc/ssh/sshd_config || "
                "sed -i '/^# PMG_VPN_REAPER/!{/^Match /i LoginGraceTime 30\\n# PMG_VPN_REAPER\\n}' "
                "/etc/ssh/sshd_config"
            ),
            (
                "grep -q 'PMG_VPN_REAPER_BLOCK' /etc/ssh/sshd_config || "
                "printf '\\n# PMG_VPN_REAPER_BLOCK - Gerado pelo Painel Master\\n"
                "Match Group pmg_vpn\\n"
                "    ClientAliveInterval 60\\n"
                "    ClientAliveCountMax 3\\n' "
                ">> /etc/ssh/sshd_config"
            ),
            # Valida antes de recarregar: config inválida derrubaria o SSH
            "sshd -t || cp -a /etc/ssh/sshd_config /etc/ssh/sshd_config.reap-bak",
            "systemctl daemon-reload",
            "systemctl enable --now reap-sessions.timer 2>/dev/null || true",
            "systemctl restart reap-sessions.timer 2>/dev/null || true",
            # Reinicia sshd para aplicar as restrições
            "systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || "
            "systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true",
            # ─────────────────────────────────────────────────────────────────
            "systemctl daemon-reload",
            "systemctl enable pmaster-agent 2>/dev/null || true",
            "systemctl restart pmaster-agent 2>/dev/null || systemctl start pmaster-agent",
            "pkill -f pmaster_module.py 2>/dev/null || true",
            "(crontab -l 2>/dev/null | grep -v pmaster_watchdog; echo '* * * * * python3 /root/pmaster_watchdog.py') | crontab -",
        ]
        for cmd in commands:
            ssh.exec_command(cmd)

        sftp.close()
        ssh.close()
        return True, 'Módulos instalados com sucesso'
    except Exception as e:
        return False, str(e)


def get_online_users_ps(ip: str, port: int, auth_token: str) -> list:
    """Return usernames currently connected via SSH/OpenVPN/Dropbear.
    Uses ps-based method like Dragon Core (more reliable than 'who').
    """
    cmd = (
        "ps -x 2>/dev/null | grep sshd | grep -v root | grep priv | awk '{print $1}' | "
        "while read pid; do cat /proc/$pid/status 2>/dev/null | grep -iE 'name|ppid' ; done | "
        "grep -A1 sshd | grep -v sshd | head -100 ; "
        # fallback: who output
        "who 2>/dev/null | awk '{print $1}' | sort -u"
    )
    # Simpler and more reliable: get sshd child processes usernames
    cmd2 = (
        "ps aux | grep -E 'sshd:.+@' | grep -v grep | awk '{print $1}' | sort -u"
    )
    ok, out = send_command(ip, port, auth_token, cmd2)
    if not ok or not out.strip():
        # fallback to who
        ok2, out2 = send_command(ip, port, auth_token, "who | awk '{print $1}' | sort -u")
        if ok2 and out2.strip():
            return [u.strip() for u in out2.splitlines() if u.strip() and u.strip() != 'root']
        return []
    users = [u.strip() for u in out.splitlines() if u.strip() and u.strip() not in ('root', 'sshd')]
    return users


def get_server_online_count(ip: str, port: int, auth_token: str) -> dict:
    """Return total online count (SSH + OpenVPN + Dropbear) like Dragon Core script."""
    cmd = (
        "ssh_count=$(ps -x 2>/dev/null | grep sshd | grep -v root | grep priv | wc -l); "
        "ovpn_count=0; [ -e /etc/openvpn/openvpn-status.log ] && ovpn_count=$(grep -c '10.8.0' /etc/openvpn/openvpn-status.log 2>/dev/null || echo 0); "
        "drp_count=0; if [ -e /etc/default/dropbear ]; then drp=$(ps aux | grep dropbear | grep -v grep | wc -l); drp_count=$((drp - 1)); fi; "
        "total=$((ssh_count + ovpn_count + drp_count)); echo $total"
    )
    ok, out = send_command(ip, port, auth_token, cmd)
    try:
        total = int(out.strip())
    except Exception:
        total = 0
    return {'total': total, 'error': None if ok else out}



# ── Contagem de sessões SSH ────────────────────────────────────────────────
# Os usuários do grupo `pmg_vpn` entram com `ForceCommand /bin/false` e
# `PermitTTY no`, então são túneis puros: NÃO registram entrada em utmp e
# `who` sempre retorna vazio. A contagem precisa vir do `ps`.
#
# Cada conexão gera DOIS processos:
#   sshd: <user> [priv]   -> processo privilegiado (PPID 1), pai do outro
#   sshd: <user>          -> a sessão do usuário
# Contar as duas linhas dobra o número de conexões. Por isso filtramos por
# NF==2 (só a linha da sessão) e ignoramos as formas de serviço
# `sshd: [accepted]`, `sshd: [rexeced]`, `sshd: user@pts/0` e o listener.
_SESSIONS_PS = (
    "ps -eo args 2>/dev/null | "
    "awk '$1==\"sshd:\" && NF==2 && $2 !~ /^[[\\/@]/ && $2!=\"root\" && $2!=\"sshd\" {print $2}'"
)


def _sessions_cmd_for_user(username: str) -> str:
    """Count real sessions of one user. Exact string compare (no regex injection)."""
    safe = username.replace("'", "'\\''")
    return (
        "ps -eo args 2>/dev/null | "
        f"awk -v u='{safe}' '$1==\"sshd:\" && NF==2 && $2==u {{c++}} END{{print c+0}}'"
    )


def get_online_users_robust(ip: str, port: int, auth_token: str) -> list:
    """Return [{'username', 'connections'}] for every user with a live session.

    Uses `ps` on purpose: the users are pure SSH tunnels, so utmp/`who` is
    always empty, and /opt/pmg-monitor is a broken build on these servers
    (ImportError on the bundled psutil) that only wasted a subprocess per call.
    """
    ok, out = send_command(
        ip, port, auth_token,
        _SESSIONS_PS + " | sort | uniq -c | awk '{print $2 \":\" $1}'"
    )
    result = []
    if not ok or not out.strip():
        return result
    for line in out.strip().splitlines():
        line = line.strip()
        if ':' not in line:
            continue
        uname, count = line.rsplit(':', 1)
        uname = uname.strip()
        if not uname or uname in ('root', 'sshd'):
            continue
        try:
            result.append({'username': uname, 'connections': int(count.strip())})
        except ValueError:
            continue
    return result


def count_user_connections_on_server(ip: str, port: int, auth_token: str, username: str) -> int:
    """Count active sessions for a specific user (ps-based, see _SESSIONS_PS)."""
    ok, out = send_command(ip, port, auth_token, _sessions_cmd_for_user(username))
    try:
        return max(0, int(out.strip()))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Multi-server broadcast helpers
# ---------------------------------------------------------------------------

def broadcast_renew(user, days_for_server: int, db_module) -> list:
    """
    Renew `user` on ALL servers it belongs to (primary + extras).
    Estratégia: exclui e recria o usuário do zero em cada servidor com os
    dados atuais do painel, em vez de tentar atualizar via chage. Isso
    garante que a VPS fique idêntica ao painel mesmo que o usuário não
    existisse mais lá, tivesse senha diferente, ou o chage estivesse
    falhando silenciosamente.
    Returns list of (server_name, ok, msg) tuples.
    """
    server_ids = db_module.get_user_all_server_ids(user['id'])
    uuid = user['v2ray_uuid'] if 'v2ray_uuid' in user.keys() else None
    results = []
    for sid in server_ids:
        srv = db_module.get_server(sid)
        if not srv:
            continue
        ok, msg = create_ssh_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            user['username'], user['password'], days_for_server,
            user['connection_limit'], uuid=uuid
        )
        results.append((srv['name'], ok, msg))
    return results


def build_block_cmd(user) -> str:
    """Command that fully blocks a user on a server.

    `passwd -l` alone is not enough: it does NOT drop sessions that are already
    open, and it does nothing at all for VLESS/Xray users — they keep connecting
    with their UUID even after the panel marks them suspended. The agent's
    `blockuser` handles the password lock, the session kill, the account expiry
    and the removal of the UUID from the Xray config.
    """
    username = user['username']
    uuid = user['v2ray_uuid'] if 'v2ray_uuid' in user.keys() else None
    return (
        f"/root/pmaster_agent blockuser {username} {uuid or ''} 2>/dev/null || "
        f"/root/dragonmodule blockuser {username} {uuid or ''} 2>/dev/null || true"
    )


def build_unblock_cmd(user) -> str:
    """Command that unlocks a user. The renewal itself recreates the account
    (and re-adds the UUID to Xray), so unlocking is just a safety net."""
    username = user['username']
    return (
        f"/root/pmaster_agent unblockuser {username} 2>/dev/null || "
        f"/root/dragonmodule unblockuser {username} 2>/dev/null || true"
    )


def broadcast_delete(user, db_module) -> list:
    """
    Delete `user` from ALL servers it belongs to (primary + extras).
    Returns list of (server_name, ok, msg) tuples.
    Accepts sqlite3.Row or dict.
    """
    server_ids = db_module.get_user_all_server_ids(user['id'])
    # sqlite3.Row has no .get() — access key directly with fallback
    uuid = user['v2ray_uuid'] if 'v2ray_uuid' in user.keys() else None
    results = []
    for sid in server_ids:
        srv = db_module.get_server(sid)
        if not srv:
            continue
        ok, msg = delete_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            user['username'], uuid
        )
        results.append((srv['name'], ok, msg))
    return results


def broadcast_command(user, command: str, db_module) -> list:
    """
    Run an arbitrary command on ALL servers for a user.
    Returns list of (server_name, ok, output) tuples.
    """
    server_ids = db_module.get_user_all_server_ids(user['id'])
    results = []
    for sid in server_ids:
        srv = db_module.get_server(sid)
        if not srv:
            continue
        ok, out = send_command(srv['ip'], srv['module_port'], srv['auth_token'], command)
        results.append((srv['name'], ok, out))
    return results


def setup_vnc_ssh(ip: str, root_user: str, root_password: str) -> tuple:
    """Instala e configura um servidor VNC (Xvfb + x11vnc) no servidor.

    Usa um display virtual headless (Xvfb) que responde a conexões VNC.
    Isso permite recuperar o servidor pelo VNC mesmo se o SSH/porta 22
    estiver corrompido ou indisponível, pois o serviço VNC sobe no boot
    de forma independente.

    Returns (success: bool, info: dict) onde info contém:
      - 'vnc_port'   : porta VNC (default 5900)
      - 'vnc_user'   : usuário de acesso (root)
      - 'vnc_pass'   : senha gerada para o VNC
      - 'message'    : descrição/texto
    """
    import random
    import string

    vnc_port = 5900
    # Senha VNC forte (x11vnc limita a 8 chars quando usa VNC auth clássica)
    vnc_pass = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(8))

    # Comandos preparados (evita problemas de escaping via exec_command)
    cmds = [
        # Instala o mínimo para um display virtual + VNC
        "export DEBIAN_FRONTEND=noninteractive",
        "command -v apt-get >/dev/null 2>&1 && (apt-get update -y >/dev/null 2>&1 || true)",
        "command -v apt-get >/dev/null 2>&1 && (apt-get install -y xvfb x11vnc xauth fluxbox >/dev/null 2>&1 || true)",
        "command -v yum >/dev/null 2>&1 && (yum install -y xorg-x11-server-Xvfb x11vnc fluxbox >/dev/null 2>&1 || true)",
        # Cria diretório de estado VNC
        "mkdir -p /etc/pmaster-vnc",
        # Armazena a senha
        f"printf '%s\\n' '{vnc_pass}' > /etc/pmaster-vnc/pass",
        "touch /root/.Xauthority",
    ]

    # Unit systemd que sobe o display virtual + x11vnc no boot
    unit = (
        "[Unit]\n"
        "Description=Painel Master VNC (Xvfb + x11vnc)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "Environment=DISPLAY=:99\n"
        "ExecStartPre=/bin/sh -c 'pkill -x Xvfb 2>/dev/null || true'\n"
        "ExecStartPre=/bin/sh -c 'pkill -x x11vnc 2>/dev/null || true'\n"
        "ExecStart=/bin/sh -c ': > /root/.Xauthority; "
        f"Xvfb :99 -screen 0 1280x800x24 & "
        f"sleep 2 && exec x11vnc -display :99 -forever -shared -rfbport {vnc_port} "
        f"-rfbauth /etc/pmaster-vnc/pass -noxdamage -listen 0.0.0.0'\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=root_user, password=root_password, timeout=25)
        # instala pacotes e prepara
        for c in cmds:
            ssh.exec_command(c)
        # grava a senha em formato aceito pelo x11vnc via -storepasswd em runtime
        ssh.exec_command(
            "mkdir -p /etc/pmaster-vnc && "
            f"x11vnc -storepasswd '{vnc_pass}' /etc/pmaster-vnc/pass 2>/dev/null || "
            f"printf '%s\\n' '{vnc_pass}' > /etc/pmaster-vnc/pass"
        )
        # grava o unit file
        sftp = ssh.open_sftp()
        try:
            with sftp.open('/etc/systemd/system/pmaster-vnc.service', 'w') as f:
                f.write(unit)
        finally:
            sftp.close()

        # abre firewall + habilita/inicia o serviço
        apply = (
            "systemctl daemon-reload && "
            f"systemctl enable pmaster-vnc 2>/dev/null; "
            f"systemctl restart pmaster-vnc; "
            # firewall
            "command -v ufw >/dev/null 2>&1 && (ufw allow {port}/tcp >/dev/null 2>&1 || true); "
            "command -v firewall-cmd >/dev/null 2>&1 && (firewall-cmd --permanent --add-port={port}/tcp >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 || true); "
            "command -v iptables >/dev/null 2>&1 && (iptables -C INPUT -p tcp --dport {port} -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport {port} -j ACCEPT 2>/dev/null || true)"
        ).format(port=vnc_port)
        ssh.exec_command(apply)

        # pequena espera para o serviço subir antes de checar
        import time
        time.sleep(4)

        stdin, stdout, stderr = ssh.exec_command("systemctl is-active pmaster-vnc")
        active = (stdout.read().decode().strip() == 'active')

        stdin2, stdout2, stderr2 = ssh.exec_command(
            f"ss -tulnp 2>/dev/null | grep -q ':{vnc_port}' && echo OPEN || echo CLOSED"
        )
        port_state = stdout2.read().decode().strip()

        ssh.close()

        info = {
            'success': active and port_state == 'OPEN',
            'vnc_port': vnc_port,
            'vnc_user': root_user,
            'vnc_pass': vnc_pass,
            'message': (
                f"VNC {'configurado com sucesso' if (active and port_state=='OPEN') else 'instalado, mas precisa de atenção'}. "
                f"Serviço pmaster-vnc {'ativo' if active else 'inativo'}; porta {vnc_port} {port_state}."
            )
        }
        return active and port_state == 'OPEN', info
    except Exception as e:
        return False, {'success': False, 'vnc_port': vnc_port, 'vnc_user': root_user,
                       'vnc_pass': vnc_pass, 'message': f'Erro ao configurar VNC: {e}'}
