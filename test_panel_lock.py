import sys
from datetime import datetime, timedelta

import app as a
import db as dbm

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0.0.0 Safari/537.36')

TZ = a.TZ
c = a.app.test_client()
conn = a.db.get_db()
CSRF = 'token-panel-lock-789'

falhas = []


def check(nome, cond, extra=''):
    print(('OK   ' if cond else 'FALHA') + ' | ' + nome + ((' — ' + str(extra)) if extra and not cond else ''))
    if not cond:
        falhas.append(nome)


def login_as(uid, role):
    with c.session_transaction() as s:
        s.clear()
        s['user_id'] = uid
        s['role'] = role
        s['csrf_token'] = CSRF


def get(url):
    return c.get(url, headers={'User-Agent': UA})


def post(url, **data):
    data['csrf_token'] = CSRF
    return c.post(url, data=data, headers={'User-Agent': UA})


def iso(delta_days):
    return (datetime.now(TZ) + timedelta(days=delta_days)).strftime('%Y-%m-%d')


def has_banner(html):
    """O banner real (não a regra CSS .panel-locked-banner no <style>)."""
    marker = '<div class="panel-locked-banner" role="alert">'
    return marker in html


# ── Setup: acha (ou cria) uma revenda ativa para os testes ──────────────────
admin_id = conn.execute(
    "SELECT id FROM panel_users WHERE role='admin' ORDER BY id LIMIT 1"
).fetchone()['id']
rev = conn.execute(
    "SELECT id, expires_at FROM panel_users WHERE role='reseller' ORDER BY id LIMIT 1"
).fetchone()
if not rev:
    print('AVISO: sem revenda cadastrada — não é possível testar o lock')
    sys.exit(0)
rev_id = rev['id']
rev_orig = rev['expires_at']


def set_rev_expiry(delta_days):
    """Ajusta o vencimento do painel da revenda e zera o flag de lock."""
    conn.execute("UPDATE panel_users SET expires_at=?, panel_locked=0 WHERE id=?",
                 (iso(delta_days), rev_id))
    conn.commit()


def rev_row():
    return a.db.get_panel_user(rev_id)


# ── 1. Regra de negócio pura (bordas 0, -1, -2) ─────────────────────────────
check('dias = 0 → NÃO travado', a.panel_is_locked_for(iso(0)) is False)
check('dias = -1 (venceu ontem) → NÃO travado', a.panel_is_locked_for(iso(-1)) is False)
check('dias = -2 → TRAVADO', a.panel_is_locked_for(iso(-2)) is True)
check('dias = -30 → TRAVADO', a.panel_is_locked_for(iso(-30)) is True)
check('dias = +5 → NÃO travado', a.panel_is_locked_for(iso(5)) is False)
check('sem data (None) → nunca trava', a.panel_is_locked_for(None) is False)

# ── 2. coluna panel_locked existe ───────────────────────────────────────────
cols = [r[1] for r in conn.execute('PRAGMA table_info(panel_users)').fetchall()]
check('coluna panel_locked existe', 'panel_locked' in cols, cols)

def novo_usuario_disabled(html):
    """True se o <button> que abre o modal de criar usuário tem `disabled`."""
    m = __import__('re').search(
        r'<button[^>]*data-bs-target="#createUserModal"[^>]*>', html)
    return bool(m and 'disabled' in m.group(0))


# ── 3. Painel em -1: acesso normal (criar/renovar liberados) ────────────────
set_rev_expiry(-1)
login_as(rev_id, 'reseller')
h = get('/users').get_data(as_text=True)
check('painel -1: /users abre (200)', get('/users').status_code == 200)
check('painel -1: sem banner de vencido', not has_banner(h))
check('painel -1: botão Novo Usuário habilitado', not novo_usuario_disabled(h))
# POST de criação deve passar do lock (vai falhar por dados, mas NÃO com panel_locked)
r = post('/users/create', username='', days='30', category_id='0')
d = r.get_json()
check('painel -1: POST create não é bloqueado pelo lock',
      not (r.status_code == 403 and d.get('panel_locked')), (r.status_code, d.get('message')))

# ── 4. Painel em -2: trava ──────────────────────────────────────────────────
set_rev_expiry(-2)
h = get('/users').get_data(as_text=True)
check('painel -2: /users abre e mostra banner', has_banner(h))
check('painel -2: banner cita vencimento', 'vencido' in h.lower())
check('painel -2: botão Novo Usuário desabilitado', novo_usuario_disabled(h))

# logout/login para forçar reavaliação no contexto
login_as(rev_id, 'reseller')

# ── 5. Endpoints de escrita bloqueados com -2 ───────────────────────────────
bloqueados = [
    ('/users/create', dict(username='zzz', days='30', category_id='1')),
    ('/users/test', dict(username='zzz', hours='2', category_id='1')),
    ('/users/renew/1', dict(days='30')),
    ('/users/bulk_renew', dict(ids='1', days='30')),
    ('/users/update/1', dict(expires_at=iso(30))),
    ('/users/suspend/1', dict()),
    ('/users/delete/1', dict()),
    ('/resellers/create', dict(username='subzzz', days='30')),
    ('/resellers/renew/1', dict(days='30')),
    ('/resellers/update/1', dict(expires_at=iso(30))),
]
for url, data in bloqueados:
    r = post(url, **data)
    d = r.get_json() or {}
    ok = r.status_code == 403 and d.get('panel_locked') and d.get('success') is False
    check(f'painel -2: POST {url} bloqueado', ok, (r.status_code, d.get('message')))

# ── 6. Leitura continua liberada com -2 ─────────────────────────────────────
for url in ['/users', '/', '/profile', '/tickets', '/notes']:
    r = get(url)
    check(f'painel -2: GET {url} liberado', r.status_code == 200, r.status_code)

# ── 7. Renovação pelo admin destrava: -2 → +30 ──────────────────────────────
set_rev_expiry(30)
login_as(rev_id, 'reseller')
h = get('/users').get_data(as_text=True)
check('após renovar (+30): sem banner', not has_banner(h))
r = post('/users/create', username='', days='30', category_id='0')
d = r.get_json() or {}
check('após renovar: POST create não é bloqueado',
      not (r.status_code == 403 and d.get('panel_locked')), (r.status_code, d.get('message')))

# ── 8. Suspensão no servidor na transição (sem tocar nas VPS reais) ─────────
# Aqui validamos a lógica de transição via flag panel_locked usando um stub de
# broadcast_command que NÃO fala com os servidores.
chamadas = {'block': 0, 'unblock': 0}
_orig_cmd = a.sc.build_block_cmd
_orig_uncmd = a.sc.build_unblock_cmd
_orig_bcast = a.sc.broadcast_command


def _fake_block(u):
    chamadas['block'] += 1
    return 'BLOCK'


def _fake_unblock(u):
    chamadas['unblock'] += 1
    return 'UNBLOCK'


def _fake_bcast(user, cmd, dbmod):
    return [('fake', True, 'ok')]


a.sc.build_block_cmd = _fake_block
a.sc.build_unblock_cmd = _fake_unblock
a.sc.broadcast_command = _fake_bcast

# cria um usuário SSH de teste para a revenda, se não houver
uid_ssh = None
try:
    row = conn.execute(
        "SELECT id FROM ssh_users WHERE owner_id=? LIMIT 1", (rev_id,)
    ).fetchone()
    if not row:
        ok, msg, new_id = dbm.create_ssh_user(
            'locktest_' + str(rev_id), 'x', rev_id, 1, iso(30), 1, None, 0, ''
        )
        uid_ssh = new_id if ok else None
    else:
        uid_ssh = row['id']
except Exception as e:
    print('AVISO: não deu para preparar usuário SSH de teste:', e)

set_rev_expiry(-2)
login_as(rev_id, 'reseller')
get('/')  # dispara _sync_panel_lock_suspension
import time
time.sleep(0.6)
check('transição p/ travado: broadcast de block executado', chamadas['block'] > 0, chamadas)
check('transição p/ travado: flag panel_locked=1', dbm.get_panel_locked(rev_id) is True)

chamadas['block'] = 0
# destrava mexendo SÓ no vencimento (preserva o flag=1) para exercitar a
# transição travado→destravado de verdade.
conn.execute("UPDATE panel_users SET expires_at=? WHERE id=?", (iso(10), rev_id))
conn.commit()
get('/')  # destrava
time.sleep(0.6)
check('transição p/ destravado: broadcast de unblock executado', chamadas['unblock'] > 0, chamadas)
check('transição p/ destravado: flag panel_locked=0', dbm.get_panel_locked(rev_id) is False)

# segunda chamada não deve re-suspender (flag já está correto)
chamadas['block'] = 0
chamadas['unblock'] = 0
login_as(rev_id, 'reseller')
get('/')
time.sleep(0.3)
check('sem transição: não re-executa broadcast', chamadas['block'] == 0 and chamadas['unblock'] == 0, chamadas)

a.sc.build_block_cmd = _orig_cmd
a.sc.build_unblock_cmd = _orig_uncmd
a.sc.broadcast_command = _orig_bcast

# ── 9. Admin NUNCA é travado ────────────────────────────────────────────────
login_as(admin_id, 'admin')
h = get('/users').get_data(as_text=True)
check('admin: sem banner de travamento', not has_banner(h))

# ── Restaura estado original ────────────────────────────────────────────────
conn.execute("UPDATE panel_users SET expires_at=?, panel_locked=0 WHERE id=?",
             (rev_orig, rev_id))
conn.commit()

print()
if falhas:
    print(f'FALHOU ({len(falhas)}):')
    for f in falhas:
        print('  -', f)
    sys.exit(1)
print('TODOS OS TESTES DE VENCIMENTO DE PAINEL PASSARAM')
