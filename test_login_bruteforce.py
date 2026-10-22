#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Testes da proteção contra brute-force / rate-limit nos logins.

Não mexe no banco de produção: usa um painel_user de teste (criado e removido
ao final) e monkeypatch nas funções de DB.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as A          # noqa: E402
import db                # noqa: E402

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

TEST_USER = 'bfteste_seg'
TEST_PASS = 'Bfteste#2026'

results = []


def setup_test_user():
    """Cria um usuário de teste (admin) e devolve o id. Remove no final."""
    conn = db.get_db()
    row = conn.execute("SELECT id FROM panel_users WHERE username=?", (TEST_USER,)).fetchone()
    if row:
        conn.execute("DELETE FROM panel_users WHERE id=?", (row['id'],))
    conn.execute(
        "INSERT INTO panel_users (username, password_hash, role, expires_at) VALUES (?,?,?,?)",
        (TEST_USER, db.hash_password(TEST_PASS), 'admin', '2099-12-31')
    )
    conn.commit()
    uid = conn.execute("SELECT id FROM panel_users WHERE username=?", (TEST_USER,)).fetchone()['id']
    conn.close()
    return uid


def teardown_test_user(uid):
    conn = db.get_db()
    conn.execute("DELETE FROM panel_users WHERE id=?", (uid,))
    conn.commit()
    conn.close()


def check(name, cond, extra=''):
    results.append((name, bool(cond), extra))
    print(('PASS  ' if cond else 'FAIL  ') + name + ((' | ' + str(extra)) if extra else ''))


def csrf(client, path='/login'):
    html = client.get(path, headers={'User-Agent': UA}).get_data(as_text=True)
    import re
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ''


def reset_state():
    with A._login_state_lock:
        A._login_fails.clear()
        A._login_locks.clear()
        A._login_strikes.clear()


def main():
    reset_state()
    uid = setup_test_user()
    try:
        return run_tests()
    finally:
        teardown_test_user(uid)
        reset_state()


def run_tests():
    c = A.app.test_client()

    # ------------------------------------------------------------------
    # 1) Login normal continua funcionando
    # ------------------------------------------------------------------
    tok = csrf(c)
    r = c.post('/login', data={'username': TEST_USER, 'password': TEST_PASS,
                                'csrf_token': tok}, headers={'User-Agent': UA},
               follow_redirects=False)
    check('login valido redireciona (302)', r.status_code in (302, 303), r.status_code)
    check('login valido leva ao dashboard', r.headers.get('Location', '') in ('/', '/dashboard'),
          r.headers.get('Location', ''))

    # ------------------------------------------------------------------
    # 2) Bloqueio por (ip + usuario) apos 5 falhas
    # ------------------------------------------------------------------
    reset_state()
    c2 = A.app.test_client()
    blocked_at = None
    for i in range(1, 9):
        tok = csrf(c2)
        r = c2.post('/login', data={'username': 'intrusor', 'password': 'errada%d' % i,
                                    'csrf_token': tok}, headers={'User-Agent': UA},
                   follow_redirects=True)
        body = r.get_data(as_text=True)
        if 'Muitas tentativas' in body or 'temporariamente bloqueado' in body:
            blocked_at = i
            break
    check('bloqueia na 5a tentativa falha', blocked_at == 5, 'bloqueio na tentativa %s' % blocked_at)

    # 7) while blocked, even a correct password does not log in
    tok = csrf(c2)
    r = c2.post('/login', data={'username': 'intrusor', 'password': 'errada6',
                                'csrf_token': tok}, headers={'User-Agent': UA},
               follow_redirects=True)
    check('permanece bloqueado durante a janela', 'Muitas tentativas' in r.get_data(as_text=True))

    # ------------------------------------------------------------------
    # 3) Chave e por (ip+usuario): outro usuario do mesmo IP nao e bloqueado
    # ------------------------------------------------------------------
    reset_state()
    c3 = A.app.test_client()
    for i in range(6):
        tok = csrf(c3)
        c3.post('/login', data={'username': 'alvoA', 'password': 'x%d' % i,
                                'csrf_token': tok}, headers={'User-Agent': UA})
    tok = csrf(c3)
    r = c3.post('/login', data={'username': 'alvoB', 'password': 'x',
                                'csrf_token': tok}, headers={'User-Agent': UA},
               follow_redirects=True)
    check('outro usuario do mesmo IP nao herda o bloqueio do par',
          'Muitas tentativas' not in r.get_data(as_text=True))

    # ------------------------------------------------------------------
    # 4) Limite por IP puro (credential stuffing com usuario variavel)
    # ------------------------------------------------------------------
    reset_state()
    c4 = A.app.test_client()
    ip_blocked = False
    for i in range(40):
        tok = csrf(c4)
        r = c4.post('/login', data={'username': 'user_%d' % i, 'password': 'x',
                                    'csrf_token': tok}, headers={'User-Agent': UA},
                   follow_redirects=True)
        if 'Muitas tentativas' in r.get_data(as_text=True):
            ip_blocked = True
            break
    check('bloqueia por IP apos muitas tentativas com usuarios diferentes', ip_blocked, 'na iteracao %d' % i)

    # ------------------------------------------------------------------
    # 5) Login correto limpa os contadores
    # ------------------------------------------------------------------
    reset_state()
    c5 = A.app.test_client()
    for i in range(3):
        tok = csrf(c5)
        c5.post('/login', data={'username': TEST_USER, 'password': 'errada',
                                'csrf_token': tok}, headers={'User-Agent': UA})
    tok = csrf(c5)
    r = c5.post('/login', data={'username': TEST_USER, 'password': TEST_PASS,
                                'csrf_token': tok}, headers={'User-Agent': UA},
               follow_redirects=False)
    ok_login = r.status_code in (302, 303) and r.headers.get('Location', '') in ('/', '/dashboard')
    check('login correto funciona', ok_login, r.status_code)
    with A._login_state_lock:
        restantes = len([k for k in A._login_fails if TEST_USER in k])
    check('zera contadores apos login valido', restantes == 0, 'restantes=%d' % restantes)


    # ------------------------------------------------------------------
    # 6) /user/login tambem e protegido
    # ------------------------------------------------------------------
    reset_state()
    c6 = A.app.test_client()
    blocked_user = False
    for i in range(8):
        tok = csrf(c6, '/user/login')
        r = c6.post('/user/login', data={'username': 'sshuser', 'password': 'err%d' % i,
                                         'csrf_token': tok}, headers={'User-Agent': UA})
        if r.status_code == 429:
            blocked_user = True
            break
    check('/user/login bloqueia com 429', blocked_user, 'na iteracao %d' % i)

    # ------------------------------------------------------------------
    # 7) Mensagem generica (nao revela se o usuario existe)
    # ------------------------------------------------------------------
    reset_state()
    c7 = A.app.test_client()
    tok = csrf(c7)
    r1 = c7.post('/login', data={'username': 'inexistente_xyz', 'password': 'x',
                                 'csrf_token': tok}, headers={'User-Agent': UA},
                follow_redirects=True)
    tok = csrf(c7)
    r2 = c7.post('/login', data={'username': TEST_USER, 'password': 'x',
                                 'csrf_token': tok}, headers={'User-Agent': UA},
                follow_redirects=True)
    m1 = 'Usuário ou senha inválidos' in r1.get_data(as_text=True)
    m2 = 'Usuário ou senha inválidos' in r2.get_data(as_text=True)
    check('mensagem identica para usuario inexistente e senha errada', m1 and m2, 'm1=%s m2=%s' % (m1, m2))

    # ------------------------------------------------------------------
    # 8) UA e CSRF continuam barrando acesso automatizado
    # ------------------------------------------------------------------
    reset_state()
    c8 = A.app.test_client()
    tok = csrf(c8)
    r = c8.post('/login', data={'username': TEST_USER, 'password': TEST_PASS,
                                'csrf_token': tok}, headers={'User-Agent': 'curl/8.0'})
    check('UA de automacao ainda bloqueada', r.status_code == 403, r.status_code)
    tok = csrf(c8)
    r = c8.post('/login', data={'username': TEST_USER, 'password': TEST_PASS,
                                'csrf_token': 'token-invalido'}, headers={'User-Agent': UA})
    check('CSRF invalido ainda bloqueado', r.status_code == 403, r.status_code)

    reset_state()
    print('\n--------------------------------------------------')
    fails = [r for r in results if not r[1]]
    print('total: %d | passaram: %d | falharam: %d' %
          (len(results), len(results) - len(fails), len(fails)))
    for name, _, extra in fails:
        print('  FALHOU: %s %s' % (name, extra))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
