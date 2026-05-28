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
    """Count active connections for a given SSH user."""
    cmd = f"who | grep '^{username} ' | wc -l"
    ok, out = send_command(ip, port, auth_token, cmd)
    try:
        return int(out.strip())
    except Exception:
        return 0


def create_ssh_user_on_server(ip: str, port: int, auth_token: str,
                               username: str, password: str, days: int,
                               limit: int, uuid: str = None) -> tuple:
    if uuid:
        cmd = f"/root/pmaster_agent v2rayadd {uuid} {username} {password} {days} {limit}"
    else:
        cmd = f"/root/pmaster_agent createssh {username} {password} {days} {limit}"
    return send_command(ip, port, auth_token, cmd)


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
    return send_command(ip, port, auth_token, cmd)


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


def sync_users_to_server(ip: str, port: int, auth_token: str, users: list) -> tuple:
    """Sync active users to server in batches, skipping users already present.

    Antes de enviar qualquer dado, consulta o servidor:
      - UUIDs já no /usr/local/etc/xray/config.json → pula usuário Xray duplicado
      - Usuários Linux já em /etc/passwd             → pula usuário SSH duplicado
    Só envia quem realmente precisa ser criado.
    """
    from datetime import datetime as _dt2

    # ── 1. Busca estado atual do servidor ────────────────────────────────────
    existing   = get_server_existing_state(ip, port, auth_token)
    xray_uuids = existing['xray_uuids']   # set lowercase
    ssh_users  = existing['ssh_users']    # set lowercase

    # ── 2. Monta lista filtrando duplicatas ──────────────────────────────────
    lines   = []
    skipped = 0
    for u in users:
        uuid        = (u.get('v2ray_uuid') or '').strip()
        username_lc = u['username'].lower()

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
            lines.append(
                f"{u['username']} {u['password']} {days_left} {u['connection_limit']} {uuid}")
        else:
            lines.append(
                f"{u['username']} {u['password']} {days_left} {u['connection_limit']}")

    skip_info = f' ({skipped} já existiam no servidor, pulados)' if skipped else ''

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
                f"with open('/tmp/pmg_sync_batch.txt') as f:\n"
                f"    lines = [l.strip() for l in f if l.strip()]\n"
                f"for linha in lines:\n"
                f"    cols = linha.split()\n"
                f"    if len(cols) >= 5:\n"
                f"        os.system('/root/pmaster_agent v2rayadd {{}} {{}} {{}} {{}} {{}} 2>/dev/null"
                f" || /root/dragonmodule v2rayadd {{}} {{}} {{}} {{}} {{}} 2>/dev/null'.format(*cols[:5], *cols[:5]))\n"
                f"    elif len(cols) >= 4:\n"
                f"        os.system('/root/pmaster_agent createssh {{}} {{}} {{}} {{}} 2>/dev/null"
                f" || /root/dragonmodule createssh {{}} {{}} {{}} {{}} 2>/dev/null'.format(*cols[:4], *cols[:4]))\n"
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
        return False, f'{synced}/{total} criados{skip_info}. Erros: {"; ".join(errors)}'
    return True, f'{synced} usuários sincronizados{skip_info}'




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
        ('pmg-monitor',        '/opt/pmg-monitor'),          # online users monitor
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
            "chmod +x /opt/pmg-monitor",
            "mkdir -p /etc/SSHPlus/senha /etc/DragonPanel",
            "touch /root/usuarios.db",
            "pkill -f pmaster_module.py || true",
            "nohup python3 /root/pmaster_module.py > /root/pmaster_module.log 2>&1 &",
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



def get_online_users_robust(ip: str, port: int, auth_token: str) -> list:
    """Get online SSH users using the ps-based approach.
    Modified from user's script to output parseable format.
    Falls back through multiple detection methods.
    """
    import json as _json

    # ── Method 1: pmg-monitor or plugin-sync binary (fastest, JSON output) ─
    # Nota: pmg-monitor pode retornar {} vazio para usuários migrados de
    # GestorSSH/DragonCore que não estão no seu banco local. Nesse caso
    # NÃO retornamos — caímos no Method 2 para pegar todos os usuários via ps.
    monitor_result = []
    for monitor_bin in ('/opt/pmg-monitor', '/opt/sshplus/plugin-sync'):
        ok, out = send_command(ip, port, auth_token,
                               f"{monitor_bin} --monitor-users 2>/dev/null")
        if ok and out.strip().startswith('{'):
            try:
                data = _json.loads(out.strip())
                monitor_result = [
                    {'username': u, 'connections': int(c)}
                    for u, c in data.items()
                    if u and u not in ('root', 'sshd', '')
                ]
                if monitor_result:
                    break
            except Exception:
                pass

    # ── Method 2: ps -eo args ────────────────────────────────────────────────
    # BUG CORRIGIDO: awk usava -F'[: ]+' que retornava "usuario@pts/0" como
    # username. Agora usa -F'[: @]+' para separar o "@pts/X" do username.
    cmd = (
        "ps -eo args 2>/dev/null | grep 'sshd:' | grep -v grep | grep -v '\\[' | "
        "awk -F'[: @]+' '/sshd:/ {if($2 && $2!=\"root\" && $2!=\"sshd\") print $2}' | "
        "grep -v '^root$' | grep -v '^sshd$' | grep -v '^$' | "
        "sort | uniq -c | awk '{print $2 \":\" $1}'"
    )
    ok, out = send_command(ip, port, auth_token, cmd)
    ps_result = []
    if ok and out.strip():
        for line in out.strip().splitlines():
            line = line.strip()
            if ':' in line:
                uname, count = line.rsplit(':', 1)
                uname = uname.strip()
                if uname and uname not in ('root', 'sshd', ''):
                    try:
                        ps_result.append({'username': uname, 'connections': int(count.strip())})
                    except ValueError:
                        pass

    # Mescla: monitor_result tem prioridade (mais preciso), mas ps_result
    # captura usuários migrados que o pmg-monitor não conhece
    if ps_result:
        if monitor_result:
            # Une os dois, sem duplicatas — ps_result sobrescreve contagem
            monitor_names = {r['username'].lower() for r in monitor_result}
            merged = list(monitor_result)
            for item in ps_result:
                if item['username'].lower() not in monitor_names:
                    merged.append(item)
            return merged
        return ps_result

    if monitor_result:
        return monitor_result

    # ── Method 3: who fallback ─────────────────────────────────────────────
    cmd_who = (
        "who 2>/dev/null | awk '{print $1}' | grep -v '^root$' | "
        "sort | uniq -c | awk '{print $2 \":\" $1}'"
    )
    ok3, out3 = send_command(ip, port, auth_token, cmd_who)
    result = []
    if ok3 and out3.strip():
        for line in out3.strip().splitlines():
            line = line.strip()
            if ':' in line:
                uname, count = line.rsplit(':', 1)
                uname = uname.strip()
                if uname and uname != 'root':
                    try:
                        result.append({'username': uname, 'connections': int(count.strip())})
                    except ValueError:
                        pass
    return result


def count_user_connections_on_server(ip: str, port: int, auth_token: str, username: str) -> int:
    """Count active sessions for a specific user."""
    cmd = f"who 2>/dev/null | grep -c '^{username} ' || echo 0"
    ok, out = send_command(ip, port, auth_token, cmd)
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
    Returns list of (server_name, ok, msg) tuples.
    """
    server_ids = db_module.get_user_all_server_ids(user['id'])
    results = []
    for sid in server_ids:
        srv = db_module.get_server(sid)
        if not srv:
            continue
        ok, msg = renew_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            user['username'], days_for_server
        )
        results.append((srv['name'], ok, msg))
    return results


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
