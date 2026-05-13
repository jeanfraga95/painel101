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
        cmd = f"/root/dragonmodule v2rayadd {uuid} {username} {password} {days} {limit}"
    else:
        cmd = f"/root/dragonmodule createssh {username} {password} {days} {limit}"
    return send_command(ip, port, auth_token, cmd)


def create_test_user_on_server(ip: str, port: int, auth_token: str,
                                username: str, password: str, minutes: int,
                                limit: int, uuid: str = None) -> tuple:
    if uuid:
        cmd = f"/root/dragonmodule v2rayaddteste {uuid} {username} {password} {minutes} {limit}"
    else:
        cmd = f"/root/dragonmodule createsshteste {username} {password} {minutes} {limit}"
    return send_command(ip, port, auth_token, cmd)


def delete_user_on_server(ip: str, port: int, auth_token: str,
                           username: str, uuid: str = None) -> tuple:
    if uuid:
        cmd = f"/root/dragonmodule v2raydel {uuid} {username}"
    else:
        cmd = f"/root/dragonmodule removessh {username}"
    return send_command(ip, port, auth_token, cmd)


def renew_user_on_server(ip: str, port: int, auth_token: str,
                          username: str, days: int) -> tuple:
    cmd = f"/root/dragonmodule timedata {username} {days}"
    return send_command(ip, port, auth_token, cmd)


def sync_users_to_server(ip: str, port: int, auth_token: str, users: list) -> tuple:
    """Sync a list of SSH users to the server via sincronizar approach."""
    lines = []
    for u in users:
        if u['v2ray_uuid']:
            from datetime import datetime
            try:
                exp = datetime.fromisoformat(u['expires_at'])
                days_left = max(0, (exp - datetime.now()).days)
            except Exception:
                days_left = 30
            lines.append(f"{u['username']} {u['password']} {days_left} {u['connection_limit']} {u['v2ray_uuid']}")
        else:
            from datetime import datetime
            try:
                exp = datetime.fromisoformat(u['expires_at'])
                days_left = max(0, (exp - datetime.now()).days)
            except Exception:
                days_left = 30
            lines.append(f"{u['username']} {u['password']} {days_left} {u['connection_limit']}")

    content = '\n'.join(lines)
    tmp_file = '/tmp/sync_painel.txt'
    # Write file to server then run sincronizar
    escaped = content.replace("'", "'\\''")
    cmd = f"echo '{escaped}' > {tmp_file} && cd /root && python3 sincronizar.py {tmp_file}"
    return send_command(ip, port, auth_token, cmd)


def install_modules_ssh(ip: str, root_user: str, root_password: str, auth_token: str) -> tuple:
    """Use paramiko to upload module files to the SSH server."""
    modules_dir = os.path.join(os.path.dirname(__file__), 'server_modules')
    files_to_upload = [
        ('modulo.py', '/root/modulo.py'),
        ('dragonmodule', '/root/dragonmodule'),
        ('sincronizar.py', '/root/sincronizar.py'),
        ('delete.py', '/root/delete.py'),
        ('verificador.py', '/root/verificador.py'),
    ]

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=root_user, password=root_password, timeout=20)
        sftp = ssh.open_sftp()

        for local_name, remote_path in files_to_upload:
            local_path = os.path.join(modules_dir, local_name)
            if os.path.exists(local_path):
                # Replace auth token in modulo.py
                if local_name == 'modulo.py':
                    with open(local_path, 'r') as f:
                        content = f.read()
                    content = content.replace('REPLACE_AUTH_TOKEN', auth_token)
                    sftp.putfo(io.BytesIO(content.encode()), remote_path)
                else:
                    sftp.put(local_path, remote_path)

        # Make dragonmodule executable, start modulo.py, set up cron
        commands = [
            "chmod +x /root/dragonmodule",
            "chmod +x /root/modulo.py",
            "mkdir -p /etc/SSHPlus/senha /etc/DragonPanel",
            "touch /root/usuarios.db",
            "pkill -f modulo.py || true",
            "nohup python3 /root/modulo.py > /root/modulo.log 2>&1 &",
            # Cron to keep modulo.py alive
            "(crontab -l 2>/dev/null | grep -v verificador.py; echo '* * * * * python3 /root/verificador.py') | crontab -",
        ]
        for cmd in commands:
            ssh.exec_command(cmd)

        sftp.close()
        ssh.close()
        return True, 'Módulos instalados com sucesso'
    except Exception as e:
        return False, str(e)
