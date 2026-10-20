import re
import sys

import app as a

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0.0.0 Safari/537.36')

c = a.app.test_client()
admin_id = a.db.get_db().execute(
    "SELECT id FROM panel_users WHERE role='admin' ORDER BY id LIMIT 1"
).fetchone()['id']
CSRF = 'token-de-teste-123'
with c.session_transaction() as sess:
    sess['user_id'] = admin_id
    sess['role'] = 'admin'
    sess['csrf_token'] = CSRF

csrf = CSRF


def post(url, **data):
    data['csrf_token'] = csrf
    return c.post(url, data=data, headers={'User-Agent': UA})


falhas = []


def check(nome, cond, extra=''):
    print(('OK   ' if cond else 'FALHA') + ' | ' + nome + ((' — ' + str(extra)) if extra and not cond else ''))
    if not cond:
        falhas.append(nome)


# ── 1. Modal de confirmação global presente em todas as páginas ────────────
paginas = ['/users', '/resellers', '/tickets', '/online', '/mercadopago',
           '/notes', '/servers', '/settings', '/backup', '/migration',
           '/', '/profile']


def sem_confirm_nativo(h):
    """Procura chamadas reais a confirm(), ignorando comentários e o fallback
    window.confirm() do próprio confirmDialog."""
    sem_coment = re.sub(r'/\*.*?\*/', '', h, flags=re.S)
    sem_coment = re.sub(r'//[^\n]*', '', sem_coment)
    sem_coment = sem_coment.replace('window.confirm', '')
    return not re.search(r'(?<![\w.])(?:!|=|return\s)confirm\s*\(', sem_coment)


for p in paginas:
    r = c.get(p, headers={'User-Agent': UA})
    h = r.get_data(as_text=True)
    check(f'{p} -> {r.status_code}', r.status_code == 200)
    if r.status_code == 200:
        check(f'{p} tem confirmModal', 'id="confirmModal"' in h)
        check(f'{p} tem confirmDialog()', 'function confirmDialog' in h)
        check(f'{p} tem toggleTheme()', 'function toggleTheme' in h)
        check(f'{p} sem confirm() nativo', sem_confirm_nativo(h))

# ── 2. Tema: variáveis CSS + botão na topbar ───────────────────────────────
h = c.get('/users', headers={'User-Agent': UA}).get_data(as_text=True)
check('CSS var --pmg-bg definida (dark)', '--pmg-bg: #16181f' in h)
check('CSS var --pmg-bg definida (light)', '--pmg-bg: #f1f3f7' in h)
check('body usa var(--pmg-bg)', 'background: var(--pmg-bg)' in h)
check('script anti-flash de tema', "localStorage.getItem('pmg_theme')" in h)
check('botão de tema na topbar', 'id="themeToggleBtn"' in h)

# ── 3. Acessibilidade ──────────────────────────────────────────────────────
sem_aria = []
for m in re.finditer(r'<button(?P<a>[^>]*)>\s*<i[^>]*></i>\s*</button>', h):
    if 'aria-label' not in m.group('a'):
        t = re.search(r'title="([^"]+)"', m.group('a'))
        sem_aria.append(t.group(1) if t else m.group('a')[:60])
check('botoes so-icone tem aria-label', not sem_aria, sem_aria[:5])
icons_deco = [i for i in re.findall(r'<i class="[^"]*"[^>]*></i>', h) if 'aria-hidden' not in i]
check('icones decorativos com aria-hidden', not icons_deco, icons_deco[:5])

# ── 4. Botão renovar em massa na listagem de usuários ──────────────────────
check('botão Renovar Selecionados', 'id="bulkRenewBtn"' in h)
check('modal bulkRenewModal', 'id="bulkRenewModal"' in h)
check('função bulkRenew()', 'function bulkRenew()' in h)
check('rota /users/bulk_renew no JS', '/users/bulk_renew' in h)

# ── 5. Busca com debounce ──────────────────────────────────────────────────
check('input de busca com id', 'id="searchInput"' in h)
check('debounce da busca', 'setTimeout(() =>' in h and 'form.submit()' in h)
check('spinner de busca', 'id="searchSpinner"' in h)

# ── 6. Rota bulk_renew responde (sem ids -> erro amigável) ─────────────────
r = post('/users/bulk_renew', ids='', days='30')
d = r.get_json()
check('bulk_renew sem ids -> 200 + erro', r.status_code == 200 and d.get('success') is False, d)

r = post('/users/bulk_renew', ids='999999', days='30')
d = r.get_json()
check('bulk_renew id inexistente -> falha controlada',
      r.status_code == 200 and d.get('success') is False and len(d.get('failed', [])) == 1, d)

# ── 7. Renovação real em massa (2 usuários existentes) ─────────────────────
rows = a.db.get_db().execute(
    "SELECT id, username, expires_at FROM ssh_users WHERE status='active' "
    "AND is_test=0 ORDER BY id DESC LIMIT 2"
).fetchall()
if len(rows) >= 2:
    ids = ','.join(str(r0['id']) for r0 in rows)
    antes = {r0['id']: r0['expires_at'] for r0 in rows}
    r = post('/users/bulk_renew', ids=ids, days='5')
    d = r.get_json()
    print('   resposta bulk_renew:', d)
    check('bulk_renew devolve renewed', d.get('success') is True and len(d.get('renewed', [])) == 2, d)
    check('bulk_renew devolve id+user+new_expiry',
          all(('id' in x and 'user' in x and 'new_expiry' in x) for x in d.get('renewed', [])), d)
    depois = {r0['id']: a.db.get_ssh_user(r0['id'])['expires_at'] for r0 in rows}
    check('vencimento avançou no banco',
          all((depois[i] or '')[:10] > (antes[i] or '')[:10] for i in antes),
          {i: (antes[i], depois[i]) for i in antes})
else:
    print('AVISO: menos de 2 usuários ativos para testar renovação real')

# ── 8. Renovação individual continua funcionando (refactor) ────────────────
if rows:
    uid = rows[0]['id']
    antes = a.db.get_ssh_user(uid)['expires_at']
    r = post(f'/users/renew/{uid}', days='1')
    d = r.get_json()
    check('renew individual ainda funciona', r.status_code == 200 and 'success' in d, d)
    depois = a.db.get_ssh_user(uid)['expires_at']
    check('renew individual avançou 1 dia', (depois or '')[:10] > (antes or '')[:10], (antes, depois))

print()
if falhas:
    print(f'FALHOU ({len(falhas)}):')
    for f in falhas:
        print('  -', f)
    sys.exit(1)
print('TODOS OS TESTES PASSARAM')
