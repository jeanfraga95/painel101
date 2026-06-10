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


def send_command(ip: str, port: int, auth_token: str, command: str) -> tuple:
    """Send a shell command to modulo.py on the SSH server.
    Returns (success: bool, output: str).
    """
    url = f"http://{ip}:{port}"
    try:
        resp = requests.post(
            url,
            headers={'Senha': auth_token},
            data={'comando': command},
            timeout=TIMEOUT
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


def sync_users_to_server(ip: str, port: int, auth_token: str, users: list) -> tuple:
    """Sync users to server.
    Usa base64 para enviar o arquivo de sync, evitando problemas de escaping
    com senhas que contenham caracteres especiais ($, !, ', espaço, etc.).
    Se o script bulk falhar, cria cada usuário individualmente como fallback.
    """
    import base64 as _b64
    from datetime import datetime as _dt2

    if not users:
        return True, '0 usuários para sincronizar'

    lines = []
    for u in users:
        try:
            exp = _dt2.fromisoformat(u['expires_at'])
            days_left = max(1, (exp - _dt2.now()).days)
        except Exception:
            days_left = 30
        if u.get('v2ray_uuid'):
            lines.append(
                f"{u['username']} {u['password']} {days_left} {u['connection_limit']} {u['v2ray_uuid']}")
        else:
            lines.append(
                f"{u['username']} {u['password']} {days_left} {u['connection_limit']}")

    content = '\n'.join(lines)

    # Codifica em base64 — evita qualquer problema de escaping no shell
    # (senhas com $, !, ', aspas, espaços não quebram mais o comando)
    content_b64 = _b64.b64encode(content.encode()).decode()
    cmd = (
        f"echo '{content_b64}' | base64 -d > /tmp/pmg_sync.txt && "
        f"(python3 /root/pmaster_sync.py /tmp/pmg_sync.txt 2>/dev/null || "
        f"python3 /root/sincronizar.py /tmp/pmg_sync.txt 2>/dev/null) && "
        f"echo 'PMG_OK:{len(lines)}'"
    )
    ok, out = send_command(ip, port, auth_token, cmd)
    if ok and 'PMG_OK' in out:
        return True, f'{len(lines)} usuários sincronizados'

    # ── Fallback: cria cada usuário individualmente ────────────────────────
    # Ativado quando pmaster_sync.py não existe no servidor ou falha
    created, fail_list = 0, []
    for u in users:
        try:
            exp = _dt2.fromisoformat(u['expires_at'])
            days_left = max(1, (exp - _dt2.now()).days)
        except Exception:
            days_left = 30
        ok2, _ = create_ssh_user_on_server(
            ip, port, auth_token,
            u['username'], u['password'], days_left,
            u['connection_limit'], u.get('v2ray_uuid')
        )
        if ok2:
            created += 1
        else:
            fail_list.append(u['username'])

    if created > 0:
        msg = f"{created}/{len(users)} usuários criados"
        if fail_list:
            msg += f" ({len(fail_list)} falha(s): {', '.join(fail_list[:5])})"
        return True, msg

    return False, out or 'Falha: módulo não encontrado no servidor. Instale os módulos primeiro.'


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
    for monitor_bin in ('/opt/pmg-monitor', '/opt/sshplus/plugin-sync'):
        ok, out = send_command(ip, port, auth_token,
                               f"{monitor_bin} --monitor-users 2>/dev/null")
        if ok and out.strip().startswith('{'):
            try:
                data = _json.loads(out.strip())
                result = [
                    {'username': u, 'connections': int(c)}
                    for u, c in data.items()
                    if u and u not in ('root', 'sshd', '')
                ]
                if result:
                    return result
            except Exception:
                pass

    # ── Method 2: ps -eo args (user's script, modified to output user:count) ─
    # Based on: ps -eo args | grep "sshd:" | grep -v "grep" | grep -v "\["
    #           | awk -F'[: ]+' '/sshd:/ {print $2}' | sort | uniq -c
    cmd = (
        "ps -eo args 2>/dev/null | grep 'sshd:' | grep -v grep | grep -v '\\[' | "
        "awk -F'[: ]+' '/sshd:/ {print $2}' | "
        "grep -v '^root$' | grep -v '^sshd$' | grep -v '^$' | "
        "sort | uniq -c | awk '{print $2 \":\" $1}'"
    )
    ok, out = send_command(ip, port, auth_token, cmd)
    result = []
    if ok and out.strip():
        for line in out.strip().splitlines():
            line = line.strip()
            if ':' in line:
                uname, count = line.rsplit(':', 1)
                uname = uname.strip()
                if uname and uname not in ('root', 'sshd', ''):
                    try:
                        result.append({'username': uname, 'connections': int(count.strip())})
                    except ValueError:
                        pass
        if result:
            return result

    # ── Method 3: who fallback ─────────────────────────────────────────────
    cmd_who = (
        "who 2>/dev/null | awk '{print $1}' | grep -v '^root$' | "
        "sort | uniq -c | awk '{print $2 \":\" $1}'"
    )
    ok3, out3 = send_command(ip, port, auth_token, cmd_who)
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
