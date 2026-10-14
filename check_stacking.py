import app as a

c = a.app.test_client()
admin_id = a.db.get_db().execute(
    "SELECT id FROM panel_users WHERE role='admin' ORDER BY id LIMIT 1"
).fetchone()['id']
with c.session_transaction() as sess:
    sess['user_id'] = admin_id
    sess['role'] = 'admin'

h = c.get('/users').get_data(as_text=True)
css = h[h.find('<style>'):h.rfind('</style>')]

lines = css.split('\n')
block = ''
selectors = []
cur_sel = ''

for ln in lines:
    if '{' in ln and '}' not in ln and cur_sel == '':
        cur_sel = ln.strip()
    elif cur_sel and '}' in ln:
        pass

# fallback: split top-level braces manually
def split_rules(css_text):
    rules = []
    depth = 0
    buf = ''
    for ch in css_text:
        if ch == '{':
            depth += 1
            buf += ch
        elif ch == '}':
            depth -= 1
            buf += ch
            if depth == 0:
                rules.append(buf)
                buf = ''
        else:
            buf += ch
    return rules

rules = split_rules(css)
print('TOTAL RULES:', len(rules))
for r in rules:
    head, _, body = r.partition('{')
    sel = ' '.join(head.split()).strip()
    b = ' '.join(body.split())
    if 'z-index' in b or 'position: fixed' in b or 'backdrop-filter' in b or 'transform' in b:
        print('SEL:', sel[:90])
        print('    ', b[:220])
