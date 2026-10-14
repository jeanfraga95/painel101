import app as a
import re
c = a.app.test_client()
admin_id = a.db.get_db().execute("SELECT id FROM panel_users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()['id']
with c.session_transaction() as sess:
    sess['user_id'] = admin_id
    sess['role'] = 'admin'

h = c.get('/users').get_data(as_text=True)
chips = re.findall(r'chip-n">(\d+)<', h)
print('users chips counts:', chips)
assert chips, 'no chip numbers rendered'

h = c.get('/resellers').get_data(as_text=True)
chips = re.findall(r'chip-n">([0-9]+)<', h)
print('resellers chips counts:', chips)
assert chips

h = c.get('/mercadopago').get_data(as_text=True)
m = re.findall(r'aprovado\(s\)</span>', h)
print('mercadopago approved summary:', bool(m))
assert m

h = c.get('/online').get_data(as_text=True)
assert 'totalOnlineCount' in h
print('online OK')
print('ALL GOOD')
