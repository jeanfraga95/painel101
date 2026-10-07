#!/usr/bin/env python3
"""
Reaper de sessoes SSH do painel PMG (grupo pmg_vpn).

Por que existe
--------------
O trafego chega ao sshd atraves de um proxy local
(/etc/VPNManager/bin/proxy --to 22). Esse proxy mantem o socket do
backend aberto mesmo depois de o cliente sumir, entao o sshd continua
vendo a conexao como ESTABLISHED e nunca descarta a sessao.
ClientAliveInterval NAO resolve: quem responde ao keepalive do sshd e o
proxy, nao o cliente.

O cliente do VPN reconecta a cada ~2 min e cada sessao velha fica viva
para sempre. Medido: 1670 sessoes, 71% sem CPU nenhuma, 1.192 com mais
de 24h. O painel passou a mostrar 439 conexoes para um usuario com 1.

Criterio
--------
Limiar absoluto de CPU nao funciona: uma sessao morta pode ter 0,07s
(so o handshake) enquanto uma viva ociosa tem poucos segundos tambem.
Por isso o reaper e stateful: compara cada sessao com o snapshot da
execucao anterior. Se CPU (ticks) e numero de sockets nao mudaram
nada entre duas execucoes separadas por GRACE, a sessao esta morta.

Regras imediatas (independem de snapshot):
  * fds >= WEDGED_FDS            -> processo travado, ja nao serve
  * sockets <= BASE_SOCKETS e idade > MIN_AGE -> nenhum canal de tunel

Como o cliente reconecta sozinho em ~2 min, derrubar uma sessao ociosa
e imperceptivel para o usuario.
"""
import json
import os
import re
import signal
import sys
import time
from collections import defaultdict

MIN_AGE = int(os.environ.get('REAP_MIN_AGE', '600'))          # 10 min
GRACE = int(os.environ.get('REAP_GRACE', '600'))               # 10 min sem mudanca
BASE_SOCKETS = int(os.environ.get('REAP_BASE_SOCKETS', '4'))
WEDGED_FDS = int(os.environ.get('REAP_WEDGED_FDS', '900'))
STALE_AGE = int(os.environ.get('REAP_STALE_AGE', '21600'))    # 6h
STALE_CPU = int(os.environ.get('REAP_STALE_CPU', '100'))      # 1s de CPU
VPN_GID = os.environ.get('REAP_VPN_GID')
STATE = os.environ.get('REAP_STATE', '/var/lib/pmg-reap/state.json')
DRY = '--dry' in sys.argv

if not VPN_GID:
    # o gid do pmg_vpn muda entre servidores; descobre em vez de fixar
    try:
        import grp
        VPN_GID = str(grp.getgrnam('pmg_vpn').gr_gid)
    except (KeyError, ImportError):
        VPN_GID = '1002'

HZ = os.sysconf('SC_CLK_TCK')
PRIV_RE = re.compile(r'^(.+?)\s*\[priv\]$')


def sockets_of(pid):
    d = f'/proc/{pid}/fd'
    n = 0
    try:
        for fd in os.listdir(d):
            try:
                if 'socket' in os.readlink(f'{d}/{fd}'):
                    n += 1
            except OSError:
                pass
    except OSError:
        return -1
    return n


def in_vpn_group(pid):
    """So toca em processos do grupo pmg_vpn. O filtro de '@' no titulo
    ja separa tunel (sem tty) de sessao administrativa, mas isto evita
    matar conta de admin se o titulo mudar de forma inesperada."""
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('Groups:'):
                    return str(VPN_GID) in line.split()[1:]
    except OSError:
        pass
    return False


def read_stat(pid):
    with open(f'/proc/{pid}/stat') as f:
        raw = f.read()
    f = raw[raw.rindex(')') + 2:].split()
    return {'ppid': int(f[1]), 'utime': int(f[11]), 'stime': int(f[12]),
            'starttime': int(f[19])}


def collect():
    """-> {pid: (user, ppid, cpu_ticks, sockets, age)} das sessoes de tunel."""
    up = float(open('/proc/uptime').read().split()[0])
    privs = set()
    titles = {}
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        pid = int(d)
        try:
            # cmdline vem preenchido com \x00 -> corta no primeiro NUL
            title = (open(f'/proc/{pid}/cmdline', 'rb').read()
                     .split(b'\x00', 1)[0]
                     .decode('utf-8', 'replace').strip())
        except OSError:
            continue
        if not title.startswith('sshd: '):
            continue
        body = title[6:]
        if PRIV_RE.match(body):
            privs.add(pid)
        titles[pid] = body

    out = {}
    for pid, body in titles.items():
        if pid in privs:
            continue
        user = body
        if user in ('root', 'sshd') or '@' in user or '/' in user or not user:
            continue
        if not in_vpn_group(pid):
            continue
        try:
            st = read_stat(pid)
        except OSError:
            continue
        age = up - st['starttime'] / HZ
        if age < 60:
            continue
        out[pid] = (user, st['ppid'], st['utime'] + st['stime'], sockets_of(pid), age)
    return out, privs


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(cur, now):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({str(p): {'cpu': v[2], 'socks': v[3], 'user': v[0], 'at': now}
                   for p, v in cur.items()}, f)
    os.replace(tmp, STATE)


def main():
    now = int(time.time())
    cur, privs = collect()
    prev = load_state()

    dead = {}          # pid a matar -> [(user, motivo)]
    for pid, (user, ppid, cpu, socks, age) in cur.items():
        target = ppid if ppid in privs else pid
        if target in dead:
            continue
        if socks < 0:
            continue
        if socks >= WEDGED_FDS:
            dead[target] = [(user, f'travado ({socks} fds)')]
            continue
        if age > MIN_AGE and socks <= BASE_SOCKETS:
            dead[target] = [(user, f'sem canais ({socks} sockets)')]
            continue
        # backlog antigo: horas de vida e praticamente zero CPU
        if age > STALE_AGE and cpu <= STALE_CPU:
            dead[target] = [(user, f'{int(age // 3600)}h de vida, {cpu}ticks de cpu')]
            continue
        p = prev.get(str(pid))
        if p and p.get('cpu') == cpu and p.get('socks') == socks \
                and now - p.get('at', 0) >= GRACE:
            dead[target] = [(user, f'parado ha {int((now - p["at"]) / 60)}min '
                                  f'(cpu={cpu}t sockets={socks})')]

    if not DRY:
        save_state(cur, now)

    print(f'sessoes de tunel: {len(cur)}   mortas: {len(dead)}')
    if not dead:
        return 0
    if DRY:
        byu = defaultdict(list)
        for items in dead.values():
            byu[items[0][0]].append(items[0][1])
        for u, rs in sorted(byu.items(), key=lambda z: -len(z[1]))[:10]:
            print(f'  {u:16} {len(rs):>3}  ex: {rs[0]}')
        return 0

    killed = 0
    for target in dead:
        try:
            os.kill(target, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(1)
    for target in dead:
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    print(f'mandados SIGTERM: {killed}')
    byu = defaultdict(int)
    for items in dead.values():
        byu[items[0][0]] += 1
    for u, n in sorted(byu.items(), key=lambda z: -z[1])[:10]:
        print(f'  {u:16} {n}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
