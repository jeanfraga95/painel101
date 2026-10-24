#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_security_passwords.py — testes das proteções da rodada de segurança.

Cobre:
  - política de senha forte
  - hash PBKDF2 + compatibilidade com o hash legado (sha256)
  - cifragem (Fernet) das senhas dos usuários SSH
  - migração que elimina password_plain e marca troca obrigatória
  - login forçado a trocar a senha + /profile exigindo senha atual
  - rate-limit e token do /checkuser
  - IP real (X-Forwarded-For falsificado é ignorado)
  - backup cifrado (não vaza o painel.db em texto puro)
  - página de login sem placeholder "admin"
  - recusa de senha fraca na troca de senha de usuário SSH

Uso:  python3 test_security_passwords.py
"""

import os
import re
import sys
import hashlib
import sqlite3
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import app as A

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

results = []


def check(name, cond, extra=''):
    results.append((name, bool(cond), extra))
    print(('PASS  ' if cond else 'FAIL  ') + name + ((' | ' + str(extra)) if extra else ''))


def csrf(client, path='/login'):
    html = client.get(path, headers={'User-Agent': UA}).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else ''


def _ids():
    """owner_id e server_id válidos no banco de teste (FK do ssh_users)."""
    conn = db.get_db()
    try:
        owner = conn.execute(
            "SELECT id FROM panel_users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        server = conn.execute("SELECT id FROM servers ORDER BY id LIMIT 1").fetchone()
    finally:
        conn.close()
    return (owner['id'] if owner else 1, server['id'] if server else None)


# ---------------------------------------------------------------------------
# 1) Política de senha
# ---------------------------------------------------------------------------
def test_policy():
    check('política: senha curta rejeitada', db.password_policy_error('abc') != '')
    check('política: sem letra rejeitada', db.password_policy_error('12345678') != '')
    check('política: sem número rejeitada', db.password_policy_error('abcdefgh') != '')
    check('política: senha forte aceita', db.password_policy_error('abc12345') == '')
    check('política: igual ao usuário rejeitada',
          db.password_policy_error('jeanfraga', 'jeanfraga') != '')
    check('política: contém o usuário rejeitada',
          db.password_policy_error('jeanfraga1', 'jeanfraga') != '')

    # Senha de usuário SSH/V2Ray: 5+ caracteres, qualquer combinação
    check('ssh: 4 caracteres rejeitada', db.ssh_password_error('abcd') != '')
    check('ssh: 5 caracteres aceita (sem exigir número)', db.ssh_password_error('abcde') == '')
    check('ssh: 5 caracteres aceita', db.ssh_password_error('abcde') == '')
    check('ssh: só números aceita', db.ssh_password_error('12345') == '')
    check('ssh: símbolos comuns aceitos', db.ssh_password_error('a@b#c-d_e+f%g=h:i,j~k^l') == '')
    check('ssh: espaço rejeitado', db.ssh_password_error('ab cd') != '')
    check('ssh: aspas rejeitadas', db.ssh_password_error('ab"cd') != '')
    check('ssh: crase rejeitada', db.ssh_password_error('ab`cd') != '')
    check('ssh: cifrão rejeitado', db.ssh_password_error('ab$cd') != '')
    check('ssh: ponto e vírgula rejeitado', db.ssh_password_error('ab;cd') != '')
    check('ssh: pipe rejeitado', db.ssh_password_error('ab|cd') != '')
    check('ssh: barra invertida rejeitada', db.ssh_password_error('ab\\cd') != '')
    check('ssh: senha longa aceita', db.ssh_password_error('a' * 64) == '')


# ---------------------------------------------------------------------------
# 2) Hash
# ---------------------------------------------------------------------------
def test_hash():
    h = db.hash_password('senha12345')
    check('hash: não é sha256 puro', len(h) != 64)
    check('hash: PBKDF2 gerado', h.startswith('pbkdf2:'))
    check('hash: check_password aceita', db.check_password('senha12345', h))
    check('hash: senha errada rejeitada', not db.check_password('senha12346', h))
    check('hash: hash vazio rejeitado', not db.check_password('x', ''))

    legado = hashlib.sha256(b'legado123').hexdigest()
    check('hash: legado sha256 ainda valida', db.check_password('legado123', legado))
    check('hash: legado sha256 rejeita senha errada', not db.check_password('outra', legado))
    check('hash: detecta necessidade de upgrade', db.password_needs_upgrade(legado))
    check('hash: novo hash não precisa de upgrade', not db.password_needs_upgrade(h))


# ---------------------------------------------------------------------------
# 3) Cifragem das senhas SSH
# ---------------------------------------------------------------------------
def test_encryption():
    plain = 'segredo123'
    enc = db.encrypt_ssh_password(plain)
    check('cifra: valor cifrado != original', enc != plain)
    check('cifra: prefixo enc:v1: presente', enc.startswith(db._SSH_ENC_PREFIX))
    check('cifra: round-trip devolve a senha', db.ssh_plain_password(enc) == plain)
    check('cifra: não deixa a senha legível no banco', plain not in enc)
    check('cifra: valor já cifrado não é cifrado de novo',
          db.encrypt_ssh_password(enc) == enc)
    check('cifra: senha em texto puro (legado) passa direto',
          db.ssh_plain_password('12345') == '12345')
    check('cifra: lixo devolve vazio (não levanta erro)',
          db.ssh_plain_password(db._SSH_ENC_PREFIX + 'lixo') == '')


# ---------------------------------------------------------------------------
# 4) Migração de segurança
# ---------------------------------------------------------------------------
def test_migration():
    conn = db.get_db()
    try:
        conn.execute("""INSERT INTO panel_users
                        (username, password_hash, password_plain, role, expires_at)
                        VALUES ('mig_teste', ?, 'senha12345', 'admin', '2099-12-31')""",
                     (hashlib.sha256(b'senha12345').hexdigest(),))
        owner_id, server_id = _ids()
        conn.execute("""INSERT INTO ssh_users
                        (username, password, owner_id, server_id, expires_at, connection_limit)
                        VALUES ('mig_ssh', 'senhassh1', ?, ?, '2099-12-31', 1)""",
                     (owner_id, server_id))
        conn.commit()
    finally:
        conn.close()

    db.migrate_schema_security()

    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM panel_users WHERE username='mig_teste'").fetchone()
        srow = conn.execute("SELECT * FROM ssh_users WHERE username='mig_ssh'").fetchone()
        check('migração: password_plain foi apagado', (row['password_plain'] or '') == '',
              repr(row['password_plain']))
        check('migração: hash virou PBKDF2', (row['password_hash'] or '').startswith('pbkdf2:'))
        check('migração: ninguém é marcado para troca obrigatória',
              int(row['must_change_password'] or 0) == 0)
        check('migração: senha SSH cifrada',
              (srow['password'] or '').startswith(db._SSH_ENC_PREFIX))
        check('migração: senha SSH continua recuperável',
              db.ssh_plain_password(srow['password']) == 'senhassh1')
    finally:
        conn.close()

    # limpeza
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM panel_users WHERE username='mig_teste'")
        conn.execute("DELETE FROM ssh_users WHERE username='mig_ssh'")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5) Troca de senha OPCIONAL (ninguém é obrigado a trocar) + upgrade automático
# ---------------------------------------------------------------------------
def test_no_forced_change():
    """A migração NÃO pode marcar ninguém para troca obrigatória."""
    conn = db.get_db()
    try:
        # conta com hash legado (sha256) e senha em texto puro no banco
        conn.execute("""INSERT INTO panel_users
                        (username, password_hash, password_plain, role, expires_at)
                        VALUES ('legado_teste', ?, 'senha12345', 'admin', '2099-12-31')""",
                     (hashlib.sha256(b'senha12345').hexdigest(),))
        conn.commit()
    finally:
        conn.close()

    db.migrate_schema_security()
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM panel_users WHERE username='legado_teste'").fetchone()
        check('migração: ninguém é marcado para troca obrigatória',
              int(row['must_change_password'] or 0) == 0, row['must_change_password'])
        check('migração: hash legado virou PBKDF2 (sem trocar a senha)',
              (row['password_hash'] or '').startswith('pbkdf2:'))
        check('migração: a senha continua a mesma',
              db.check_password('senha12345', row['password_hash']))
    finally:
        conn.close()

    # login normal: sem redirecionamento para /profile e sem bloqueio
    c = A.app.test_client()
    tok = csrf(c)
    r = c.post('/login', data={'username': 'legado_teste', 'password': 'senha12345',
                                'csrf_token': tok}, headers={'User-Agent': UA},
               follow_redirects=False)
    check('login: vai direto ao dashboard (sem troca obrigatória)',
          r.headers.get('Location', '').rstrip('/') in ('', '/dashboard'),
          r.headers.get('Location'))

    r2 = c.get('/', headers={'User-Agent': UA}, follow_redirects=False)
    check('navegação: dashboard liberado sem trocar a senha', r2.status_code == 200,
          r2.status_code)

    # troca de senha: opcional, mas se a senha atual for informada, tem de estar certa
    tok2 = csrf(c, '/profile')
    c.post('/profile', data={'current_password': 'errada999',
                             'new_password': 'novasenha123',
                             'csrf_token': tok2}, headers={'User-Agent': UA},
            follow_redirects=False)
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM panel_users WHERE username='legado_teste'").fetchone()
        check('perfil: senha atual errada impede a troca',
              db.check_password('senha12345', row['password_hash']))
    finally:
        conn.close()

    tok3 = csrf(c, '/profile')
    c.post('/profile', data={'current_password': 'senha12345',
                             'new_password': 'novasenha123',
                             'csrf_token': tok3}, headers={'User-Agent': UA},
            follow_redirects=False)
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM panel_users WHERE username='legado_teste'").fetchone()
        check('perfil: troca voluntary funciona com a senha atual correta',
              db.check_password('novasenha123', row['password_hash']))
    finally:
        conn.close()

    conn = db.get_db()
    try:
        conn.execute("DELETE FROM panel_users WHERE username='legado_teste'")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6) /checkuser: rate-limit e token
# ---------------------------------------------------------------------------
def test_checkuser():
    c = A.app.test_client()
    A._checkuser_hits.clear()
    codes = []
    for i in range(A.CHECKUSER_MAX_PER_MIN + 5):
        r = c.get(f'/checkuser?user=teste{i}', headers={'User-Agent': UA})
        codes.append(r.status_code)
    check('checkuser: rate-limit devolve 429', 429 in codes,
          f'{codes.count(200)}x200 / {codes.count(429)}x429')

    A._checkuser_hits.clear()
    r = c.get('/checkuser?user=teste', headers={'User-Agent': UA})
    check('checkuser: responde normalmente antes do limite', r.status_code == 200, r.status_code)

    old = A.CHECKUSER_TOKEN
    try:
        A.CHECKUSER_TOKEN = 'segredo-token'
        A._checkuser_hits.clear()
        r = c.get('/checkuser?user=teste', headers={'User-Agent': UA})
        check('checkuser: sem token é 403', r.status_code == 403, r.status_code)
        r = c.get('/checkuser?user=teste&token=segredo-token', headers={'User-Agent': UA})
        check('checkuser: com token correto é 200', r.status_code == 200, r.status_code)
    finally:
        A.CHECKUSER_TOKEN = old
        A._checkuser_hits.clear()


# ---------------------------------------------------------------------------
# 7) IP real (X-Forwarded-For falsificado)
# ---------------------------------------------------------------------------
def test_client_ip():
    # cliente externo (peer nao confiavel) forja o XFF -> deve ser ignorado
    with A.app.test_request_context('/login', headers={
            'User-Agent': UA,
            'X-Forwarded-For': '1.2.3.4'},
            environ_base={'REMOTE_ADDR': '203.0.113.9'}):
        ip = A._client_ip()
        check('IP: X-Forwarded-For de cliente nao confiavel e ignorado',
              ip == '203.0.113.9', ip)

    # acesso direto pelo proxy local (nginx) -> o ultimo item do XFF vale
    with A.app.test_request_context('/login', headers={
            'User-Agent': UA,
            'X-Forwarded-For': '1.2.3.4, 198.51.100.7'},
            environ_base={'REMOTE_ADDR': '127.0.0.1'}):
        ip = A._client_ip()
        check('IP: nginx local -> usa o ultimo XFF (IP real)',
              ip == '198.51.100.7', ip)

    # sem XFF, peer local
    with A.app.test_request_context('/login', headers={'User-Agent': UA},
                                    environ_base={'REMOTE_ADDR': '127.0.0.1'}):
        ip = A._client_ip()
        check('IP: sem XFF usa o peer', ip == '127.0.0.1', ip)


# ---------------------------------------------------------------------------
# 8) Backup cifrado
# ---------------------------------------------------------------------------
def test_backup():
    import backup as B
    path = B.create_backup_archive()
    try:
        check('backup: arquivo gerado existe', os.path.exists(path))
        with open(path, 'rb') as f:
            head = f.read(16)
        check('backup: não é um tar.gz em texto puro', not head.startswith(b'\x1f\x8b'),
              head[:4])
        raw = open(path, 'rb').read()
        check('backup: não contém o cabeçalho SQLite em claro',
              b'SQLite format 3' not in raw)
        # decifra e confere que o tar tem o painel.db
        tmp = tempfile.mkdtemp()
        plain = os.path.join(tmp, 'b.tar.gz')
        ok = B._decrypt_file(path, plain)
        check('backup: decifra com a chave do painel', ok)
        if ok:
            with tarfile.open(plain, 'r:gz') as tar:
                nomes = tar.getnames()
            check('backup: contém painel.db', 'painel.db' in nomes, nomes[:5])
    finally:
        if os.path.exists(path):
            os.unlink(path)
        try:
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 9) Página de login sem placeholder "admin"
# ---------------------------------------------------------------------------
def test_login_template():
    c = A.app.test_client()
    html = c.get('/login', headers={'User-Agent': UA}).get_data(as_text=True)
    check('login: sem placeholder com o usuário admin',
          'placeholder="admin"' not in html)


# ---------------------------------------------------------------------------
# 10) Troca de senha de usuário SSH recusa senha fraca
# ---------------------------------------------------------------------------
def test_ssh_password_policy():
    c = A.app.test_client()
    conn = db.get_db()
    try:
        conn.execute("""INSERT INTO panel_users
                        (username, password_hash, role, expires_at)
                        VALUES ('sshpol_admin', ?, 'admin', '2099-12-31')""",
                     (db.hash_password('admin12345'),))
        conn.commit()
    finally:
        conn.close()

    c = A.app.test_client()
    tok = csrf(c)
    c.post('/login', data={'username': 'sshpol_admin', 'password': 'admin12345',
                            'csrf_token': tok}, headers={'User-Agent': UA},
            follow_redirects=False)

    conn = db.get_db()
    try:
        owner_id, server_id = _ids()
        conn.execute("""INSERT INTO ssh_users
                        (username, password, owner_id, server_id, expires_at, connection_limit)
                        VALUES ('pol_ssh', ?, ?, ?, '2099-12-31', 1)""",
                     (db.encrypt_ssh_password('senhaantiga1'), owner_id, server_id))
        conn.commit()
        uid = conn.execute("SELECT id FROM ssh_users WHERE username='pol_ssh'").fetchone()['id']
    finally:
        conn.close()

    tok = csrf(c, '/')
    r = c.post(f'/users/update/{uid}', data={'password': '1234', 'csrf_token': tok},
               headers={'User-Agent': UA}, follow_redirects=False)
    body = r.get_data(as_text=True)
    check('usuário SSH: senha de 4 caracteres recusada',
          (r.status_code in (400, 403)) or ('"success":false' in body.replace(' ', '') or '"success": false' in body),
          f'{r.status_code} {body[:120]}')
    conn = db.get_db()
    try:
        row = conn.execute("SELECT password FROM ssh_users WHERE id=?", (uid,)).fetchone()
        check('usuário SSH: senha não foi alterada para a curta',
              db.ssh_plain_password(row['password']) == 'senhaantiga1')
    finally:
        conn.close()

    tok = csrf(c, '/')
    r = c.post(f'/users/update/{uid}', data={'password': 'abcde', 'csrf_token': tok},
               headers={'User-Agent': UA}, follow_redirects=False)
    conn = db.get_db()
    try:
        row = conn.execute("SELECT password FROM ssh_users WHERE id=?", (uid,)).fetchone()
        check('usuário SSH: senha de 5 caracteres (sem número) aceita',
              db.ssh_plain_password(row['password']) == 'abcde')
    finally:
        conn.close()

    tok = csrf(c, '/')
    r = c.post(f'/users/update/{uid}', data={'password': 'a@b#c-d', 'csrf_token': tok},
               headers={'User-Agent': UA}, follow_redirects=False)
    conn = db.get_db()
    try:
        row = conn.execute("SELECT password FROM ssh_users WHERE id=?", (uid,)).fetchone()
        check('usuário SSH: senha com símbolos aceita',
              db.ssh_plain_password(row['password']) == 'a@b#c-d')
    finally:
        conn.close()

    tok = csrf(c, '/')
    r = c.post(f'/users/update/{uid}', data={'password': 'novasenha1', 'csrf_token': tok},
               headers={'User-Agent': UA}, follow_redirects=False)
    conn = db.get_db()
    try:
        row = conn.execute("SELECT password FROM ssh_users WHERE id=?", (uid,)).fetchone()
        check('usuário SSH: senha forte aceita e gravada cifrada',
              db.ssh_plain_password(row['password']) == 'novasenha1' and
              row['password'].startswith(db._SSH_ENC_PREFIX))
    finally:
        conn.close()

    conn = db.get_db()
    try:
        conn.execute("DELETE FROM ssh_users WHERE username='pol_ssh'")
        conn.execute("DELETE FROM panel_users WHERE username='sshpol_admin'")
        conn.commit()
    finally:
        conn.close()


def main():
    for fn in (test_policy, test_hash, test_encryption, test_migration,
               test_no_forced_change, test_checkuser, test_client_ip,
               test_backup, test_login_template, test_ssh_password_policy):
        print(f'\n--- {fn.__name__} ---')
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f'{fn.__name__} (exceção)', False, e)

    ok = sum(1 for _, c, _ in results if c)
    total = len(results)
    print(f'\n{ok}/{total} testes passaram')
    return 0 if ok == total else 1


if __name__ == '__main__':
    sys.exit(main())
