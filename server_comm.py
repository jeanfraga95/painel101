#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_comm.py — Communicate with modulo.py running on SSH servers
"""

import requests
import paramiko
import io
import os
import time


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


def get_user_connections(ip: str, port: int, auth_token: str, username: str,
                         timeout: int = 4) -> int:
    """Count active sessions for a given SSH user.

    `who` não serve aqui: os usuários são túneis puros (ForceCommand
    /bin/false) e não registram utmp, então sempre retornava 0.
    """
    ok, out = send_command(ip, port, auth_token, _sessions_cmd_for_user(username),
                           timeout=timeout)
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



def diagnose_server(ip: str, port: int, auth_token: str,
                    root_user: str, root_password: str) -> dict:
    """Roda uma checagem de ponta a ponta no servidor.

    Cobre os problemas mais comuns relatados:
      - módulo não instalado / fora do ar / porta errada;
      - usuário criado no painel mas que não chega na VPS (token inválido,
        agent ausente, serviço parado);
      - agent desatualizado (sem limpeza de órfãos) que faz o `useradd`
        falhar com "group exists" enquanto o painel reporta sucesso falso;
      - grupos órfãos / contas fantasma que bloqueiam recriações de usuário;
      - disco cheio, watchdog ausente, múltiplos processos do módulo.

    Returns {'checks': [{'ok', 'name', 'detail'}], 'ssh': bool, 'module': bool}
    """
    checks = []

    def add(name, ok, detail, warn=False):
        checks.append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:420],
                       'warn': bool(warn)})

    def _is_y(out):
        lines = (out or '').strip().splitlines()
        return bool(lines) and lines[-1] == 'Y'

    # 1) Módulo HTTP na porta configurada (auth por header 'Senha')
    mod_ok, mod_out = send_command(ip, port, auth_token, 'echo PMG_DIAG_OK', timeout=15)
    if mod_out.startswith('HTTP 401') or '401' in mod_out:
        add('Módulo (HTTP {})'.format(port), False,
            'Módulo está respondendo mas rejeitou o token (401). '
            'O token salvo no painel não corresponde ao do servidor — '
            'execute "Corrigir" para reinstalar o módulo com o token certo.')
    else:
        add('Módulo (HTTP {})'.format(port), mod_ok,
            mod_out if not mod_ok else 'Respondendo e autenticado.')

    # 2) SSH root
    ssh_ok = False
    ssh = None
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=root_user, password=root_password, timeout=20)
        ssh_ok = True
        add('SSH ({}@{})'.format(root_user, ip), True, 'Conectado.')
    except Exception as e:
        add('SSH ({}@{})'.format(root_user, ip), False,
            'Falha na conexão SSH: {}'.format(e))

    if not (ssh_ok and ssh):
        return {'checks': checks, 'ssh': False, 'module': mod_ok}

    # 3) agent presente, sintaxe válida e com a limpeza de órfãos (V2)
    _, out, _ = _ssh_run(ssh, 'test -f /root/pmaster_agent && echo Y || echo N')
    agent_present = _is_y(out)
    _, out2, _ = _ssh_run(ssh, 'bash -n /root/pmaster_agent 2>&1 && echo SYNTAX_OK || echo SYNTAX_ERR')
    syntax_ok = 'SYNTAX_OK' in (out2 or '')
    _, out3, _ = _ssh_run(ssh,
        "grep -q 'PMG_AGENT_V2_ORPHANCLEAN' /root/pmaster_agent 2>/dev/null && echo Y || echo N")
    agent_v2 = _is_y(out3)
    if agent_present and syntax_ok and agent_v2:
        add('Agent /root/pmaster_agent', True,
            'Presente, sintaxe válida e com limpeza de órfãos (V2).')
    elif agent_present and syntax_ok:
        add('Agent /root/pmaster_agent', False,
            'Presente e sem erro de sintaxe, MAS é a versão antiga (sem a '
            'limpeza de órfãos que evita "useradd: group exists"). '
            'Rodar "Corrigir" resolve.')
    elif agent_present:
        add('Agent /root/pmaster_agent', False,
            'Presente mas com erro de sintaxe: {}'.format((out2 or '').strip()[:200]))
    else:
        add('Agent /root/pmaster_agent', False,
            'Não encontrado em /root/pmaster_agent. É preciso instalar os módulos.')

    # 4) módulo no disco + serviço ativo
    _, out, _ = _ssh_run(ssh, 'test -f /root/pmaster_module.py && echo Y || echo N')
    mod_present = _is_y(out)
    _, out, _ = _ssh_run(ssh, 'systemctl is-active pmaster-agent 2>/dev/null || echo inactive')
    svc_state = (out.strip() or 'inactive').splitlines()[-1].strip()
    if mod_present and svc_state == 'active':
        add('Serviço pmaster-agent', True, 'Ativo e módulo no disco.')
    elif mod_present:
        add('Serviço pmaster-agent', False,
            'Módulo existe mas o serviço está "{}".'.format(svc_state))
    else:
        add('Serviço pmaster-agent', False,
            'Módulo ausente e serviço "{}".'.format(svc_state))

    # 5) módulo aceita autenticação por campo (token/senha), além do header
    if mod_present and svc_state == 'active':
        probe = (f"curl -s -m 6 -d 'comando=echo PMG_FIELD_OK&token={auth_token}' "
                 f"http://127.0.0.1:{port}/ 2>&1 | head -c 80")
        _, out, _ = _ssh_run(ssh, probe)
        if 'PMG_FIELD_OK' in (out or ''):
            add('Autenticação do módulo', True,
                'Aceita header "Senha" e campo token/senha (V2).')
        else:
            add('Autenticação do módulo', False,
                'Aceita só o header "Senha" (versão antiga). Clientes que '
                'enviam o token por campo recebem 401 e o usuário não nasce '
                'na VPS. Rodar "Corrigir" resolve.')

    # 6) porta do módulo escutando
    _, out, _ = _ssh_run(ssh, f"ss -ltn 2>/dev/null | grep -E ':{port}\\b' || echo NONE")
    listening = 'NONE' not in (out or '')
    add('Porta :{} escutando'.format(port), listening,
        (out or '').strip().replace('\n', ' | ') if listening else
        'Nada escutando na porta {}.'.format(port))
    if listening:
        _, out, _ = _ssh_run(ssh, "ps aux | grep -c '[p]master_module.py'")
        procs = 0
        try:
            procs = int(out.strip().splitlines()[-1])
        except Exception:
            procs = 0
        if procs > 1:
            add('Processos do módulo', False,
                '{} processos pmaster_module.py em paralelo — a porta pode '
                'ficar instável. Reiniciar o serviço resolve.'.format(procs))

    # 7) grupos órfãos — bloqueiam o useradd com "group exists"
    orphan_cmd = (
        "while IFS=: read -r gn gt gid adm mem; do"
        "  [ \"$gid\" -ge 1000 ] || continue;"
        "  [ \"$gn\" = \"pmg_vpn\" ] && continue;"
        "  if grep -sq \"^[^:]*:[^:]*:$gid:\" /etc/passwd; then continue; fi;"
        "  [ -n \"$mem\" ] && continue;"
        "  echo \"$gn\";"
        "done < /etc/gshadow"
    )
    _, out, _ = _ssh_run(ssh, orphan_cmd)
    orphans = [l.strip() for l in (out or '').splitlines() if l.strip()]
    if orphans:
        add('Grupos órfãos', False,
            '{} grupo(s) sem dono/membros que farão o `useradd` falhar com '
            '"group exists": {}. O agent V2 limpa sozinho na criação; se o '
            'agent está antigo, use "Corrigir" e recrie o usuário.'.format(
                len(orphans), ', '.join(orphans[:8])))
    else:
        add('Grupos órfãos', True, 'Nenhum grupo órfão (gid>=1000) encontrado.')

    # 8) usuários em /etc/shadow sem entrada em /etc/passwd (fantasmas)
    ghost_cmd = (
        "comm -23 <(awk -F: '{print $1}' /etc/shadow | sort) "
        "<(awk -F: '{print $1}' /etc/passwd | sort) | head -6"
    )
    _, out, _ = _ssh_run(ssh, ghost_cmd)
    ghost = [l.strip() for l in (out or '').splitlines() if l.strip()]
    if ghost:
        add('Contas fantasma (shadow)', True,
            '{} entrada(s) em /etc/shadow sem correspondência no passwd: {}. '
            'Não bloqueiam o useradd, mas o agent V2 remove ao recriar.'.format(
                len(ghost), ', '.join(ghost)),
            warn=True)
    else:
        add('Contas fantasma (shadow)', True, 'Shadow e passwd consistentes.')

    # 9) disco — / cheio impede criar senha/home.
    #    Em VPS com script de download (ex.: vídeos p/ Telegram) o uso alto é
    #    esperado; só vira falha quando realmente não sobra espaço.
    _, out, _ = _ssh_run(ssh, "df -P / | awk 'NR==2{print $5}' | tr -d '%'")
    disk_pct = 0
    try:
        disk_pct = int((out or '').strip().splitlines()[-1] or 0)
    except Exception:
        pass
    _, out_av, _ = _ssh_run(ssh, "df -P / | awk 'NR==2{print $4}'")
    avail_mb = 0
    try:
        avail_mb = int((out_av or '').strip().splitlines()[-1] or 0)
    except Exception:
        pass
    if disk_pct >= 99 or avail_mb < 300:
        add('Espaço em disco', False,
            'Partição raiz a {}% ({}) — crítico: pode impedir criar '
            'usuários/home.'.format(disk_pct, disk_pct))
    elif disk_pct >= 85:
        add('Espaço em disco', True,
            '{}% em uso — alerta. Pode ser atividade esperada da VPS '
            '(ex.: gravação de vídeos), mas fique de olho se chegar a '
            '99%.'.format(disk_pct),
            warn=True)
    else:
        add('Espaço em disco', True, '{}% em uso.'.format(disk_pct))

    # 10) sync/watchdog presentes e cron do watchdog ativo
    _, out, _ = _ssh_run(ssh,
        'test -f /root/pmaster_sync.py && test -f /root/pmaster_watchdog.py && echo Y || echo N')
    sync_present = _is_y(out)
    _, out, _ = _ssh_run(ssh, 'crontab -l 2>/dev/null | grep -c pmaster_watchdog || echo 0')
    cron_count = (out or '').strip().splitlines()[-1].strip()
    if sync_present and cron_count not in ('0', ''):
        add('Sync / watchdog', True, 'Presentes; watchdog no cron ({}x).'.format(cron_count))
    else:
        add('Sync / watchdog', False,
            'sync: {}, watchdog no cron: {}. Rodar "Corrigir" resolve.'.format(
                'presente' if sync_present else 'ausente',
                cron_count if cron_count not in ('', '0') else 'falta'))

    return {'checks': checks, 'ssh': True, 'module': mod_ok}


def _vps_avail_mb(ip: str, port: int, auth_token: str) -> int:
    """Espaço livre (MB) na partição raiz da VPS via módulo."""
    ok, out = send_command(ip, port, auth_token,
                           "df -P / | awk 'NR==2{print $4}'", timeout=30)
    if not ok:
        return None
    try:
        return max(0, int((out or '').strip().splitlines()[-1]))
    except Exception:
        return None


def cleanup_vps(ip: str, port: int, auth_token: str, wait: int = 25) -> tuple:
    """Libera espaço na VPS de forma segura, sem tocar nos dados do usuário:
    - apt-get clean / autoclean (cache de pacotes baixados)
    - journald contido em 120M
    - logs rotacionados (.gz/.1/.2/.old) com +2 dias removidos
    - logs grandes (+100M) com +1 dia truncados
    - /tmp: arquivos com +1 dia removidos e diretórios vazios
    Não mexe em /home, /root/vod e derivados.

    Roda de forma ASSÍNCRONA (nohup na VPS) para o módulo HTTP não derrubar
    a conexão em comandos demorados; aqui só fica esperando o processo
    terminar (máx `wait` segundos) e mede o espaço liberado.

    Retorna (success, info). info = {before_mb, after_mb, freed_mb, detail}.
    """
    before_mb = _vps_avail_mb(ip, port, auth_token)
    inner = (
        "apt-get clean 2>/dev/null; apt-get autoclean 2>/dev/null; "
        "journalctl --vacuum-size=120M >/dev/null 2>&1; "
        "find /var/log -type f -name '*.gz' -mtime +2 -delete 2>/dev/null; "
        "find /var/log -type f -name '*.1' -mtime +2 -delete 2>/dev/null; "
        "find /var/log -type f -name '*.2' -mtime +2 -delete 2>/dev/null; "
        "find /var/log -type f -name '*.old' -mtime +2 -delete 2>/dev/null; "
        "find /var/log -type f -size +100M -mtime +1 -exec truncate -s 0 {} + 2>/dev/null; "
        "find /tmp -type f -mtime +1 -delete 2>/dev/null; "
        "find /tmp -type d -empty -depth -delete 2>/dev/null"
    )
    launch = (
        "nohup sh -c '{0}' >/tmp/pmg_cleanup.log 2>&1 & "
        "echo $! > /tmp/pmg_cleanup.pid; echo LANCADO".format(inner)
    )
    ok, out = send_command(ip, port, auth_token, launch, timeout=30)
    if not ok or 'LANCADO' not in (out or ''):
        info = {'before_mb': before_mb, 'after_mb': None, 'freed_mb': None,
                'detail': out or 'falha ao lançar limpeza'}
        return False, info

    # aguarda o processo de limpeza terminar
    for _ in range(max(1, wait)):
        time.sleep(1)
        okc, outc = send_command(
            ip, port, auth_token,
            "p=$(cat /tmp/pmg_cleanup.pid 2>/dev/null); "
            "[ -n \"$p\" ] && kill -0 \"$p\" 2>/dev/null && echo RODANDO || echo PRONTO",
            timeout=20)
        if outc and 'RODANDO' not in outc:
            break

    after_mb = _vps_avail_mb(ip, port, auth_token)
    freed_mb = None
    if before_mb is not None and after_mb is not None:
        freed_mb = max(0, after_mb - before_mb)
    okd, outd = send_command(ip, port, auth_token,
                             "cat /tmp/pmg_cleanup.log 2>/dev/null | head -40",
                             timeout=20)
    info = {
        'before_mb': before_mb,
        'after_mb': after_mb,
        'freed_mb': freed_mb,
        'detail': outd or '',
    }
    return True, info


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
            # Backup antes de mexer e rollback se a config ficar inválida:
            # sshd quebrado nesse servidor significa perder o acesso.
            "cp -a /etc/ssh/sshd_config /etc/ssh/sshd_config.pre-reaper",
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
            # Valida antes de recarregar; se ficar inválida, restaura o backup
            (
                "sshd -t 2>/dev/null || "
                "{ cp -a /etc/ssh/sshd_config.pre-reaper /etc/ssh/sshd_config && "
                "echo 'sshd_config invalido, backup restaurado'; }"
            ),
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
        # Executa toda a configuração em UM único canal SSH, com retry.
        # Em servidores com muitas sessões abertas (proxy com milhares de
        # túneis), abrir um canal por comando estoura o limite de
        # MaxSessions do sshd e o setup inteiro falha com
        # "ChannelException(2, 'Connect failed')".
        script = 'set +e\n' + '\n'.join(commands)
        for attempt in range(3):
            try:
                _ssh_run(ssh, script)
                break
            except Exception as channel_err:
                if attempt >= 2:
                    sftp.close()
                    ssh.close()
                    return (False,
                            'Falha ao executar o setup no servidor: {}'.format(channel_err))
                time.sleep(2)

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

    def _create(srv):
        return create_ssh_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            user['username'], user['password'], days_for_server,
            user['connection_limit'], uuid=uuid
        )

    results = []
    for sid in server_ids:
        srv = db_module.get_server(sid)
        if not srv:
            continue
        ok, msg = _create(srv)
        results.append({'ok': ok, 'msg': msg, 'srv': srv})

    # 1 tentativa extra nos servidores que falharam (rede/transição)
    for r in [r for r in results if not r['ok']]:
        ok, msg = _create(r['srv'])
        r['ok'], r['msg'] = ok, msg

    return [(r['srv']['name'], r['ok'], r['msg']) for r in results]


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


VNC_WEB_UNIT = """[Unit]
Description=Painel Master VNC web (NoVNC via websockify)
# Expõe a saida do x11vnc em HTTP/WebSocket para o painel web, para que o
# navegador abra o console sem cliente VNC instalado. O acesso e controlado
# no painel; a porta so aceita conexao do IP do painel.
After=network-online.target pmaster-vnc.service
Wants=network-online.target
Requires=pmaster-vnc.service

[Service]
Type=simple
ExecStart={websockify_bin} --web {novnc_dir} 0.0.0.0:6080 127.0.0.1:5900
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

VNC_FIREWALL_SCRIPT = """#!/bin/sh
# Firewall do console VNC do Painel Master.
#
# Por padrao as portas ficam restritas ao IP do painel: o acesso normal e
# pelo navegador, atraves do painel, e o 5900/6080 nao aceitam mais ninguem.
# "open" libera para a internet, para quando for preciso entrar com um
# cliente VNC externo (RealVNC, TigerVNC) de uma maquina que nao e o painel.
#
# Uso: pmaster-vnc-firewall restrict|open|status
#
# Por que restringir por padrao: o x11vnc usa VNC auth classica, que e DES
# de 56 bits e aceita senha de 8 caracteres. Com a porta 5900 aberta na
# internet isso e quebravel por forca bruta, e o root do servidor e a
# recompensa. Por isso o padrao e fechar e o acesso normal ser pelo painel.

PANEL_IP="${PM_PANEL_IP:-}"
PORTS="5900,6080"
CHAIN=PMVNC
MODEFILE=/etc/pmaster-vnc/firewall-mode

chain_exists() { iptables -n -L "$CHAIN" >/dev/null 2>&1; }

is_ipv4() {
    case "$1" in
        ''|*[!0-9.]*) return 1 ;;
        *.*.*.*) ;;
        *) return 1 ;;
    esac
    _a=$(echo "$1" | cut -d. -f1)
    _b=$(echo "$1" | cut -d. -f2)
    _c=$(echo "$1" | cut -d. -f3)
    _d=$(echo "$1" | cut -d. -f4)
    for _o in "$_a" "$_b" "$_c" "$_d"; do
        [ -n "$_o" ] || return 1
        [ "$_o" -ge 0 ] 2>/dev/null && [ "$_o" -le 255 ] 2>/dev/null || return 1
    done
    return 0
}

# Apaga TODOS os saltos para a cadeia, em qualquer formato, e instala um so.
#
# Nao basta remover o que coincide com a regra nova. Versoes anteriores do
# painel gravavam o salto como "-j PMVNC" simples, sem multiport, e essa regra
# casa com QUALQUER pacote de entrada, nao so com 5900/6080. Ainda hoje, o
# salto fica no topo do INPUT: sobrando ele, todo trafego que chega de fora
# cai na cadeia e termina no DROP -- inclusive o SSH. Quem fizesse o upgrade
# perderia o acesso a maquina.
#
# Por isso a limpeza e por varredura: qualquer linha da INPUT que pule para a
# cadeia e removida, e so depois entra a regra correta, com as portas.
ensure_jump() {
    iptables -S INPUT 2>/dev/null | grep -- "-j $CHAIN" | while read -r _line; do
        _del=$(echo "$_line" | sed "s/^-A INPUT/-D INPUT/")
        iptables $_del 2>/dev/null
    done
    iptables -I INPUT -p tcp -m multiport --dports "$PORTS" -j "$CHAIN" || return 1
    return 0
}

apply() {
    mode="$1"
    mkdir -p /etc/pmaster-vnc

    # Validar ANTES de tocar em qualquer regra. A cadeia e esvaziada logo
    # abaixo; se a validacao viesse depois, um "open" sem IP do painel
    # recusado deixaria a cadeia vazia -- ou seja, a porta sem filtro --
    # enquanto o arquivo de modo continuaria dizendo "restrict". Recusar
    # precisa nao ter efeito colateral.
    if [ "$mode" = "open" ] && ! is_ipv4 "$PANEL_IP"; then
        echo "modo open exige o IP do painel (PM_PANEL_IP); recusado para nao abrir sem filtro" >&2
        return 1
    fi

    chain_exists || iptables -N "$CHAIN" || return 1
    ensure_jump || return 1
    iptables -F "$CHAIN"
    case "$mode" in
        open)
            iptables -A "$CHAIN" -s "$PANEL_IP" -j ACCEPT
            iptables -A "$CHAIN" -j ACCEPT
            ;;
        restrict)
            # loopback: o proprio websockify fala com o x11vnc em 127.0.0.1
            iptables -A "$CHAIN" -s 127.0.0.1 -j ACCEPT
            if is_ipv4 "$PANEL_IP"; then
                iptables -A "$CHAIN" -s "$PANEL_IP" -j ACCEPT
            fi
            iptables -A "$CHAIN" -j DROP
            ;;
        *)
            echo "modo invalido: $mode" >&2
            return 1
            ;;
    esac
    echo "$mode" > "$MODEFILE"
    return 0
}

case "${1:-}" in
    restrict|open)
        if apply "$1"; then
            echo "$1"
            exit 0
        fi
        echo "falha ao aplicar regra de firewall" >&2
        exit 1
        ;;
    status)
        if [ -f "$MODEFILE" ]; then
            echo -n "modo: "; cat "$MODEFILE"
        else
            echo "modo: desconhecido (nunca aplicado)"
        fi
        echo "portas: $PORTS"
        echo "regras:"
        iptables -S "$CHAIN" 2>/dev/null | sed 's/^/  /' || echo "  cadeia $CHAIN inexistente"
        exit 0
        ;;
    *)
        echo "uso: $0 restrict|open|status" >&2
        exit 2
        ;;
esac
"""

VNC_FIREWALL_UNIT = """[Unit]
Description=Aplica as regras de firewall do console VNC do Painel Master
# As regras do iptables nao sobrevivem a reboot, entao reaplica o ultimo modo
# usado assim que a rede sobe. Sem isso o servidor voltaria com 5900/6080
# abertos para a internet, que era o estado antes de existirem estas regras.
After=network-online.target
Wants=network-online.target
Before=pmaster-vnc-web.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'M=$(cat /etc/pmaster-vnc/firewall-mode 2>/dev/null || echo restrict); /usr/local/sbin/pmaster-vnc-firewall $M >/dev/null'

[Install]
WantedBy=multi-user.target
"""

def _ssh_run(ssh, cmd, timeout=300):
    """Executa um comando remoto aguardando o fim, devolvendo (rc, stdout, stderr).

    A versao anterior usava ssh.exec_command() sem ler stdout/stderr, o que
    descartava o codigo de saida e nunca aguardava a instalacao dos pacotes.
    """
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def _panel_ip_from_ssh(ssh) -> str:
    """Descobre o IP do painel pelo proprio SSH, sem configuracao previa.

    A conexao que instala o console vem do painel, entao o servidor ja sabe de
    onde ela veio. Isso evita fixar o IP do painel no codigo, que quebraria em
    qualquer outra VPS, e evita depender de env var ou de detectar IP publico
    por servico externo.
    """
    _, out, _ = _ssh_run(ssh, 'echo ${SSH_CONNECTION%% *}')
    cand = out.strip().splitlines()[0].strip() if out.strip() else ''
    parts = cand.split('.')
    if len(parts) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in parts):
        return cand
    return ''


def _wait_vnc_port(ssh, port, timeout=45):
    """Espera a porta do VNC abrir de fato, em vez de checar uma unica vez.

    Checava com 4 segundos de espera fixa logo apos o restart, e o x11vnc
    ainda nao tinha feito bind: dava falso 'CLOSED' mesmo com o servico
    ativo, o que escondia as credenciais do painel.
    """
    cmd = (
        f"for i in $(seq 1 {int(timeout)}); do "
        f"  ss -tulnH 2>/dev/null | awk '{{print $5}}' | grep -qE '[:.]{port}$' && echo OPEN && exit 0; "
        f"  sleep 1; "
        f"done; echo CLOSED"
    )
    _, out, _ = _ssh_run(ssh, cmd, timeout=timeout + 15)
    return out.strip().splitlines()[-1].strip().upper() if out.strip() else 'CLOSED'


VNC_START_SCRIPT = r"""#!/bin/sh
# Console de recuperacao do Painel Master (Xvfb + x11vnc)
# Gerado por server_comm.setup_vnc_ssh - nao editar a mao.
export DISPLAY=:99
PASSFILE=/etc/pmaster-vnc/pass

# Paleta escura: o console e para recuperar um servidor que caiu, e a maior
# parte da tela ficava branca — o xterm vem com fundo branco por padrao e o
# desktop do fluxbox cinza claro. Alem de feio, texto claro sobre fundo claro
# e ruim de ler numa tela pequena. These colors are also what the browser
# NoVNC chrome uses, so the two do not clash.
FONTE=Monospace
TAM=13
FUNDO='#0d1117'
TEXTO='#c9d1d9'
CURSOR='#58a6ff'

pkill -x x11vnc 2>/dev/null
pkill -f 'Xvfb :99' 2>/dev/null
sleep 1
rm -f /tmp/.X99-lock

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp -ac &
XVFB_PID=$!

# espera o X ficar pronto antes de tentar falar com ele
i=0
while [ $i -lt 30 ]; do
    xdpyinfo -display :99 >/dev/null 2>&1 && break
    kill -0 $XVFB_PID 2>/dev/null || break
    i=$((i + 1))
    sleep 1
done

# O gerenciador de janelas continua: sem ele quem perde o foco de teclado e o
# console de recuperacao fica inutil, porque o usuario digita e nada acontece.
# Mas a barra de titulo do fluxbox entra como uma faixa clara no meio da tela,
# entao a decoracao vai desligada e o fundo da area de trabalho tambem.
if command -v fluxbox >/dev/null 2>&1; then
    mkdir -p /root/.fluxbox
    cat > /root/.fluxbox/init <<FLUXEOF
session.screen0.defaultDeco: NONE
session.screen0.rootColor: $FUNDO
session.screen0.workspaces: 1
session.screen0.focusModel: FocusFollowsMouse
session.menuFile: /dev/null
FLUXEOF
    fluxbox >/dev/null 2>&1 &
    sleep 1
fi

# xsetroot depois do fluxbox: o gerenciador pinta a area de trabalho ao subir
# e sobrescreveria a cor, entao a ultima palavra e nossa.
if command -v xsetroot >/dev/null 2>&1; then
    xsetroot -display :99 -solid "$FUNDO" 2>/dev/null
fi

# Terminal de recuperacao, senao nao ha como digitar comando nenhum.
# -b/-bw 0 remove as bordas internas e externas do xterm, que sozinhas ja
# desenham um retangulo claro em volta do conteudo.
# ATENCAO: a geometria do xterm e em CELULAS, nao em pixels. Um -geometry como
# 1276x796 num Xvfb de 1280x800 faz o TUI (menus do vpnmanager, `opencode` etc.)
# achar que tem 1276 colunas e desenhar o menu fora da area visivel — por isso o
# menu "nao aparecia". Use uma grade que caiba na tela com a fonte de 13px:
# ~150 colunas x 44 linhas. (correcao VNC fixa)
if command -v xterm >/dev/null 2>&1; then
    xterm -display :99 -geometry 150x44+2+2 \
          -fa "$FONTE" -fs "$TAM" \
          -bg "$FUNDO" -fg "$TEXTO" -cr "$CURSOR" \
          -b 0 -bw 0 -b 0 \
          -T "Recuperacao - $(hostname)" \
          -e env TERM=xterm-256color COLORTERM=truecolor \
                PS1='\[\e[38;5;39m\]\u@\h\[\e[0m\]:\[\e[38;5;245m\]\w\[\e[0m\]\$ ' \
                /bin/bash -l &
fi

exec x11vnc -display :99 -forever -shared -rfbport {port} \
     -rfbauth $PASSFILE -listen 0.0.0.0 -noxdamage -repeat \
     -desktop "Painel Master - $(hostname)"

"""


def setup_vnc_ssh(ip: str, root_user: str, root_password: str, new_password: str = None,
                  panel_ip: str = '') -> tuple:
    """Instala e configura um console VNC (Xvfb + x11vnc) para recuperacao da maquina.

    Serve para recuperar o servidor pelo VNC quando o SSH (porta 22) estiver
    corrompido ou indisponivel: o servico sobe no boot, independente do SSH.

    Returns (success: bool, info: dict) com:
      - 'vnc_port'  : porta VNC (5900)
      - 'vnc_user'  : usuario de acesso
      - 'vnc_pass'  : senha do VNC
      - 'success'   : True somente se a porta respondeu de fato
      - 'warnings'  : lista de problemas nao fatais
      - 'message'   : descricao legivel
    """
    import secrets
    import string

    vnc_port = 5900
    warnings = []
    notes = []

    # Senha em texto puro e guardada para permitir reinstalacao idempotente.
    # Antes, cada clique gerava senha nova, invalidando a que o usuario ja tinha.
    pass_plain_path = '/etc/pmaster-vnc/pass.txt'

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=root_user, password=root_password, timeout=25)

        # ---- 1. detectar gerenciador de pacotes -------------------------------
        _, pm_out, _ = _ssh_run(ssh, "command -v apt-get || command -v yum || command -v dnf || true")
        pm = pm_out.strip().splitlines()[-1].strip() if pm_out.strip() else ''
        if not pm:
            ssh.close()
            return False, {
                'success': False, 'vnc_port': vnc_port, 'vnc_user': root_user,
                'vnc_pass': None, 'warnings': warnings,
                'message': 'Nao foi possivel detectar o gerenciador de pacotes (apt-get/yum/dnf).',
            }

        # ---- 2. instalar pacotes, AGUARDANDO de verdade ----------------------
        if pm.endswith('apt-get'):
            pkgs = 'xvfb x11vnc xauth fluxbox xterm x11-utils'
            # novnc/websockify sao separados porque podem nao existir no repo;
            # o console classico (x11vnc) nao deve falhar por causa deles
            rc, _, _ = _ssh_run(ssh, 'export DEBIAN_FRONTEND=noninteractive; '
                                     'apt-get update -qq >/dev/null 2>&1; '
                                     f'apt-get install -y -qq {pkgs}', timeout=900)
        else:
            pkgs = 'xorg-x11-server-Xvfb x11vnc fluxbox xterm xorg-x11-utils'
            inst = 'yum' if pm.endswith('yum') else 'dnf'
            rc, _, _ = _ssh_run(ssh, f'{inst} install -y -q {pkgs}', timeout=900)
        if rc != 0:
            warnings.append(f'instalacao de pacotes retornou codigo {rc}')

        # ---- 2b. console web: novnc + websockify, opcionais -----------------
        # Sem eles o VNC classico funciona, mas o console no navegador nao.
        # Nao pode derrubar a instalacao inteira por causa disso.
        if pm.endswith('apt-get'):
            _ssh_run(ssh, 'export DEBIAN_FRONTEND=noninteractive; '
                          'apt-get install -y -qq novnc websockify >/dev/null 2>&1; true', timeout=600)
        else:
            _ssh_run(ssh, 'command -v websockify >/dev/null 2>&1 || '
                          '{ yum install -y -q novnc websockify || dnf install -y -q novnc websockify; } '
                          '>/dev/null 2>&1; true', timeout=600)
        _, ws_path, _ = _ssh_run(ssh, "command -v websockify || true")
        websockify_bin = ws_path.strip().splitlines()[-1].strip() if ws_path.strip() else ''
        novnc_dir = ''
        for cand in ('/usr/share/novnc', '/usr/share/webapps/novnc', '/usr/share/novnc-utils'):
            _, found, _ = _ssh_run(ssh, f"test -f {cand}/vnc_lite.html && echo SIM || echo NAO")
            if 'SIM' in found:
                novnc_dir = cand
                break
        if not websockify_bin or not novnc_dir:
            websockify_bin = ''
            warnings.append('novnc/websockify nao disponiveis neste servidor: o VNC classico '
                            '(cliente externo) funciona, mas o console no navegador nao. '
                            'No RHEL eles costumam precisar de um repositorio extra.')
        else:
            websockify_bin = websockify_bin.strip()
        web_possible = bool(websockify_bin and novnc_dir)

        # ---- 3. confirmar que os binarios existem ---------------------------
        _, bin_out, _ = _ssh_run(
            ssh, "for b in Xvfb x11vnc xdpyinfo; do command -v $b >/dev/null 2>&1 "
                 "&& echo OK:$b || echo FALTA:$b; done"
        )
        faltando = [l.split(':', 1)[1] for l in bin_out.splitlines() if l.startswith('FALTA:')]
        if faltando:
            ssh.close()
            return False, {
                'success': False, 'vnc_port': vnc_port, 'vnc_user': root_user,
                'vnc_pass': None, 'warnings': warnings,
                'message': ('Pacotes nao instalados: ' + ', '.join(faltando)
                            + '. Verifique a saida de apt-get/yum no servidor.'),
            }
        for b in ('fluxbox', 'xterm'):
            _, has, _ = _ssh_run(ssh, f"command -v {b} >/dev/null 2>&1 && echo SIM || echo NAO")
            if 'SIM' not in has:
                warnings.append(f'{b} nao instalado: area de trabalho pode ficar sem janela/terminal')

        # ---- 4. senha: reaproveitar a existente, senao gerar ---------------
        _, cur, _ = _ssh_run(ssh, f"cat {pass_plain_path} 2>/dev/null || true")
        vnc_pass = (new_password or '').strip() or cur.strip()
        if not vnc_pass:
            vnc_pass = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
            notes.append('senha gerada')
        else:
            notes.append('senha existente preservada')
        vnc_pass = vnc_pass[:8]  # VNC auth classica corta em 8 caracteres

        # texto puro (root-only) para permitir reinstalar sem trocar a senha
        _ssh_run(ssh, f"mkdir -p /etc/pmaster-vnc && chmod 700 /etc/pmaster-vnc && "
                      f"printf '%s\\n' '{vnc_pass}' > {pass_plain_path} && "
                      f"chmod 600 {pass_plain_path}")

        # arquivo binario exigido pelo -rfbauth
        _, _, store_err = _ssh_run(
            ssh, f"x11vnc -storepasswd '{vnc_pass}' /etc/pmaster-vnc/pass >/dev/null 2>&1 "
                 f"|| printf '%s\\n' '{vnc_pass}' > /etc/pmaster-vnc/pass"
        )
        _, chk, _ = _ssh_run(ssh, "test -s /etc/pmaster-vnc/pass && echo TEM || echo VAZIO")
        if 'VAZIO' in chk:
            warnings.append('arquivo de senha do VNC ficou vazio')
        _ssh_run(ssh, "chmod 600 /etc/pmaster-vnc/pass 2>/dev/null")

        # ---- 5. script de inicializacao + unit ------------------------------
        sftp = ssh.open_sftp()
        try:
            with sftp.open('/usr/local/bin/pmaster-vnc-start', 'w') as f:
                f.write(VNC_START_SCRIPT.format(port=vnc_port))
            sftp.chmod('/usr/local/bin/pmaster-vnc-start', 0o755)
            unit = (
                "[Unit]\n"
                "Description=Painel Master VNC (console de recuperacao)\n"
                "After=network-online.target\n"
                "Wants=network-online.target\n"
                "\n"
                "[Service]\n"
                "Type=simple\n"
                "ExecStartPre=-/usr/bin/pkill -x x11vnc\n"
                "ExecStartPre=-/usr/bin/pkill -f Xvfb\n"
                "ExecStart=/usr/local/bin/pmaster-vnc-start\n"
                "Restart=always\n"
                "RestartSec=5\n"
                "KillMode=control-group\n"
                "\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n"
            )
            with sftp.open('/etc/systemd/system/pmaster-vnc.service', 'w') as f:
                f.write(unit)

            # console web: NoVNC servido por websockify, que traduz WebSocket
            # em TCP. E o que permite abrir a tela no navegador do painel.
            if web_possible:
                with sftp.open('/etc/systemd/system/pmaster-vnc-web.service', 'w') as f:
                    f.write(VNC_WEB_UNIT.format(websockify_bin=websockify_bin,
                                                novnc_dir=novnc_dir))
            with sftp.open('/usr/local/sbin/pmaster-vnc-firewall', 'w') as f:
                f.write(VNC_FIREWALL_SCRIPT)
            sftp.chmod('/usr/local/sbin/pmaster-vnc-firewall', 0o755)
            with sftp.open('/etc/systemd/system/pmaster-vnc-firewall.service', 'w') as f:
                f.write(VNC_FIREWALL_UNIT)
        finally:
            sftp.close()

        # ---- 6. subir os servicos e fechar o acesso externo -----------------
        # Antes, o painel abria a porta 5900 para qualquer um e nunca fechava.
        # O x11vnc usa VNC auth classica (DES de 56 bits, senha de 8 chars),
        # entao deixar isso aberto e a internet e forca bruta com root de premio.
        # Agora o padrao e permitir apenas o IP do painel, que e o caminho normal.
        _ssh_run(ssh,
                 "systemctl daemon-reload; "
                 "systemctl enable pmaster-vnc pmaster-vnc-firewall >/dev/null 2>&1; "
                 "systemctl restart pmaster-vnc; "
                 "systemctl restart pmaster-vnc-firewall"
                 + ("; systemctl enable pmaster-vnc-web >/dev/null 2>&1; "
                    "systemctl restart pmaster-vnc-web" if web_possible else ""),
                 timeout=180)
        # o IP vem do proprio SSH; o argumento so e usado como reserva
        real_panel_ip = _panel_ip_from_ssh(ssh) or panel_ip
        if not real_panel_ip:
            warnings.append('nao foi possivel descobrir o IP do painel; o firewall pode '
                            'ficar restrito ao endereco errado')
        _ssh_run(ssh, f"PM_PANEL_IP={real_panel_ip} /usr/local/sbin/pmaster-vnc-firewall "
                      f"restrict >/dev/null 2>&1", timeout=90)
        _, mode_out, _ = _ssh_run(ssh, "/usr/local/sbin/pmaster-vnc-firewall status 2>/dev/null | head -1")
        if 'restrict' not in mode_out:
            warnings.append('nao foi possivel confirmar o firewall do VNC; verifique se iptables existe')

        # ---- 7. verificar de verdade, com espera ----------------------------
        port_state = _wait_vnc_port(ssh, vnc_port)
        _, act, _ = _ssh_run(ssh, "systemctl is-active pmaster-vnc 2>/dev/null || true")
        active = 'active' in act

        if not active:
            _, jrnl, _ = _ssh_run(ssh, "journalctl -u pmaster-vnc --no-pager -n 12 2>/dev/null | tail -12")
            ssh.close()
            return False, {
                'success': False, 'vnc_port': vnc_port, 'vnc_user': root_user,
                'vnc_pass': vnc_pass, 'warnings': warnings, 'journal': jrnl,
                'message': (f'servico pmaster-vnc esta {act.strip() or "inativo"}. '
                            f'Ultimas linhas do log:\n{jrnl.strip()}'),
            }
        if port_state != 'OPEN':
            ssh.close()
            return False, {
                'success': False, 'vnc_port': vnc_port, 'vnc_user': root_user,
                'vnc_pass': vnc_pass, 'warnings': warnings,
                'message': (f'servico ativo, mas a porta {vnc_port} nao respondeu apos 45s. '
                            f'Verifique se o x11vnc subiu (journalctl -u pmaster-vnc).'),
            }

        # ---- 8. confirmacao ponta a ponta a partir do painel ---------------
        rfb = False
        try:
            import socket
            s = socket.create_connection((ip, vnc_port), 6)
            s.settimeout(6)
            banner = s.recv(12)
            rfb = banner.startswith(b'RFB')
            s.close()
        except Exception as e:
            warnings.append(f'nao foi possivel confirmar o banner RFB a partir do painel: {e}')

        # ---- 9. conferir se ha area de trabalho de verdade ------------------
        _, wm, _ = _ssh_run(ssh, "ps -eo args | grep -E 'fluxbox|xterm' | grep -v grep | wc -l")
        if wm.strip() in ('0', ''):
            warnings.append('display sem gerenciador de janelas/terminal: a tela pode aparecer preta')

        ssh.close()

        ok = rfb
        if not ok:
            warnings.append('porta aberta mas o servico nao respondeu com banner RFB')

        msg = 'VNC configurado com sucesso.' if ok else \
              f'VNC instalado e rodando na porta {vnc_port}, mas a confirmacao final falhou.'
        if notes:
            msg += ' (' + '; '.join(notes) + ')'
        if warnings:
            msg += ' Avisos: ' + '; '.join(warnings) + '.'

        return ok, {
            'success': ok,
            'vnc_port': vnc_port,
            'vnc_user': root_user,
            'vnc_pass': vnc_pass,
            'rfb_ok': rfb,
            'vnc_web_possible': web_possible,
            'warnings': warnings,
            'message': msg,
        }
    except Exception as e:
        return False, {'success': False, 'vnc_port': vnc_port, 'vnc_user': root_user,
                       'vnc_pass': None, 'warnings': warnings,
                       'message': f'Erro ao configurar VNC: {e}'}


def vnc_status_ssh(ip: str, root_user: str, root_password: str) -> tuple:
    """Le o estado do console VNC no servidor, sem alterar nada.

    Serve para o modal mostrar a situacao real (servico ativo, console web no
    ar, firewall restrito ou liberado) assim que abre, em vez de so depois de
    instalar.
    """
    vnc_port = 5900
    web_port = 6080
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=root_user, password=root_password, timeout=25)

        _, act, _ = _ssh_run(ssh, "systemctl is-active pmaster-vnc 2>/dev/null || true")
        _, web, _ = _ssh_run(ssh, "systemctl is-active pmaster-vnc-web 2>/dev/null || true")
        active = 'active' in act
        web_active = 'active' in web

        # o console no navegador depende de novnc + websockify, que nao existem
        # em todo servidor. Distinguir "nao instalado" de "nao disponivel" evita
        # mandar o usuario instalar de novo sem resolver o problema.
        _, novnc, _ = _ssh_run(
            ssh, "for d in /usr/share/novnc /usr/share/webapps/novnc; do "
                 "[ -f $d/vnc_lite.html ] && echo SIM && break; done; "
                 "command -v websockify >/dev/null 2>&1 && echo WS || true"
        )
        web_possible = 'SIM' in novnc and 'WS' in novnc

        port_state = _wait_vnc_port(ssh, vnc_port, timeout=12)

        _, mode, _ = _ssh_run(
            ssh, "cat /etc/pmaster-vnc/firewall-mode 2>/dev/null || echo desconhecido"
        )
        mode = mode.strip() or 'desconhecido'

        # a senha em texto puro so existe se o install ja tiver rodado
        _, pw, _ = _ssh_run(ssh, "head -1 /etc/pmaster-vnc/pass.txt 2>/dev/null || true")
        vnc_pass = pw.strip() or None

        _, wm, _ = _ssh_run(ssh, "ps -eo args | grep -E 'fluxbox|xterm -geometry' | grep -v grep | wc -l")
        desktop = wm.strip() not in ('0', '')

        ssh.close()
        return True, {
            'success': active and port_state == 'OPEN',
            'installed': active,
            'vnc_active': active,
            'vnc_web_active': web_active,
            'vnc_web_possible': web_possible,
            'vnc_port': vnc_port,
            'vnc_web_port': web_port,
            'vnc_user': root_user,
            'vnc_pass': vnc_pass,
            'port_state': port_state,
            'firewall_mode': mode,
            'has_desktop': desktop,
            'warnings': ([] if desktop else ['display sem gerenciador de janelas/terminal'])
                        + ([] if web_possible else
                           ['novnc/websockify nao existem neste servidor: o console no '
                            'navegador nao vai funcionar; o VNC classico continua'])
        }
    except Exception as e:
        return False, {'success': False, 'vnc_pass': None,
                       'message': f'Erro ao consultar o VNC: {e}'}


def vnc_firewall_ssh(ip: str, root_user: str, root_password: str,
                     mode: str, panel_ip: str = '') -> tuple:
    """Liga ou desliga o acesso externo ao console VNC.

    'restrict' (padrao) so aceita conexao do painel. 'open' libera 5900/6080
    para a internet, para permitir entrar com um cliente VNC externo. Liberar
    expoe o servidor a forca bruta: o x11vnc usa VNC auth classica, que e DES
    de 56 bits, entao a senha de 8 caracteres e quebravel.

    O modo fica gravado no servidor e reaplicado no boot.
    """
    if mode not in ('restrict', 'open'):
        return False, {'success': False,
                       'message': f"modo invalido: {mode} (use 'restrict' ou 'open')"}
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=root_user, password=root_password, timeout=25)

        cmd = '/usr/local/sbin/pmaster-vnc-firewall ' + mode
        real_panel_ip = _panel_ip_from_ssh(ssh) or panel_ip
        if real_panel_ip:
            cmd = f'PM_PANEL_IP={real_panel_ip} ' + cmd
        rc, out, err = _ssh_run(ssh, cmd, timeout=90)
        if rc != 0:
            ssh.close()
            return False, {'success': False,
                           'message': f'falha ao aplicar o firewall: {err.strip() or out.strip()}'}

        # confirma que a regra de fato ficou no lugar
        _, chk, _ = _ssh_run(
            ssh, "iptables -S PMVNC 2>/dev/null | grep -c '^-A' || echo 0"
        )
        ssh.close()
        esperado = 3 if mode == 'restrict' else 1
        got = int((chk.strip() or '0').splitlines()[-1])
        if got != esperado:
            return False, {'success': False,
                           'message': (f'firewall nao ficou como esperado: '
                                       f'{got} regras na cadeia, esperava {esperado}')}
        aviso = (' Portas 5900 e 6080 abertas para a internet: o VNC usa DES de 56 bits '
                 'e esta exposto a forca bruta. Volte para restrito quando terminar.'
                 if mode == 'open' else '')
        return True, {
            'success': True,
            'firewall_mode': mode,
            'message': ('Acesso externo liberado.' + aviso) if mode == 'open'
                       else 'Acesso restrito ao painel. Portas fechadas para a internet.',
        }
    except Exception as e:
        return False, {'success': False, 'message': f'Erro ao ajustar o firewall: {e}'}


# ---- Proxy do console VNC no proprio painel --------------------------------
# Estas duas pecas ficam no painel, nao no servidor do console, entao nao vao
# por SSH: sao arquivos do proprio painel. Ficam aqui para que uma instalacao
# nova possa recria-las, em vez de depender de alguem copiar na mao.
# Sem o mapa, todo servidor cai no default e o console abre em branco.

VNC_NGINX_LOCATION = """# ---- Console VNC (NoVNC) ---------------------------------------------------
# O navegador nunca fala com o servidor VNC direto: passa pelo painel. O nginx
# valida a sessao do Flask com auth_request antes de encaminhar, entao o
# console fica inacessivel a quem nao estiver logado como admin - e a porta do
# websockify no servidor so aceita o IP do painel.
location ~ ^/servers/vnc/(?<vnc_sid>\\d+)/(?<vnc_rest>.*)$ {
    auth_request /_vnc_authz;
    error_page 401 = @vnc_unauth;
    error_page 403 = @vnc_denied;

    proxy_pass http://$vnc_backend/$vnc_rest;

    # Upgrade de WebSocket: o noVNC fala direto com o x11vnc por tras do
    # websockify, entao o handshake tem que passar intacto.
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header Upgrade           $http_upgrade;
    proxy_set_header Connection        "upgrade";
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header CF-Connecting-IP  $http_cf_connecting_ip;

    # Console e sessao longa e ociosa. Com o timeout padrao de 60s a conexao
    # cairia sozinha toda vez que o usuario parasse para digitar um comando.
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}

location @vnc_unauth {
    default_type application/json;
    return 401 '{"success":false,"message":"Sessao expirada. Faca login novamente."}';
}

location @vnc_denied {
    default_type application/json;
    return 403 '{"success":false,"message":"Acesso negado: requer admin."}';
}

# Subrequisicao do auth_request. "internal" impede que alguem chame pela
# internet e use isto para sondar sessao de terceiros.
location = /_vnc_authz {
    internal;
    proxy_pass http://127.0.0.1:2083/_vnc_authz;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie           $http_cookie;
    proxy_set_header X-Original-URI   $request_uri;
}
"""


def vnc_nginx_upstreams_map(servers) -> str:
    """Monta o mapa de upstreams do VNC a partir da lista de servidores.

    'default' aponta para porta descartavel de proposito: um id que nao esteja
    no mapa nao pode acabar conectado em algum servidor real por acidente.
    """
    linhas = [
        '# Mapa de servidores VNC: id do servidor -> backend websockify.',
        '# Gerado pelo painel (regenerado quando a lista de servidores muda).',
        '# default aponta para porta descartavel: um id desconhecido nao deve',
        '# acabar conectado em algum servidor real.',
        'map $vnc_sid $vnc_backend {',
        '    default        "127.0.0.1:1";',
    ]
    for srv in servers or []:
        ip = (srv.get('ip') or '').strip()
        try:
            sid = int(srv.get('id'))
        except (TypeError, ValueError):
            continue
        # sem ip nao ha backend; sem id nao ha rota. Entrada invalida e
        # ignorada de proposito: gerar "null" quebraria o reload do nginx.
        if not ip or ip.lower() in ('none', 'null', '0.0.0.0'):
            continue
        linhas.append(f'    {sid}             "{ip}:6080";')
    linhas.append('}')
    return '\n'.join(linhas) + '\n'


def install_vnc_proxy(servers) -> tuple:
    """Instala/atualiza o proxy do console VNC no proprio painel.

    Roda localmente: escreve o snippet e o mapa de upstreams, inclui o snippet
    nos server blocks do nginx e recarrega. Idempotente, entao pode rodar a
    cada cadastro de servidor ou no start do painel.
    """
    import shutil
    import subprocess

    snippet_path = '/etc/nginx/snippets/vnc-location.conf'
    map_path = '/etc/nginx/conf.d/vnc-upstreams.conf'
    site_dirs = ['/etc/nginx/sites-enabled', '/etc/nginx/conf.d']

    if shutil.which('nginx') is None:
        return False, {'success': False,
                       'message': 'nginx nao encontrado neste painel; o console VNC no '
                                  'navegador depende do proxy, entao nao da para instalar.'}

    try:
        os.makedirs(os.path.dirname(snippet_path), exist_ok=True)
        with open(snippet_path, 'w', encoding='utf-8') as f:
            f.write(VNC_NGINX_LOCATION)
        with open(map_path, 'w', encoding='utf-8') as f:
            f.write(vnc_nginx_upstreams_map(servers))

        # garantir que o mapa exista no contexto http
        with open('/etc/nginx/nginx.conf', 'r', encoding='utf-8') as f:
            main = f.read()
        if 'conf.d/*.conf' not in main and 'snippets/' not in main:
            return False, {'success': False,
                           'message': ('o nginx.conf nao inclui conf.d/*.conf; inclua para que '
                                       'o mapa de upstreams do VNC seja carregado.')}

        incluidos = 0
        for d in site_dirs:
            if not os.path.isdir(d):
                continue
            for nome in os.listdir(d):
                caminho = os.path.join(d, nome)
                if not os.path.isfile(caminho):
                    continue
                try:
                    with open(caminho, 'r', encoding='utf-8') as f:
                        txt = f.read()
                except Exception:
                    continue
                if 'listen' not in txt or 'server {' not in txt:
                    continue
                if 'vnc-location.conf' in txt:
                    continue
                if 'location / {' not in txt:
                    continue
                novo = txt.replace('location / {',
                                   f'include {snippet_path};\n\n    location / {{', 1)
                with open(caminho, 'w', encoding='utf-8') as f:
                    f.write(novo)
                incluidos += 1

        chk = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
        if chk.returncode != 0:
            return False, {'success': False,
                           'message': 'nginx -t falhou:\n' + (chk.stderr or chk.stdout)[-500:]}
        subprocess.run(['nginx', '-s', 'reload'], capture_output=True, text=True)
        return True, {
            'success': True,
            'message': (f'Proxy do console VNC atualizado. '
                        f'{incluidos} server block(s)receberam o include.'),
        }
    except Exception as e:
        return False, {'success': False, 'message': f'Falha ao instalar o proxy do VNC: {e}'}
