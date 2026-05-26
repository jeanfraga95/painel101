#!/usr/bin/python3
# -*- coding: utf-8 -*-
# verificador.py — Keep modulo.py alive via cron

import os
import requests

def get_token():
    try:
        with open('/root/modulo.py', 'r') as f:
            for line in f:
                if 'senha_autenticacao' in line and '=' in line:
                    return line.split('=')[1].strip().strip("'").strip('"')
    except Exception:
        pass
    return None

def ensure_cron():
    result = os.popen('crontab -l 2>/dev/null').read()
    if 'verificador.py' not in result:
        os.system('(crontab -l 2>/dev/null; echo "* * * * * python3 /root/verificador.py") | crontab -')
        os.system('systemctl restart cron 2>/dev/null || service cron restart 2>/dev/null || true')

def restart_modulo():
    os.system('pkill -f modulo.py 2>/dev/null || true')
    os.system('nohup python3 /root/modulo.py > /root/modulo.log 2>&1 &')

ensure_cron()

token = get_token()
if token:
    try:
        resp = requests.post(
            'http://localhost:6969',
            headers={'Senha': token},
            data={'comando': 'echo ok'},
            timeout=5
        )
        if resp.status_code != 200:
            restart_modulo()
    except Exception:
        restart_modulo()
else:
    restart_modulo()
