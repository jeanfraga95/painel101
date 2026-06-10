#!/usr/bin/python3
# -*- coding: utf-8 -*-
# verificador.py — Keep modulo.py alive via cron (MELHORADO)

import os
import sys
import requests
import socket

# Configuração
MODULE_PORT = 7072
MODULE_PATH = '/root/modulo.py'
LOG_PATH = '/root/verificador.log'

def log(msg):
    """Escreve no log do verificador."""
    try:
        with open(LOG_PATH, 'a') as f:
            from datetime import datetime
            f.write(f"{datetime.now().isoformat()} - {msg}\n")
    except Exception:
        pass

def get_token():
    """Extrai o token de autenticação do modulo.py."""
    try:
        with open(MODULE_PATH, 'r') as f:
            content = f.read()
            # Procura por diferentes padrões de token
            patterns = [
                'senha_autenticacao',
                'AUTH_TOKEN',
                'auth_token',
                'TOKEN'
            ]
            for line in content.split('\n'):
                for pattern in patterns:
                    if pattern in line and '=' in line:
                        token = line.split('=')[1].strip().strip("'").strip('"')
                        if token and len(token) > 5:
                            log(f"Token encontrado via padrão '{pattern}'")
                            return token
            # Fallback: procura qualquer string entre aspas após 'token'
            import re
            match = re.search(r'[Tt]oken\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                log("Token encontrado via regex")
                return match.group(1)
    except Exception as e:
        log(f"Erro ao ler token: {e}")
    return None

def is_module_running():
    """Verifica se o processo modulo.py está rodando."""
    try:
        result = os.popen('pgrep -f "python3.*modulo.py"').read().strip()
        return bool(result)
    except Exception:
        return False

def is_port_open(port):
    """Verifica se a porta está ouvindo."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        return result == 0
    except Exception:
        return False

def check_module_health():
    """Verifica se o módulo está respondendo corretamente."""
    token = get_token()
    if not token:
        log("Token não encontrado")
        return False
    
    try:
        resp = requests.post(
            f'http://localhost:{MODULE_PORT}',
            headers={'Senha': token},
            data={'comando': 'echo ok'},
            timeout=5
        )
        if resp.status_code == 200:
            # Verifica se a resposta é 'ok' ou similar
            if resp.text.strip().lower() in ('ok', 'ok\n', 'pong'):
                log("Health check: OK")
                return True
            else:
                log(f"Resposta inesperada: {resp.text[:50]}")
                return False
        else:
            log(f"HTTP {resp.status_code}")
            return False
    except requests.exceptions.Timeout:
        log("Timeout na requisição")
        return False
    except requests.exceptions.ConnectionError:
        log("Erro de conexão")
        return False
    except Exception as e:
        log(f"Erro no health check: {e}")
        return False

def restart_module():
    """Reinicia o módulo."""
    log("*** REINICIANDO MODULO ***")
    # Mata processos existentes
    os.system('pkill -f "python3.*modulo.py" 2>/dev/null || true')
    os.system('pkill -f modulo.py 2>/dev/null || true')
    
    # Aguarda processo morrer
    import time
    time.sleep(1)
    
    # Inicia novo processo
    result = os.system(f'nohup python3 {MODULE_PATH} > /root/modulo.log 2>&1 &')
    if result == 0:
        log("Módulo reiniciado com sucesso")
        # Aguarda 2 segundos e verifica se subiu
        time.sleep(2)
        if is_port_open(MODULE_PORT):
            log("Porta aberta após reinício")
        else:
            log("ATENCAO: Porta não abriu após reinício")
    else:
        log(f"Falha ao reiniciar módulo (código: {result})")

def ensure_cron():
    """Garante que o verificador está no crontab."""
    cron_entry = "* * * * * python3 /root/verificador.py"
    result = os.popen('crontab -l 2>/dev/null').read()
    
    if 'verificador.py' not in result:
        log("Adicionando verificador ao crontab")
        new_cron = result.rstrip() + "\n" + cron_entry + "\n" if result else cron_entry + "\n"
        os.system(f'echo "{new_cron}" | crontab -')
        os.system('systemctl restart cron 2>/dev/null || service cron restart 2>/dev/null || true')
    else:
        # Verifica se a entrada está correta
        if cron_entry not in result:
            log("Cron entry existente mas diferente, atualizando")
            lines = [l for l in result.split('\n') if 'verificador.py' not in l]
            lines.append(cron_entry)
            new_cron = '\n'.join(lines) + '\n'
            os.system(f'echo "{new_cron}" | crontab -')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    log("=== Verificador executando ===")
    
    # 1. Garante que está no crontab
    ensure_cron()
    
    # 2. Verifica se o módulo está saudável
    if not check_module_health():
        log("Módulo não está saudável, reiniciando...")
        restart_module()
    else:
        # 3. Verificação extra: se a porta não está ouvindo mas o processo existe
        if not is_port_open(MODULE_PORT) and is_module_running():
            log("Processo existe mas porta fechada, reiniciando...")
            restart_module()
        elif not is_module_running():
            log("Processo não encontrado, iniciando...")
            restart_module()
        else:
            log("Módulo funcionando normalmente")
    
    log("=== Verificador finalizado ===")
