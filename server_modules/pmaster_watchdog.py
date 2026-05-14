#!/usr/bin/python3
# -*- coding: utf-8 -*-
# pmaster_watchdog.py — Keep pmaster_module.py alive (cron every minute)
import os
import requests

def get_token():
    try:
        with open('/root/pmaster_module.py', 'r') as f:
            for line in f:
                if 'senha_autenticacao' in line and '=' in line:
                    return line.split('=')[1].strip().strip("'").strip('"')
    except Exception:
        pass
    return None

def ensure_cron():
    result = os.popen('crontab -l 2>/dev/null').read()
    if 'pmaster_watchdog.py' not in result:
        os.system('(crontab -l 2>/dev/null; echo "* * * * * python3 /root/pmaster_watchdog.py") | crontab -')
        os.system('systemctl restart cron 2>/dev/null || service cron restart 2>/dev/null || true')

def restart_module():
    os.system('pkill -f pmaster_module.py 2>/dev/null || true')
    os.system('nohup python3 /root/pmaster_module.py > /root/pmaster_module.log 2>&1 &')

ensure_cron()

token = get_token()
if token:
    try:
        resp = requests.post(
            'http://localhost:7277',
            headers={'Senha': token},
            data={'comando': 'echo ok'},
            timeout=5
        )
        if resp.status_code != 200:
            restart_module()
    except Exception:
        restart_module()
else:
    restart_module()
