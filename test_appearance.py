import sys

import app as a

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0.0.0 Safari/537.36')

c = a.app.test_client()
conn = a.db.get_db()
admin_id = conn.execute(
    "SELECT id FROM panel_users WHERE role='admin' ORDER BY id LIMIT 1"
).fetchone()['id']
CSRF = 'token-aparencia-456'
with c.session_transaction() as sess:
    sess['user_id'] = admin_id
    sess['role'] = 'admin'
    sess['csrf_token'] = CSRF

falhas = []


def check(nome, cond, extra=''):
    print(('OK   ' if cond else 'FALHA') + ' | ' + nome + ((' — ' + str(extra)) if extra and not cond else ''))
    if not cond:
        falhas.append(nome)


def get(url):
    return c.get(url, headers={'User-Agent': UA})


def post(url, **data):
    data['csrf_token'] = CSRF
    return c.post(url, data=data, headers={'User-Agent': UA})


def db_appearance(uid):
    row = a.db.get_appearance(uid)
    return row


def reset_appearance(uid):
    a.db.set_appearance(uid, theme='', style='')


reset_appearance(admin_id)

# ── 1. Migração das colunas ─────────────────────────────────────────────────
cols = [r[1] for r in conn.execute('PRAGMA table_info(panel_users)').fetchall()]
check('coluna theme_mode existe', 'theme_mode' in cols, cols)
check('coluna style_preset existe', 'style_preset' in cols, cols)

# ── 2. HTML do dashboard sem escolha salva (padrão) ─────────────────────────
r = get('/')
h = r.get_data(as_text=True)
check('dashboard -> 200', r.status_code == 200, r.status_code)
check('html com data-style="default"', 'data-style="default"' in h)
check('link presets.css', 'styles/presets.css' in h)
check('modal appearanceModal', 'id="appearanceModal"' in h)
check('radio tema escuro/claro', 'id="appearanceThemeDark"' in h and 'id="appearanceThemeLight"' in h)
check('9 botões de estilo', h.count('data-style-id="') == 9, h.count('data-style-id="'))
check('presets no JS (PMG_STYLE_NAMES)', "PMG_STYLE_NAMES['tailwind-emerald']" in h)
check('rota /profile/appearance no JS', '/profile/appearance' in h)
check('botão Estilo na topbar', 'openAppearance()' in h)
check('card de aparência no dashboard', 'id="appearanceStyleName"' in h and 'Aparência do painel' in h)
check('card mostra estilo atual (Padrão)', 'id="appearanceStyleName">Padrão<' in h)
import re as _re
check('presets agrupados por família',
      len(_re.findall(r'text-uppercase fw-bold mb-2"[^>]*>\s*\n\s*(Bootstrap|Tailwind|CSS Puro|SCSS/Sass)\s*\n', h)) == 4)

# ── 3. POST válido: salva na conta e aparece no HTML ────────────────────────
r = post('/profile/appearance', theme='light', style='bootstrap-cosmo')
d = r.get_json()
check('salva tema+estilo -> success', r.status_code == 200 and d.get('success') is True, d)
check('grava no banco', db_appearance(admin_id) == {'theme': 'light', 'style': 'bootstrap-cosmo'},
      db_appearance(admin_id))

h = get('/').get_data(as_text=True)
check('html usa data-style="bootstrap-cosmo"', 'data-style="bootstrap-cosmo"' in h)
check('html usa data-bs-theme="light"', 'data-bs-theme="light"' in h)
check('botão de estilo ativo marcado',
      'data-style-id="bootstrap-cosmo" onclick="chooseStyle' in h)
check('card mostra nome do estilo (Cosmo)', 'id="appearanceStyleName">Cosmo<' in h)
check('card mostra tema (Claro)', 'id="appearanceThemeName">Claro<' in h)

# ── 4. Cada preset troca o atributo no HTML ─────────────────────────────────
faltando = []
for p in a.STYLE_PRESETS:
    r = post('/profile/appearance', theme='dark', style=p['id'])
    d = r.get_json()
    h = get('/').get_data(as_text=True)
    if not (d.get('success') is True and f'data-style="{p["id"]}"' in h):
        faltando.append((p['id'], d))
check('todos os 9 presets aplicam no HTML', not faltando, faltando)

# ── 5. Validações de entrada ────────────────────────────────────────────────
r = post('/profile/appearance', theme='dark', style='estilo-que-nao-existe')
d = r.get_json()
check('estilo inválido -> erro', r.status_code == 200 and d.get('success') is False, d)
check('estilo inválido não altera o banco',
      db_appearance(admin_id)['style'] in [p['id'] for p in a.STYLE_PRESETS], db_appearance(admin_id))

r = post('/profile/appearance', theme='rosa', style='default')
d = r.get_json()
check('tema inválido -> erro', r.status_code == 200 and d.get('success') is False, d)

# ── 6. Limpa a escolha (volta ao padrão do painel) ──────────────────────────
r = post('/profile/appearance', theme='', style='')
d = r.get_json()
check('limpar -> success', r.status_code == 200 and d.get('success') is True, d)
check('limpar zera as duas colunas', db_appearance(admin_id) == {'theme': '', 'style': ''},
      db_appearance(admin_id))
h = get('/').get_data(as_text=True)
check('após limpar, data-style volta a default', 'data-style="default"' in h)
import re as _re
_m = _re.search(r'<html[^>]*data-bs-theme="([^"]+)"', h)
panel_theme = a.get_settings().get('panel_theme') or 'dark'
check('após limpar, tema volta ao do painel', _m is not None and _m.group(1) == panel_theme,
      (_m.group(1) if _m else None, panel_theme))

# ── 7. Sessão é por conta: revenda tem a própria escolha ────────────────────
rev = conn.execute(
    "SELECT id FROM panel_users WHERE role='reseller' ORDER BY id LIMIT 1"
).fetchone()
if rev:
    rev_id = rev['id']
    reset_appearance(admin_id)
    reset_appearance(rev_id)

    # login como revenda
    with c.session_transaction() as sess:
        sess.clear()
        sess['user_id'] = rev_id
        sess['role'] = 'reseller'
        sess['csrf_token'] = CSRF
    r = post('/profile/appearance', theme='light', style='sass-neon')
    d = r.get_json()
    check('revenda salva o próprio estilo', d.get('success') is True, d)
    check('revenda gravado', db_appearance(rev_id) == {'theme': 'light', 'style': 'sass-neon'},
          db_appearance(rev_id))
    check('admin intacto pela revenda', db_appearance(admin_id) == {'theme': '', 'style': ''},
          db_appearance(admin_id))

    h = get('/').get_data(as_text=True)
    check('dash da revenda usa data-style="sass-neon"', 'data-style="sass-neon"' in h)
    check('dash da revenda tem o card', 'id="appearanceStyleName">Neon<' in h)
    check('página da revenda 200', r.status_code == 200)

    # volta para o admin
    with c.session_transaction() as sess:
        sess.clear()
        sess['user_id'] = admin_id
        sess['role'] = 'admin'
        sess['csrf_token'] = CSRF
else:
    print('AVISO: sem conta de revenda para testar isolamento')

# ── 8. Presets.css existe e cobre cada preset (dark + light) ────────────────
r = get('/static/styles/presets.css')
css = r.get_data(as_text=True)
check('presets.css servido (200)', r.status_code == 200 and len(css) > 3000,
      (r.status_code, len(css)))
sem_css = [p['id'] for p in a.STYLE_PRESETS
           if p['id'] != 'default' and f'[data-style="{p["id"]}"]' not in css]
check('todo preset tem regras no CSS', not sem_css, sem_css)
check('variantes dark e light presentes',
      css.count('[data-bs-theme="dark"]') >= 8 and css.count('[data-bs-theme="light"]') >= 8,
      (css.count('[data-bs-theme="dark"]'), css.count('[data-bs-theme="light"]')))

reset_appearance(admin_id)
if rev:
    reset_appearance(rev_id)

print()
if falhas:
    print(f'FALHOU ({len(falhas)}):')
    for f in falhas:
        print('  -', f)
    sys.exit(1)
print('TODOS OS TESTES DE APARÊNCIA PASSARAM')
