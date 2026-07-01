#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Painel Master — SSH User Management Panel
"""

import json
import os
import secrets
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from functools import wraps

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, stream_with_context,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import backup as bk
import db
import server_comm as sc

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'),
)
# Support Cloudflare / reverse-proxy: trust X-Forwarded-Proto, X-Forwarded-For
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB for restore
# Work over HTTP locally but send Secure cookies when behind HTTPS proxy
app.config['SESSION_COOKIE_SECURE'] = False   # Cloudflare handles TLS termination
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

TZ = pytz.timezone('America/Sao_Paulo')

try:
    db.init_db()
    db.migrate_schema()
    db.migrate_schema_v2()
    db.migrate_schema_v3()
except Exception as _startup_err:
    import traceback as _tb
    print(f"[PMG STARTUP ERROR] DB init failed: {_startup_err}", flush=True)
    _tb.print_exc()

# ---------------------------------------------------------------------------
# Helpers / decorators
# ---------------------------------------------------------------------------


def now_br():
    return datetime.now(TZ)


def days_until(date_str: str) -> int:
    try:
        exp = datetime.fromisoformat(date_str)
        if exp.tzinfo is None:
            exp = TZ.localize(exp)
        delta = exp - now_br()
        return delta.days
    except Exception:
        return 0


app.jinja_env.globals['days_until'] = days_until
app.jinja_env.globals['now_br'] = now_br


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.method == 'POST':
                return jsonify(success=False, message='Sessão expirada. Faça login novamente.'), 401
            return redirect(url_for('login'))
        # Re-check status on every request — catch suspensions applied while logged in
        user = db.get_panel_user(session['user_id'])
        if not user or user['status'] == 'suspended':
            session.clear()
            if request.method == 'POST':
                return jsonify(success=False, message='Conta suspensa. Contate o administrador.'), 403
            flash('Sua conta foi suspensa. Contate o administrador.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.method == 'POST':
                return jsonify(success=False, message='Sessão expirada. Faça login novamente.'), 401
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            if request.method == 'POST':
                return jsonify(success=False, message='Acesso negado: requer admin.'), 403
            flash('Acesso negado.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


@app.errorhandler(500)
def err_500(e):
    orig = getattr(e, 'original_exception', e)
    tb   = traceback.format_exception(type(orig), orig, orig.__traceback__)
    msg  = ''.join(tb)
    app.logger.error('Erro 500:\n%s', msg)
    if request.method == 'POST':
        short = f"{type(orig).__name__}: {str(orig)}"
        return jsonify(success=False, message=f'Erro interno: {short[:300]}'), 500
    return (f'<h3>Erro 500</h3><pre style="white-space:pre-wrap;font-size:12px">{msg[:3000]}</pre>', 500)


@app.errorhandler(404)
def err_404(e):
    if request.method == 'POST':
        return jsonify(success=False, message='Rota não encontrada'), 404
    return redirect(url_for('dashboard'))


def get_current_user():
    if 'user_id' in session:
        return db.get_panel_user(session['user_id'])
    return None


def get_settings():
    return db.get_all_settings()


# Inject settings and current user into every template
@app.context_processor
def inject_globals():
    return dict(
        settings=get_settings(),
        current_user=get_current_user(),
        panel_name=db.get_setting('panel_name', 'Painel Master'),
        panel_color=db.get_setting('panel_color', '#0d6efd'),
        panel_theme=db.get_setting('panel_theme', 'dark'),
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()

            user = db.get_panel_user_by_username(username)
            if user and db.check_password(password, user['password_hash']):
                if user['status'] == 'suspended':
                    flash('Conta suspensa. Contate o administrador.', 'danger')
                    return redirect(url_for('login'))
                session['user_id'] = int(user['id'])
                session['role']    = str(user['role'])
                session['username']= str(user['username'])
                return redirect(url_for('dashboard'))
            flash('Usuário ou senha inválidos.', 'danger')
        except Exception:
            app.logger.error('Erro no login:\n%s', traceback.format_exc())
            flash('Erro interno ao fazer login. Verifique os logs.', 'danger')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route('/')
@login_required
def dashboard():
    user = get_current_user()
    role = session.get('role')

    if role == 'admin':
        total_ssh = len(db.get_ssh_users(all_users=True))
        total_resellers = len(db.get_resellers())
        total_servers = len(db.get_servers())
        expiring = db.get_expiring_ssh_users(7)
        stats = {
            'total_ssh': total_ssh,
            'total_resellers': total_resellers,
            'total_servers': total_servers,
            'expiring_soon': len(expiring),
        }
        return render_template('admin/dashboard.html', stats=stats, expiring=expiring)
    else:
        # Reseller
        my_users = db.get_ssh_users(owner_id=user['id'])
        sub_resellers = db.get_all_resellers_under(user['id'])
        expiring = [u for u in my_users if days_until(u['expires_at']) <= 7]
        days_panel = days_until(user['expires_at']) if user['expires_at'] else 0
        stats = {
            'total_ssh': len(my_users),
            'total_resellers': len(sub_resellers),
            'account_limit': user['account_limit'],
            'accounts_used': user['accounts_used'],
            'days_panel': days_panel,
        }
        servers = db.get_servers_for_user(user)
        all_cats      = db.get_server_categories()
        avail_cat_ids = set(s['category_id'] for s in servers if s['category_id'])
        avail_cat_ids.update(db.get_reseller_categories(user['id']))
        user_categories = [c for c in all_cats if c['id'] in avail_cat_ids]
        return render_template('reseller/dashboard.html', stats=stats, expiring=expiring,
                               servers=servers, categories=user_categories)


# ---------------------------------------------------------------------------
# SSH Users — shared logic
# ---------------------------------------------------------------------------

def _get_visible_users(current_user, sort='expires_at', search=''):
    role = current_user['role']
    if role == 'admin':
        users = db.get_ssh_users(all_users=True, sort=sort)
    else:
        # Reseller sees their own + all sub-resellers' users
        sub_ids = [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        owner_ids = [current_user['id']] + sub_ids
        users = db.get_ssh_users_for_tree(owner_ids, sort=sort)

    if search:
        search_l = search.lower()
        users = [u for u in users if search_l in u['username'].lower()]
    return users


@app.route('/users')
@login_required
def users_list():
    user = get_current_user()
    sort = request.args.get('sort', 'expires_at')
    search = request.args.get('search', '').strip()
    filter_type = request.args.get('filter', 'all')
    owner_filter = request.args.get('owner_id', '', type=str).strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 50

    users = _get_visible_users(user, sort=sort, search=search)

    if filter_type == 'expiring':
        users = [u for u in users if 0 <= days_until(u['expires_at']) <= 7]
    elif filter_type == 'expiring3':
        users = [u for u in users if 0 <= days_until(u['expires_at']) <= 3]
    elif filter_type == 'expired':
        users = [u for u in users if days_until(u['expires_at']) < 0]
    elif filter_type == 'test':
        users = [u for u in users if u['is_test']]

    if owner_filter and owner_filter.isdigit():
        users = [u for u in users if str(u['owner_id']) == owner_filter]

    total = len(users)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    users_page = users[(page - 1) * per_page: page * per_page]

    servers    = db.get_servers()
    server_map = {s['id']: s for s in servers}
    categories = db.get_server_categories()

    # Categorias disponíveis para o usuário logado (para o modal de criação)
    user_servers = db.get_servers_for_user(user)
    if user['role'] == 'admin':
        user_categories = categories
    else:
        avail_cat_ids = set(s['category_id'] for s in user_servers if s['category_id'])
        avail_cat_ids.update(db.get_reseller_categories(user['id']))
        user_categories = [c for c in categories if c['id'] in avail_cat_ids]

    all_panel_users_map = {}
    for pu in db.get_db().execute("SELECT id, username FROM panel_users").fetchall():
        all_panel_users_map[pu['id']] = pu['username']

    if user['role'] == 'admin':
        all_resellers = db.get_db().execute(
            "SELECT id,username,role FROM panel_users ORDER BY username").fetchall()
    else:
        all_resellers = db.get_all_resellers_under(user['id'])

    return render_template('shared/users.html',
                           users=users_page, servers=user_servers, server_map=server_map,
                           categories=user_categories,
                           owner_map=all_panel_users_map,
                           all_resellers=all_resellers,
                           sort=sort, search=search, filter_type=filter_type,
                           owner_filter=owner_filter,
                           page=page, total_pages=total_pages, total=total,
                           per_page=per_page)


@app.route('/users/create', methods=['GET', 'POST'])
@login_required
def create_user():
    current_user = get_current_user()

    if request.method == 'POST':
        username    = request.form.get('username', '').strip().lower()
        password    = request.form.get('password', '').strip()
        days        = int(request.form.get('days', 30))
        limit       = int(request.form.get('limit', 1))
        category_id = request.form.get('category_id', type=int)
        use_v2ray   = request.form.get('use_v2ray') == '1'
        uuid        = request.form.get('uuid', '').strip() or (db.random_uuid() if use_v2ray else None)

        if current_user['role'] != 'admin':
            if current_user['account_limit'] != -1:
                avail = current_user['account_limit'] - current_user['accounts_used']
                if avail <= 0:
                    return jsonify(success=False, message='Limite de contas atingido')

        if not username:
            return jsonify(success=False, message='Username inválido')
        if not category_id:
            return jsonify(success=False, message='Selecione uma categoria')

        target_servers = db.get_servers_by_category(category_id)
        if not target_servers:
            return jsonify(success=False, message='Nenhum servidor ativo nesta categoria')

        primary_server_id = target_servers[0]['id']
        expires_at = (now_br() + timedelta(days=days)).strftime('%Y-%m-%d')

        # Verifica duplicidade no primeiro servidor da categoria
        first_srv = target_servers[0]
        chk_ok, chk_out = sc.send_command(
            first_srv['ip'], first_srv['module_port'], first_srv['auth_token'],
            f"id {username} 2>/dev/null && echo EXISTS || echo NOTFOUND"
        )
        if chk_ok and 'EXISTS' in chk_out:
            return jsonify(success=False, message=f'Usuário "{username}" já existe no servidor. Escolha outro nome.')

        ok, msg, new_id = db.create_ssh_user(
            username, password, current_user['id'], primary_server_id,
            expires_at, limit, uuid if use_v2ray else None, 0
        )
        if not ok:
            return jsonify(success=False, message=msg)

        # Cria em TODOS os servidores da categoria em paralelo
        def _create_on(srv):
            return sc.create_ssh_user_on_server(
                srv['ip'], srv['module_port'], srv['auth_token'],
                username, password, days, limit, uuid if use_v2ray else None
            )

        server_msg = ''
        ok_count   = 0
        with ThreadPoolExecutor(max_workers=len(target_servers)) as pool:
            futures = {pool.submit(_create_on, srv): srv for srv in target_servers}
            try:
                for fut in as_completed(futures, timeout=30):
                    s_ok, s_msg = fut.result()
                    if s_ok:
                        ok_count += 1
                    if not server_msg:
                        server_msg = s_msg
            except FuturesTimeout:
                server_msg = 'Alguns servidores demoraram e foram ignorados'

        # Registra todos os servidores extras da categoria em ssh_user_servers
        # (o primário já está em ssh_users.server_id; apenas os demais vão aqui)
        # Isso garante que delete/suspend/renew atuem em TODOS os servidores.
        for srv in target_servers[1:]:
            db.add_user_extra_server(new_id, srv['id'])

        app_link = db.get_setting('app_link', '')
        return jsonify(
            success=True,
            message=f'Usuário criado em {ok_count}/{len(target_servers)} servidor(es)',
            user={
                'username':   username,
                'password':   password,
                'expires_at': expires_at,
                'uuid':       uuid if use_v2ray else None,
                'app_link':   app_link,
                'server_msg': server_msg,
            }
        )

    user_servers   = db.get_servers_for_user(current_user)
    all_cats       = db.get_server_categories()
    avail_cat_ids  = set(s['category_id'] for s in user_servers if s['category_id'])
    if current_user['role'] != 'admin':
        avail_cat_ids.update(db.get_reseller_categories(current_user['id']))
        user_categories = [c for c in all_cats if c['id'] in avail_cat_ids]
    else:
        user_categories = all_cats
    return render_template('shared/create_user.html', categories=user_categories, is_test=False)


@app.route('/users/test', methods=['POST'])
@login_required
def create_test():
    current_user = get_current_user()

    username    = request.form.get('username', '').strip().lower()
    password    = request.form.get('password', '').strip()
    hours       = max(1, int(request.form.get('hours', request.form.get('days', 2))))
    limit       = int(request.form.get('limit', 1))
    category_id = request.form.get('category_id', type=int)
    use_v2ray   = request.form.get('use_v2ray') == '1'
    uuid        = db.random_uuid() if use_v2ray else None

    if not category_id:
        return jsonify(success=False, message='Selecione uma categoria para o teste')

    if current_user['role'] != 'admin':
        if current_user['account_limit'] != -1:
            avail = current_user['account_limit'] - current_user['accounts_used']
            if avail <= 0:
                return jsonify(success=False, message='Limite de contas atingido')

    if not username:
        return jsonify(success=False, message='Username inválido')

    target_servers = db.get_servers_by_category(category_id)
    if not target_servers:
        return jsonify(success=False, message='Nenhum servidor ativo nesta categoria')

    primary_server_id = target_servers[0]['id']
    expires_at = (now_br() + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')

    ok, msg, new_id = db.create_ssh_user(
        username, password, current_user['id'], primary_server_id,
        expires_at, limit, uuid if use_v2ray else None, 1
    )
    if not ok:
        return jsonify(success=False, message=msg)

    # Cria em TODOS os servidores da categoria em paralelo
    def _create_test_on(srv):
        return sc.create_test_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            username, password, hours, limit,
            uuid if use_v2ray else None
        )

    server_msg = ''
    ok_count   = 0
    with ThreadPoolExecutor(max_workers=len(target_servers)) as pool:
        futures = {pool.submit(_create_test_on, srv): srv for srv in target_servers}
        try:
            for fut in as_completed(futures, timeout=30):
                s_ok, s_msg = fut.result()
                if s_ok:
                    ok_count += 1
                if not server_msg:
                    server_msg = s_msg
        except FuturesTimeout:
            server_msg = 'Alguns servidores demoraram e foram ignorados'

    # Registra servidores extras da categoria em ssh_user_servers
    # para que delete/suspend/renew atuem em TODOS os servidores
    for srv in target_servers[1:]:
        db.add_user_extra_server(new_id, srv['id'])

    app_link = db.get_setting('app_link', '')
    return jsonify(
        success=True,
        message=f'Teste criado em {ok_count}/{len(target_servers)} servidor(es)',
        user={
            'username':   username,
            'password':   password,
            'expires_at': expires_at,
            'uuid':       uuid if use_v2ray else None,
            'app_link':   app_link,
            'hours':      hours,
            'server_msg': server_msg,
        }
    )



@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')

    # Permission check
    if current_user['role'] != 'admin':
        # Check if owner_id is in current_user's tree
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    # Delete on ALL servers (primary + extras)
    srv_results = sc.broadcast_delete(u, db)
    server_msg = '; '.join(f"{n}:{'OK' if ok else msg}" for n, ok, msg in srv_results) if srv_results else ''

    db.delete_ssh_user(user_id)
    return jsonify(success=True, message='Usuário deletado', server_msg=server_msg)


def _sync_renew_to_category(u):
    """
    Renova (ou cria, se ainda não existir) o usuário SSH em TODOS os servidores
    da categoria do seu servidor primário — não só nos já vinculados em
    ssh_user_servers. Isso resolve o caso de servidores adicionados à
    categoria depois que o usuário já existia no painel.
    Vincula automaticamente qualquer servidor novo da categoria (evita ter
    que ir em 'editar usuário' e adicionar manualmente).
    Retorna lista de (server_name, ok, msg).
    """
    results = []
    if not u['server_id']:
        return results

    primary_srv = db.get_server(u['server_id'])
    if not primary_srv or not primary_srv['category_id']:
        # Sem categoria: mantém comportamento antigo (só primário + extras)
        return sc.broadcast_renew(u, _days_left(u), db)

    cat_servers = db.get_servers_by_category(primary_srv['category_id'])
    if not cat_servers:
        return sc.broadcast_renew(u, _days_left(u), db)

    known_ids = set(db.get_user_all_server_ids(u['id']))
    exp_days = _days_left(u)

    for srv in cat_servers:
        # Vincula o servidor ao usuário se ainda não estiver registrado
        if srv['id'] != u['server_id'] and srv['id'] not in known_ids:
            db.add_user_extra_server(u['id'], srv['id'])

        # Verifica se o usuário já existe nesse servidor específico
        if u['v2ray_uuid']:
            uuid_check_cmd = (
                "python3 -c \""
                "import json\n"
                "try:\n"
                "    cfg=json.load(open('/usr/local/etc/xray/config.json'))\n"
                "    ids=[c.get('id','') for i in cfg.get('inbounds',[])"
                " for c in i.get('settings',{}).get('clients',[])]\n"
                f"    print('UUID_EXISTS' if '{u['v2ray_uuid']}' in ids else 'UUID_MISSING')\n"
                "except Exception:\n"
                "    print('UUID_MISSING')\n"
                "\" 2>/dev/null || echo UUID_MISSING"
            )
            ck_ok, ck_out = sc.send_command(srv['ip'], srv['module_port'], srv['auth_token'], uuid_check_cmd)
            exists = ck_ok and 'UUID_EXISTS' in (ck_out or '')
        else:
            ck_ok, ck_out = sc.send_command(
                srv['ip'], srv['module_port'], srv['auth_token'],
                f"id {u['username']} 2>/dev/null && echo EXISTS || echo MISSING"
            )
            exists = ck_ok and 'EXISTS' in (ck_out or '')

        if exists:
            ok, msg = sc.renew_user_on_server(
                srv['ip'], srv['module_port'], srv['auth_token'], u['username'], exp_days
            )
        else:
            ok, msg = sc.create_ssh_user_on_server(
                srv['ip'], srv['module_port'], srv['auth_token'],
                u['username'], u['password'], exp_days, u['connection_limit'],
                uuid=u['v2ray_uuid']
            )

        results.append((srv['name'], ok, msg))

    return results


def _days_left(u) -> int:
    """Dias restantes a partir de hoje até expires_at (mínimo 1)."""
    try:
        from datetime import date as _date
        exp_date = _date.fromisoformat(u['expires_at'][:10])
        return max(1, (exp_date - now_br().date()).days)
    except Exception:
        return 30


@app.route('/users/renew/<int:user_id>', methods=['POST'])
@login_required
def renew_user(user_id):
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')

    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    days = int(request.form.get('days', 30))
    if days < 1:
        return jsonify(success=False, message='Informe quantos dias renovar')

    was_suspended = u['status'] == 'suspended'
    db.renew_ssh_user(user_id, days)

    # Auto-reativa se estava suspenso — sem precisar clicar no botão de suspender
    if was_suspended:
        db.update_ssh_user(user_id, status='active')
        unlock_cmd = (
            f"passwd -u {u['username']} 2>/dev/null; "
            f"/root/pmaster_agent unblockuser {u['username']} 2>/dev/null || true"
        )
        sc.broadcast_command(u, unlock_cmd, db)

   updated = db.get_ssh_user(user_id)
    srv_results = _sync_renew_to_category(updated)

    srv_results = sc.broadcast_renew(u, days_for_server, db)
    server_msg  = '; '.join(f"{n}:{'OK' if ok else msg}" for n, ok, msg in srv_results) if srv_results else ''

    new_exp    = updated['expires_at'][:10] if updated else ''
    reativado  = ' e reativado' if was_suspended else ''
    return jsonify(success=True,
                   message=f'Renovado{reativado} +{days} dias. Novo vencimento: {new_exp}',
                   server_msg=server_msg, new_expiry=new_exp,
                   was_suspended=was_suspended)


@app.route('/users/update/<int:user_id>', methods=['POST'])
@login_required
def update_user(user_id):
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')

    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    allowed_fields = {'connection_limit', 'status', 'expires_at', 'password'}
    updates = {k: v for k, v in request.form.items() if k in allowed_fields}
    if 'connection_limit' in updates:
        try:
            updates['connection_limit'] = int(updates['connection_limit'])
        except ValueError:
            return jsonify(success=False, message='Limite inválido')

    new_password = updates.pop('password', None)

    if updates:
        db.update_ssh_user(user_id, **updates)

    # Change password on SSH server if requested
    server_msg = ''
    if new_password:
        if len(new_password) < 4:
            return jsonify(success=False, message='Senha muito curta (mínimo 4 caracteres)')
        db.update_ssh_user(user_id, password=new_password)
        if u['server_id']:
            srv = db.get_server(u['server_id'])
            if srv:
                s_ok, server_msg = sc.send_command(
                    srv['ip'], srv['module_port'], srv['auth_token'],
                    f"echo '{u['username']}:{new_password}' | chpasswd"
                )

    # Sincroniza a nova data de expiração com TODOS os servidores (primário + extras)
    # quando expires_at foi editado manualmente neste modal (antes só ia pro banco local)
    # Sincroniza a nova data de expiração com TODOS os servidores da categoria
    # Sincroniza a nova data de expiração com TODOS os servidores da categoria
    if 'expires_at' in updates:
        updated_u = db.get_ssh_user(user_id)
        srv_results = _sync_renew_to_category(updated_u)
        if srv_results:
            exp_msg = '; '.join(f"{n}:{'OK' if ok else msg}" for n, ok, msg in srv_results)
            server_msg = (server_msg + '; ' if server_msg else '') + exp_msg

    return jsonify(success=True, message='Atualizado', server_msg=server_msg)


@app.route('/users/suspend/<int:user_id>', methods=['POST'])
@login_required
def suspend_user(user_id):
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')

    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    new_status = 'suspended' if u['status'] == 'active' else 'active'
    db.update_ssh_user(user_id, status=new_status)

    if new_status == 'suspended':
        # Lock account + kill sessions on ALL servers
        cmd = (
            f"passwd -l {u['username']} 2>/dev/null; "
            f"/root/pmaster_agent blockuser {u['username']} 2>/dev/null || true; "
            f"pkill -u {u['username']} 2>/dev/null || true"
        )
    else:
        # Unlock account on ALL servers
        cmd = (
            f"passwd -u {u['username']} 2>/dev/null; "
            f"/root/pmaster_agent unblockuser {u['username']} 2>/dev/null || true"
        )

    sc.broadcast_command(u, cmd, db)

    return jsonify(success=True, message=f'Status: {new_status}', new_status=new_status)


# ---------------------------------------------------------------------------
# Recreate user on server(s)
# ---------------------------------------------------------------------------

@app.route('/users/recreate_on_server/<int:user_id>', methods=['POST'])
@login_required
def recreate_user_on_server(user_id):
    """Create user on ALL their servers if they don't exist yet. Safe to call multiple times."""
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')
    u = dict(u)  # sqlite3.Row → dict (necessário para usar .get())
    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    server_ids = db.get_user_all_server_ids(user_id)
    if not server_ids:
        return jsonify(success=False, message='Nenhum servidor associado a este usuário')

    try:
        exp_days = max(1, (
            __import__('datetime').date.fromisoformat(u['expires_at'][:10]) -
            now_br().date()
        ).days)
    except Exception:
        exp_days = 30

    results = []
    for sid in server_ids:
        srv = db.get_server(sid)
        if not srv:
            continue

        # ── Verifica existência de forma adequada ao tipo de usuário ──────
        already_exists = False

        if u.get('v2ray_uuid'):
            # Usuário Xray: verifica se o UUID já está no config.json.
            # Checar só o usuário Linux (id username) não é suficiente —
            # o uuid pode estar ausente do config.json mesmo que o user exista.
            uuid_check_cmd = (
                "python3 -c \""
                "import json,sys\n"
                "try:\n"
                "    cfg=json.load(open('/usr/local/etc/xray/config.json'))\n"
                "    ids=[c.get('id','') for i in cfg.get('inbounds',[])"
                " for c in i.get('settings',{}).get('clients',[])]\n"
                f"    print('UUID_EXISTS' if '{u['v2ray_uuid']}' in ids else 'UUID_MISSING')\n"
                "except Exception as e:\n"
                "    print('UUID_MISSING')\n"
                "\" 2>/dev/null || echo UUID_MISSING"
            )
            ck_ok, ck_out = sc.send_command(
                srv['ip'], srv['module_port'], srv['auth_token'], uuid_check_cmd
            )
            already_exists = ck_ok and 'UUID_EXISTS' in (ck_out or '')
        else:
            # Usuário SSH puro: basta verificar se o usuário Linux existe
            ck_ok, ck_out = sc.send_command(
                srv['ip'], srv['module_port'], srv['auth_token'],
                f"id {u['username']} 2>/dev/null && echo EXISTS || echo MISSING"
            )
            already_exists = ck_ok and 'EXISTS' in (ck_out or '')

        if already_exists:
            results.append({'server': srv['name'], 'action': 'já existe', 'ok': True})
            continue

        # ── Cria o usuário (ou adiciona UUID ao Xray se estiver faltando) ──
        c_ok, c_msg = sc.create_ssh_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            u['username'], u['password'], exp_days, u['connection_limit'],
            uuid=u['v2ray_uuid']
        )
        results.append({'server': srv['name'], 'action': 'criado' if c_ok else f'erro: {c_msg}', 'ok': c_ok})

    any_ok = any(r['ok'] for r in results)
    summary = '; '.join(f"{r['server']}: {r['action']}" for r in results)
    return jsonify(success=any_ok, message=summary, results=results)


# ---------------------------------------------------------------------------
# Extra servers per user (multi-server)
# ---------------------------------------------------------------------------

@app.route('/users/<int:user_id>/servers', methods=['GET'])
@login_required
def user_extra_servers(user_id):
    """Return primary + extra servers for a user (used by the edit modal)."""
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')
    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    all_servers = db.get_servers()
    extra_ids   = set(db.get_user_extra_server_ids(user_id))
    primary_id  = u['server_id']

    servers_out = []
    for srv in all_servers:
        servers_out.append({
            'id':      srv['id'],
            'name':    srv['name'],
            'ip':      srv['ip'],
            'primary': srv['id'] == primary_id,
            'extra':   srv['id'] in extra_ids,
        })

    return jsonify(success=True, servers=servers_out, primary_id=primary_id, extra_ids=list(extra_ids))


@app.route('/users/<int:user_id>/servers/add', methods=['POST'])
@login_required
def user_add_extra_server(user_id):
    """Add an extra server to a user and create the account on that server."""
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')
    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    server_id = int(request.form.get('server_id', 0))
    if not server_id:
        return jsonify(success=False, message='Servidor não informado')

    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')

    # Register in DB
    ok, msg = db.add_user_extra_server(user_id, server_id)
    if not ok:
        return jsonify(success=False, message=msg)

    # Create user on the new server
    try:
        exp_days = max(1, (
            __import__('datetime').date.fromisoformat(u['expires_at'][:10]) -
            now_br().date()
        ).days)
    except Exception:
        exp_days = 30

    s_ok, s_msg = sc.create_ssh_user_on_server(
        srv['ip'], srv['module_port'], srv['auth_token'],
        u['username'], u['password'], exp_days, u['connection_limit'],
        uuid=u['v2ray_uuid']
    )

    return jsonify(
        success=True,
        message=f"Usuário adicionado ao servidor {srv['name']}" + ('' if s_ok else f' (aviso servidor: {s_msg})'),
        server_ok=s_ok,
        server_msg=s_msg
    )


@app.route('/users/<int:user_id>/servers/remove', methods=['POST'])
@login_required
def user_remove_extra_server(user_id):
    """Remove an extra server from a user and delete the account on that server."""
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')
    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    server_id = int(request.form.get('server_id', 0))
    if not server_id:
        return jsonify(success=False, message='Servidor não informado')

    # Remove from DB first
    db.remove_user_extra_server(user_id, server_id)

    # Delete from server
    srv = db.get_server(server_id)
    s_ok, s_msg = False, 'Servidor não encontrado'
    if srv:
        s_ok, s_msg = sc.delete_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            u['username'], u['v2ray_uuid']
        )

    return jsonify(
        success=True,
        message=f"Usuário removido do servidor {srv['name'] if srv else server_id}",
        server_ok=s_ok, server_msg=s_msg
    )


# ---------------------------------------------------------------------------
# Resellers
# ---------------------------------------------------------------------------

@app.route('/resellers')
@login_required
def resellers_list():
    current_user = get_current_user()
    all_servers = db.get_servers()

    # ── Filter params ──────────────────────────────────────────────────────
    f_type   = request.args.get('filter', 'all')      # all | mine | sub | expiring
    f_parent = request.args.get('parent_id', '', type=str).strip()  # for sub-filter

    if current_user['role'] == 'admin':
        resellers = db.get_resellers()          # all resellers flat
    else:
        resellers = db.get_all_resellers_under(current_user['id'])

    # Apply filters
    if f_type == 'mine':
        resellers = [r for r in resellers if r['parent_id'] == current_user['id']]
    elif f_type == 'sub' and f_parent and f_parent.isdigit():
        resellers = [r for r in resellers if str(r['parent_id']) == f_parent]
    elif f_type == 'expiring':
        resellers = [r for r in resellers if r['expires_at'] and 0 <= days_until(r['expires_at']) <= 7]

    # Build parent-name map
    parent_map = {}
    for r in resellers:
        if r['parent_id']:
            p = db.get_panel_user(r['parent_id'])
            parent_map[r['id']] = p['username'] if p else '-'

    # Build server assignments map + usage_pct
    server_assign_map = {}
    usage_map = {}
    for r in resellers:
        server_assign_map[r['id']] = db.get_reseller_servers(r['id'])
        lim = r['account_limit']
        used = r['accounts_used'] or 0
        if lim == -1:
            usage_map[r['id']] = {'pct': 0, 'avail': '∞', 'used': used, 'lim': '∞'}
        else:
            pct = round((used / lim) * 100) if lim > 0 else 100
            usage_map[r['id']] = {'pct': min(pct, 100), 'avail': max(lim - used, 0),
                                  'used': used, 'lim': lim}

    # Resellers that current user can filter by (for "sub" dropdown)
    if current_user['role'] == 'admin':
        filter_resellers = db.get_resellers()
    else:
        filter_resellers = db.get_all_resellers_under(current_user['id'])

    return render_template('shared/resellers.html',
                           resellers=resellers, parent_map=parent_map,
                           all_servers=all_servers,
                           all_categories=db.get_server_categories(),
                           server_assign_map=server_assign_map,
                           category_assign_map={r['id']: db.get_reseller_categories(r['id']) for r in resellers},
                           usage_map=usage_map,
                           filter_resellers=filter_resellers,
                           f_type=f_type, f_parent=f_parent)


@app.route('/resellers/create', methods=['POST'])
@login_required
def create_reseller():
    current_user = get_current_user()
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '').strip()
    expires_at = request.form.get('expires_at', '').strip()
    account_limit = int(request.form.get('account_limit', 10))

    if not username or not password or not expires_at:
        return jsonify(success=False, message='Preencha todos os campos')

    # Validate limits
    unlimited = request.form.get('unlimited') == '1'
    if unlimited and current_user['role'] != 'admin':
        return jsonify(success=False, message='Apenas admin pode criar revendas ilimitadas')
    if unlimited:
        account_limit = -1  # -1 = unlimited

    if current_user['role'] != 'admin' and account_limit > 0:
        # Parent must have enough available slots
        parent_avail = current_user['account_limit'] - current_user['accounts_used']
        # If parent itself is unlimited (-1), they can give any amount
        if current_user['account_limit'] != -1 and account_limit > parent_avail:
            return jsonify(success=False, message=f'Limite insuficiente. Disponível: {parent_avail}')

    ok, msg, new_id = db.create_panel_user(
        username, password, 'reseller', current_user['id'], expires_at, account_limit
    )
    if ok and new_id:
        # Assign servers
        server_ids_raw = request.form.get('server_ids', '')
        if server_ids_raw:
            sids = [int(x) for x in server_ids_raw.split(',') if x.strip().isdigit()]
            db.set_reseller_servers(new_id, sids)
        panel_url = request.host_url.rstrip('/')
        app_link = db.get_setting('app_link', '')
        return jsonify(success=True, message=msg, reseller={
            'username': username,
            'password': password,
            'expires_at': expires_at,
            'panel_url': panel_url,
            'app_link': app_link,
        })
    return jsonify(success=ok, message=msg)


@app.route('/resellers/set_password/<int:reseller_id>', methods=['POST'])
@login_required
def set_reseller_password(reseller_id):
    current_user = get_current_user()
    r = db.get_panel_user(reseller_id)
    if not r:
        return jsonify(success=False, message='Revenda não encontrada')
    if current_user['role'] != 'admin':
        tree = db.get_all_resellers_under(current_user['id'])
        if reseller_id not in [t['id'] for t in tree]:
            return jsonify(success=False, message='Sem permissão')
    new_pw = request.form.get('password', '').strip()
    if len(new_pw) < 4:
        return jsonify(success=False, message='Senha muito curta (mínimo 4 caracteres)')
    db.update_panel_user(reseller_id,
                         password_hash=db.hash_password(new_pw),
                         password_plain=new_pw)
    return jsonify(success=True, message='Senha alterada com sucesso')


@app.route('/resellers/set_servers/<int:reseller_id>', methods=['POST'])
@login_required
def set_reseller_servers_route(reseller_id):
    current_user = get_current_user()
    if current_user['role'] != 'admin':
        return jsonify(success=False, message='Apenas admin pode atribuir servidores')
    server_ids_raw = request.form.get('server_ids', '')
    sids = [int(x) for x in server_ids_raw.split(',') if x.strip().isdigit()]
    db.set_reseller_servers(reseller_id, sids)
    return jsonify(success=True, message=f'{len(sids)} servidor(es) atribuído(s)')


@app.route('/resellers/set_categories/<int:reseller_id>', methods=['POST'])
@login_required
def set_reseller_categories_route(reseller_id):
    current_user = get_current_user()
    if current_user['role'] != 'admin':
        return jsonify(success=False, message='Apenas admin pode atribuir categorias')
    cat_ids_raw = request.form.get('category_ids', '')
    cids = [int(x) for x in cat_ids_raw.split(',') if x.strip().isdigit()]
    db.set_reseller_categories(reseller_id, cids)
    return jsonify(success=True, message=f'{len(cids)} categoria(s) atribuída(s)')


@app.route('/resellers/delete/<int:reseller_id>', methods=['POST'])
@login_required
def delete_reseller(reseller_id):
    current_user = get_current_user()
    r = db.get_panel_user(reseller_id)
    if not r:
        return jsonify(success=False, message='Revenda não encontrada')

    if current_user['role'] != 'admin':
        # Only allowed if it's a direct child or in their tree
        tree = db.get_all_resellers_under(current_user['id'])
        tree_ids = [t['id'] for t in tree]
        if reseller_id not in tree_ids:
            return jsonify(success=False, message='Sem permissão')

    # Collect full subtree: this reseller + ALL child resellers recursively
    tree_resellers = [reseller_id] + [s['id'] for s in db.get_all_resellers_under(reseller_id)]
    conn = db.get_db()
    affected_users = conn.execute(
        f"SELECT * FROM ssh_users WHERE owner_id IN ({','.join('?'*len(tree_resellers))})",
        tree_resellers
    ).fetchall()
    conn.close()

    # Delete SSH users from all their servers first
    for u in affected_users:
        sc.broadcast_delete(u, db)
        db.delete_ssh_user(u['id'])

    # Delete child resellers (deepest first to avoid FK issues)
    children = db.get_all_resellers_under(reseller_id)
    for child in reversed(children):
        db.delete_panel_user(child['id'])

    # Finally delete the reseller itself
    db.delete_panel_user(reseller_id)
    return jsonify(success=True, message=f'Revenda, {len(children)} sub-revenda(s) e {len(affected_users)} usuário(s) deletados')


@app.route('/resellers/suspend/<int:reseller_id>', methods=['POST'])
@login_required
def suspend_reseller(reseller_id):
    current_user = get_current_user()
    r = db.get_panel_user(reseller_id)
    if not r:
        return jsonify(success=False, message='Revenda não encontrada')
    if current_user['role'] != 'admin':
        tree = db.get_all_resellers_under(current_user['id'])
        if reseller_id not in [t['id'] for t in tree]:
            return jsonify(success=False, message='Sem permissão')
    new_status = 'suspended' if r['status'] == 'active' else 'active'

    # Suspend/unsuspend the reseller AND all child resellers recursively
    children = db.get_all_resellers_under(reseller_id)
    all_reseller_ids = [reseller_id] + [c['id'] for c in children]
    for rid in all_reseller_ids:
        db.update_panel_user(rid, status=new_status)

    # Get ALL SSH users under the entire tree
    conn = db.get_db()
    affected_users = conn.execute(
        f"SELECT * FROM ssh_users WHERE owner_id IN ({','.join('?'*len(all_reseller_ids))})",
        all_reseller_ids
    ).fetchall()
    conn.close()

    if new_status == 'suspended':
        # Lock accounts + kill sessions on ALL servers for every user
        for u in affected_users:
            db.update_ssh_user(u['id'], status='suspended')
            lock_cmd = (
                f"passwd -l {u['username']} 2>/dev/null; "
                f"/root/pmaster_agent blockuser {u['username']} 2>/dev/null || true; "
                f"pkill -u {u['username']} 2>/dev/null || true"
            )
            sc.broadcast_command(u, lock_cmd, db)
    else:
        # Reactivate: unlock accounts on ALL servers
        for u in affected_users:
            db.update_ssh_user(u['id'], status='active')
            unlock_cmd = (
                f"passwd -u {u['username']} 2>/dev/null; "
                f"/root/pmaster_agent unblockuser {u['username']} 2>/dev/null || true"
            )
            sc.broadcast_command(u, unlock_cmd, db)

    return jsonify(
        success=True,
        new_status=new_status,
        affected_users=len(affected_users),
        affected_resellers=len(all_reseller_ids)
    )


@app.route('/resellers/renew/<int:reseller_id>', methods=['POST'])
@login_required
def renew_reseller(reseller_id):
    current_user = get_current_user()
    r = db.get_panel_user(reseller_id)
    if not r:
        return jsonify(success=False, message='Revenda não encontrada')
    # Admin can renew any reseller; reseller can renew only their own subtree
    if current_user['role'] != 'admin':
        tree_ids = [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if reseller_id not in tree_ids:
            return jsonify(success=False, message='Sem permissão para renovar esta revenda')
    days = int(request.form.get('days', 30))
    if days < 1:
        return jsonify(success=False, message='Informe quantos dias renovar')
    db.renew_panel_user(reseller_id, days)
    updated = db.get_panel_user(reseller_id)
    return jsonify(success=True, message=f'Renovado +{days} dias', new_expiry=updated['expires_at'])


@app.route('/resellers/update/<int:reseller_id>', methods=['POST'])
@login_required
def update_reseller(reseller_id):
    """Any ancestor in the tree can update a reseller's limit (limited to their own available slots)."""
    current_user = get_current_user()
    r = db.get_panel_user(reseller_id)
    if not r:
        return jsonify(success=False, message='Revenda não encontrada')

    # Build the ancestor chain of reseller_id to check permission
    def is_ancestor(ancestor_id, target_id):
        node = db.get_panel_user(target_id)
        while node and node['parent_id']:
            if node['parent_id'] == ancestor_id:
                return True
            node = db.get_panel_user(node['parent_id'])
        return False

    if current_user['role'] != 'admin':
        if r['parent_id'] != current_user['id'] and not is_ancestor(current_user['id'], reseller_id):
            return jsonify(success=False, message='Sem permissão para editar esta revenda')

    updates = {}
    new_limit = request.form.get('account_limit')
    new_expires = request.form.get('expires_at')

    if new_limit is not None:
        try:
            new_limit = int(new_limit)
        except ValueError:
            return jsonify(success=False, message='Limite inválido')

        old_limit = r['account_limit'] if r['account_limit'] else 0

        # Non-admin: check they have enough available slots
        if current_user['role'] != 'admin' and new_limit > 0:
            cu = get_current_user()
            cu_lim = cu['account_limit']
            cu_used = cu['accounts_used'] or 0
            if cu_lim != -1:
                # available = total - already assigned to others (excluding what this reseller already holds)
                available = cu_lim - cu_used + max(old_limit, 0)
                if new_limit > available:
                    return jsonify(success=False,
                                   message=f'Você só tem {available} slots disponíveis')

        # Adjust parent's accounts_used by the delta
        if r['parent_id']:
            delta = new_limit - max(old_limit, 0)
            if delta != 0:
                conn = db.get_db()
                conn.execute(
                    "UPDATE panel_users SET accounts_used = MAX(0, accounts_used + ?) WHERE id=?",
                    (delta, r['parent_id'])
                )
                conn.commit()
                conn.close()

        updates['account_limit'] = new_limit

    if new_expires:
        updates['expires_at'] = new_expires

    if not updates:
        return jsonify(success=False, message='Nada a atualizar')

    db.update_panel_user(reseller_id, **updates)
    return jsonify(success=True, message='Revenda atualizada')


# ---------------------------------------------------------------------------
# Servers (admin only)
# ---------------------------------------------------------------------------

@app.route('/servers')
@admin_required
def servers_list():
    servers = db.get_servers()
    categories = db.get_server_categories()
    cat_map = {c['id']: c['name'] for c in categories}
    return render_template('admin/servers.html', servers=servers, categories=categories, cat_map=cat_map)


@app.route('/servers/add', methods=['POST'])
@admin_required
def add_server():
    name = request.form.get('name', '').strip()
    ip = request.form.get('ip', '').strip()
    module_port = int(request.form.get('module_port', 7270))
    root_user = request.form.get('root_user', 'root').strip()
    root_password = request.form.get('root_password', '').strip()
    auth_token = request.form.get('auth_token', db.random_password(22)).strip()

    if not name or not ip:
        return jsonify(success=False, message='Nome e IP são obrigatórios')

    new_id = db.add_server(name, ip, module_port, root_user, root_password, auth_token)
    # Assign category if provided
    cat_id = request.form.get('category_id', '').strip()
    if cat_id and cat_id.isdigit():
        db.update_server(new_id, category_id=int(cat_id))
    return jsonify(success=True, message='Servidor adicionado', server_id=new_id)


@app.route('/servers/delete/<int:server_id>', methods=['POST'])
@admin_required
def delete_server(server_id):
    db.delete_server(server_id)
    return jsonify(success=True, message='Servidor removido')


@app.route('/servers/edit/<int:server_id>', methods=['POST'])
@admin_required
def edit_server(server_id):
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')
    updates = {}
    for field in ('name', 'ip', 'module_port', 'root_user', 'root_password', 'auth_token', 'category_id'):
        val = request.form.get(field, '').strip()
        if val != '':
            updates[field] = int(val) if field in ('module_port', 'category_id') else val
    db.update_server(server_id, **updates)
    return jsonify(success=True, message='Servidor atualizado')


@app.route('/categories/add', methods=['POST'])
@admin_required
def add_category():
    name = request.form.get('name', '').strip()
    if not name:
        return jsonify(success=False, message='Nome obrigatório')
    try:
        cat_id = db.add_server_category(name)
        return jsonify(success=True, message='Categoria criada', id=cat_id)
    except Exception as e:
        return jsonify(success=False, message=str(e))


@app.route('/categories/delete/<int:cat_id>', methods=['POST'])
@admin_required
def delete_category(cat_id):
    db.delete_server_category(cat_id)
    return jsonify(success=True, message='Categoria removida')


@app.route('/servers/install_modules/<int:server_id>', methods=['POST'])
@admin_required
def install_modules(server_id):
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')
    ok, msg = sc.install_modules_ssh(srv['ip'], srv['root_user'], srv['root_password'], srv['auth_token'])
    return jsonify(success=ok, message=msg)


@app.route('/servers/sync/<int:server_id>', methods=['POST'])
@admin_required
def sync_server(server_id):
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')

    # force=true: envia TODOS os usuários ativos do painel (ignora vinculação ao servidor)
    # Útil para servidor novo onde nenhum usuário está ainda vinculado.
    # force=false: envia apenas usuários vinculados a este servidor (primário ou extra).
    force = request.form.get('force', 'false').lower() in ('1', 'true', 'yes')

    conn = db.get_db()
    if force:
        # Sync Total: todos os usuários ativos do painel, qualquer servidor
        rows = conn.execute(
            "SELECT * FROM ssh_users WHERE status='active' ORDER BY expires_at"
        ).fetchall()
    else:
        # Sync normal: apenas os vinculados a este servidor
        user_ids = db.get_users_on_server(server_id)
        if user_ids:
            placeholders = ','.join('?' * len(user_ids))
            rows = conn.execute(
                f"SELECT * FROM ssh_users WHERE id IN ({placeholders}) AND status='active'",
                user_ids
            ).fetchall()
        else:
            rows = []
    conn.close()

    users_list = [dict(u) for u in rows]
    ok, msg = sc.sync_users_to_server(srv['ip'], srv['module_port'], srv['auth_token'], users_list, force=force)
    return jsonify(success=ok, message=msg, users_count=len(users_list))


@app.route('/servers/command/<int:server_id>', methods=['POST'])
@admin_required
def server_command(server_id):
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')

    command = request.form.get('command', '').strip()
    allowed = ['iptables -F', 'reboot', 'systemctl restart xray', 'systemctl restart v2ray',
               'df -h', 'free -m', 'uptime', 'who']
    # Only allow predefined or custom commands if admin explicitly confirms
    if not command:
        return jsonify(success=False, message='Comando vazio')

    ok, output = sc.send_command(srv['ip'], srv['module_port'], srv['auth_token'], command)
    return jsonify(success=ok, output=output)


@app.route('/servers/stats/<int:server_id>')
@admin_required
def server_stats_sse(server_id):
    """SSE endpoint for real-time CPU/RAM stats."""
    srv = db.get_server(server_id)
    if not srv:
        abort(404)

    def generate():
        import time
        while True:
            stats = sc.get_cpu_mem(srv['ip'], srv['module_port'], srv['auth_token'])
            data = json.dumps(stats)
            yield f"data: {data}\n\n"
            time.sleep(3)

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={
                        'Cache-Control': 'no-cache',
                        'X-Accel-Buffering': 'no',
                    })


# ---------------------------------------------------------------------------
# Online users
# ---------------------------------------------------------------------------

@app.route('/online')
@login_required
def online_users():
    current_user = get_current_user()
    servers      = db.get_servers_for_user(current_user)
    categories   = db.get_server_categories()
    cat_map      = {c['id']: c['name'] for c in categories}
    if current_user['role'] == 'admin':
        all_resellers = db.get_resellers()
    else:
        all_resellers = db.get_all_resellers_under(current_user['id'])
    return render_template('shared/online.html', servers=servers,
                           cat_map=cat_map, all_resellers=all_resellers)


@app.route('/api/online/<int:server_id>')
@login_required
def api_online(server_id):
    current_user = get_current_user()
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(online=[])

    online_raw = sc.get_online_users_robust(srv['ip'], srv['module_port'], srv['auth_token'])
    # online_raw is list of {username, connections}

    # Filter by permission
    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        conn = db.get_db()
        my_usernames = set(
            r['username'] for r in conn.execute(
                f"SELECT username FROM ssh_users WHERE owner_id IN ({','.join('?'*len(tree_ids))})",
                tree_ids
            ).fetchall()
        )
        conn.close()
        online_raw = [o for o in online_raw if o['username'] in my_usernames]

    # Enrich with panel info (limit, owner, etc.)
    result = []
    for item in online_raw:
        u = db.get_ssh_user_by_username(item['username'])
        owner_name = ''
        if u:
            pu = db.get_panel_user(u['owner_id'])
            owner_name = pu['username'] if pu else ''
        result.append({
            'username': item['username'],
            'connections': item['connections'],
            'limit': u['connection_limit'] if u else '?',
            'owner': owner_name,
            'expires_at': u['expires_at'][:10] if u and u['expires_at'] else '',
        })

    return jsonify(online=result)


# ---------------------------------------------------------------------------
# Settings (admin)
# ---------------------------------------------------------------------------

@app.route('/checkuser_panel')
@admin_required
def checkuser_panel():
    settings = db.get_all_settings()
    return render_template('checkuser_panel.html', settings=settings)


@app.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    if request.method == 'POST':
        for key in ['panel_name', 'panel_color', 'panel_theme', 'app_link', 'checkuser_url']:
            val = request.form.get(key, '').strip()
            if val is not None:
                db.set_setting(key, val)
        flash('Configurações salvas!', 'success')
        return redirect(url_for('settings'))

    return render_template('admin/settings.html')


@app.route('/settings/reset', methods=['POST'])
@admin_required
def system_reset():
    """Hard reset: wipe SSH users, resellers, payments. Keeps admin account and settings."""
    confirm_pass = request.form.get('confirm_password', '').strip()
    current_user = get_current_user()
    # password_hash is SHA-256 of the plain text password
    if not db.check_password(confirm_pass, current_user['password_hash']):
        return jsonify(success=False, message='Senha incorreta. Reset cancelado.')
    try:
        conn = db.get_db()
        # Delete everything except the admin account and global settings
        conn.execute("DELETE FROM ssh_users")
        conn.execute("DELETE FROM payments")
        conn.execute("DELETE FROM panel_users WHERE role != 'admin'")
        conn.execute("DELETE FROM reseller_servers")
        conn.commit()
        conn.close()
        return jsonify(success=True, message='Sistema zerado com sucesso.')
    except Exception as e:
        return jsonify(success=False, message=f'Erro: {e}')


# ---------------------------------------------------------------------------
# Backup (admin)
# ---------------------------------------------------------------------------

@app.route('/backup', methods=['GET', 'POST'])
@admin_required
def backup_page():
    cfg = db.get_backup_config()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'save_config':
            bot_token = request.form.get('bot_token', '').strip()
            chat_id   = request.form.get('chat_id', '').strip()
            enabled   = 1 if request.form.get('enabled') else 0
            wh_url    = request.form.get('webhook_url', '').strip()
            db.set_backup_config(bot_token, chat_id, enabled, wh_url)
            flash('Configuração de backup salva!', 'success')

        elif action == 'backup_now':
            cfg = db.get_backup_config()
            if cfg and cfg['telegram_bot_token'] and cfg['telegram_chat_id']:
                ok, msg = bk.send_backup_telegram(cfg['telegram_bot_token'], cfg['telegram_chat_id'])
                if ok:
                    db.update_last_backup(now_br().strftime('%Y-%m-%d %H:%M:%S'))
                    flash(f'Backup enviado: {msg}', 'success')
                else:
                    flash(f'Erro no backup: {msg}', 'danger')
            else:
                flash('Configure o Telegram primeiro.', 'warning')

        elif action == 'restore':
            f = request.files.get('backup_file')
            if f:
                ok, msg = bk.restore_backup(f)
                flash(msg, 'success' if ok else 'danger')
            else:
                flash('Selecione um arquivo de backup.', 'warning')

        return redirect(url_for('backup_page'))

    return render_template('admin/backup.html', cfg=cfg)


# ---------------------------------------------------------------------------
# MercadoPago config
# ---------------------------------------------------------------------------

@app.route('/mercadopago', methods=['GET', 'POST'])
@login_required
def mercadopago_config():
    current_user = get_current_user()

    if request.method == 'POST':
        token = request.form.get('mp_token', '').strip()
        price = float(request.form.get('mp_price', 0))
        db.update_panel_user(current_user['id'], mercadopago_token=token, mercadopago_price=price)
        flash('Configuração do Mercado Pago salva!', 'success')
        return redirect(url_for('mercadopago_config'))

    # Refresh user
    current_user = db.get_panel_user(current_user['id'])
    payments = db.get_payments(owner_id=current_user['id'])
    return render_template('shared/mercadopago.html', payments=payments)


@app.route('/api/online_usernames')
@login_required
def api_online_usernames():
    """Return set of online usernames across all visible servers (for users list indicators)."""
    current_user = get_current_user()
    if current_user['role'] == 'admin':
        servers = db.get_servers()
    else:
        # Only servers accessible to this user's tree
        srv_ids = set()
        conn = db.get_db()
        rows = conn.execute(
            "SELECT DISTINCT server_id FROM ssh_users WHERE owner_id=? AND server_id IS NOT NULL",
            (current_user['id'],)
        ).fetchall()
        conn.close()
        srv_ids = {r['server_id'] for r in rows}
        servers = [db.get_server(sid) for sid in srv_ids if db.get_server(sid)]

    online_set = set()
    for srv in servers:
        if not srv:
            continue
        raw = sc.get_online_users_robust(srv['ip'], srv['module_port'], srv['auth_token'])
        for item in raw:
            online_set.add(item['username'].lower())

    return jsonify(online=list(online_set))


@app.route('/users/bulk_delete', methods=['POST'])
@login_required
def bulk_delete_users():
    """Delete multiple users at once. Only allows deleting users visible to current user."""
    current_user = get_current_user()
    ids_raw = request.form.get('ids', '')
    try:
        ids = [int(x) for x in ids_raw.split(',') if x.strip()]
    except ValueError:
        return jsonify(success=False, message='IDs inválidos')

    if not ids:
        return jsonify(success=False, message='Nenhum usuário selecionado')

    deleted = 0
    for uid in ids:
        u = db.get_ssh_user(uid)
        if not u:
            continue
        # Permission check: admin can delete any, reseller only their tree
        if current_user['role'] != 'admin':
            visible_ids = [v['id'] for v in _get_visible_users(current_user)]
            if uid not in visible_ids:
                continue
        # Remove from ALL servers (primary + extras)
        sc.broadcast_delete(u, db)
        db.delete_ssh_user(uid)
        deleted += 1

    return jsonify(success=True, message=f'{deleted} usuário(s) excluído(s)')


@app.route('/settings/recalculate', methods=['POST'])
@admin_required
def recalculate_limits():
    """Recalculate accounts_used for all panel users from actual data."""
    try:
        db.recalculate_accounts_used()
        return jsonify(success=True, message='Limites recalculados com sucesso!')
    except Exception as e:
        return jsonify(success=False, message=str(e))


def _public_base_url() -> str:
    """Build the correct public-facing base URL, respecting Cloudflare/proxy headers."""
    proto = request.headers.get('X-Forwarded-Proto', request.scheme)
    host  = request.headers.get('X-Forwarded-Host',
                                request.headers.get('Host', request.host))
    return f"{proto}://{host}"


@app.route('/mercadopago/delete/<int:payment_id>', methods=['POST'])
@login_required
def delete_payment(payment_id):
    current_user = get_current_user()
    conn = db.get_db()
    payment = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    conn.close()
    if not payment:
        return jsonify(success=False, message='Pagamento não encontrado')
    if current_user['role'] != 'admin' and payment['owner_id'] != current_user['id']:
        return jsonify(success=False, message='Sem permissão')
    db.delete_payment(payment_id)
    return jsonify(success=True, message='Pagamento excluído')


@app.route('/resellers/transfer/<int:reseller_id>', methods=['POST'])
@login_required
def transfer_reseller(reseller_id):
    """Admin pulls a reseller from any tree into a new parent (or directly under admin)."""
    current_user = get_current_user()
    if current_user['role'] != 'admin':
        return jsonify(success=False, message='Apenas admin pode transferir revendas')
    r = db.get_panel_user(reseller_id)
    if not r:
        return jsonify(success=False, message='Revenda não encontrada')
    new_parent_id = request.form.get('new_parent_id', type=int)
    # new_parent_id=None means make it a direct child of admin (top-level reseller)
    db.update_panel_user(reseller_id, parent_id=new_parent_id)
    parent_name = 'admin (topo)' if not new_parent_id else (
        db.get_panel_user(new_parent_id) or {}).get('username', str(new_parent_id))
    return jsonify(success=True, message=f'Revenda transferida para {parent_name}')


@app.route('/mercadopago/webhook', methods=['POST'])
def mp_webhook():
    """Webhook for MercadoPago payment notifications."""
    data = request.json or {}
    if data.get('type') == 'payment':
        payment_id = str(data.get('data', {}).get('id', ''))
        # Look up which panel user has a pending payment
        conn = db.get_db()
        payment = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
        conn.close()

        if payment and payment['status'] == 'pending':
            # Verify with MercadoPago API
            owner = db.get_panel_user(payment['owner_id'])
            if owner and owner['mercadopago_token']:
                try:
                    resp = requests.get(
                        f"https://api.mercadopago.com/v1/payments/{payment_id}",
                        headers={'Authorization': f"Bearer {owner['mercadopago_token']}"},
                        timeout=10
                    )
                    mp_data = resp.json()
                    if mp_data.get('status') == 'approved':
                        db.update_payment_status(payment_id, 'approved')
                        # Renew expiry +30 days stacked on top of current expiry
                        db.renew_ssh_user(payment['ssh_user_id'], 30)
                        # Reactivate user (may have been suspended due to expiry)
                        db.update_ssh_user(payment['ssh_user_id'], status='active')
                        u = db.get_ssh_user(payment['ssh_user_id'])
                        if u:
                            # Renova/cria em TODOS os servidores da categoria (não só nos já vinculados)
                            _sync_renew_to_category(u)
                            unlock_cmd = (
                                f"passwd -u {u['username']} 2>/dev/null; "
                                f"/root/pmaster_agent unblockuser {u['username']} 2>/dev/null || true"
                            )
                            sc.broadcast_command(u, unlock_cmd, db)
                            unlock_cmd = (
                                f"passwd -u {u['username']} 2>/dev/null; "
                                f"/root/pmaster_agent unblockuser {u['username']} 2>/dev/null || true"
                            )
                            sc.broadcast_command(u, unlock_cmd, db)
                except Exception:
                    pass

    return jsonify(status='ok')


# ---------------------------------------------------------------------------
# User login page (show renewal QR code)
# ---------------------------------------------------------------------------

@app.route('/user/login', methods=['GET', 'POST'])
def user_login():
    """Page where SSH users can see expiry and pay renewal."""
    error = None
    user_data = None

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        u = db.get_ssh_user_by_username(username)

        if u and u['password'] == password:
            user_data = dict(u)
            # Store in session so generate_pix can use it without re-auth
            session['pix_user_id'] = u['id']
        else:
            error = 'Usuário ou senha inválidos'

    return render_template('user_login.html', user_data=user_data, error=error)


@app.route('/user/generate_pix', methods=['POST'])
def user_generate_pix():
    """AJAX: generate a PIX payment code for the logged-in SSH user."""
    user_id = session.get('pix_user_id')
    if not user_id:
        return jsonify(success=False, message='Sessão expirada. Faça login novamente.')

    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado.')

    _owner_row = db.get_panel_user(u['owner_id'])
    owner = dict(_owner_row) if _owner_row else None
    if not owner or not owner.get('mercadopago_token') or not (owner.get('mercadopago_price') or 0) > 0:
        return jsonify(success=False, message='Pagamento não configurado pelo seu provedor.')

    try:
        import urllib.parse
        public_base  = _public_base_url()
        notif_url    = public_base + url_for('mp_webhook')
        external_ref = f"{u['id']}_{owner['id']}"

        # MP rejeita domínios locais; sanitizar username e usar TLD válido
        _safe_user = ''.join(c for c in u['username'] if c.isalnum() or c in '-_') or 'pagador'
        pix_payload = {
            "transaction_amount": float(owner['mercadopago_price']),
            "description": f"Renovacao {u['username']} - 30 dias",
            "payment_method_id": "pix",
            "payer": {"email": f"{_safe_user}@email.com"},
            "notification_url": notif_url,
            "external_reference": external_ref,
        }
        pix_resp = requests.post(
            "https://api.mercadopago.com/v1/payments",
            headers={
                'Authorization': f"Bearer {owner['mercadopago_token']}",
                'Content-Type': 'application/json',
                'X-Idempotency-Key': f"renov-{u['id']}-{int(now_br().timestamp())}",
            },
            json=pix_payload,
            timeout=15
        )
        if pix_resp.status_code in (200, 201):
            pix_data       = pix_resp.json()
            pix_payment_id = str(pix_data.get('id', ''))
            ti             = ((pix_data.get('point_of_interaction') or {})
                               .get('transaction_data') or {})
            pix_copy_code  = ti.get('qr_code', '')
            pix_qr_b64     = ti.get('qr_code_base64', '')

            qr_code = None
            if pix_qr_b64:
                qr_code = f"data:image/png;base64,{pix_qr_b64}"
            elif pix_copy_code:
                qr_code = (f"https://api.qrserver.com/v1/create-qr-code/"
                           f"?data={urllib.parse.quote(pix_copy_code, safe='')}"
                           f"&size=220x220&format=png")

            db.add_payment(u['id'], owner['id'],
                           owner['mercadopago_price'],
                           pix_payment_id, '', 'pending')

            # Save payment_id in session so frontend can poll status
            session['pix_payment_id'] = pix_payment_id

            return jsonify(
                success=True,
                pix_copy_code=pix_copy_code,
                qr_code=qr_code,
                amount=f"R$ {owner['mercadopago_price']:.2f}"
            )
        else:
            err_msg = pix_resp.json().get('message', 'Erro desconhecido')
            return jsonify(success=False, message=f'Erro ao gerar PIX: {err_msg}')

    except Exception as exc:
        app.logger.error('Erro generate_pix: %s', traceback.format_exc())
        return jsonify(success=False, message=f'Erro interno: {str(exc)[:200]}')


@app.route('/user/check_payment')
def user_check_payment():
    """AJAX: poll payment status after PIX was generated."""
    payment_id = session.get('pix_payment_id')
    if not payment_id:
        return jsonify(status='unknown')

    conn = db.get_db()
    payment = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
    conn.close()

    if not payment:
        return jsonify(status='unknown')

    if payment['status'] == 'approved':
        u = db.get_ssh_user(payment['ssh_user_id'])
        new_expiry = u['expires_at'][:10] if u else ''
        session.pop('pix_payment_id', None)
        return jsonify(status='approved', new_expiry=new_expiry)

    return jsonify(status=payment['status'])


# ---------------------------------------------------------------------------
# CheckUser API
# ---------------------------------------------------------------------------

def _clean_username(username: str) -> str:
    """Strip path prefixes and query-string params that some VPN apps include.
    e.g. '/check/jean55?deviceid=123' → 'jean55'
    """
    # Remove query string
    if '?' in username:
        username = username.split('?')[0]
    # Remove leading slashes / path components
    if '/' in username:
        username = username.rstrip('/').rsplit('/', 1)[-1]
    return username.strip()


def _get_user_connections(username: str) -> int:
    """Get current active connections for a user from all servers."""
    total_connections = 0
    servers = db.get_all_servers()
    
    for server in servers:
        try:
            # Tenta obter conexões ativas do servidor
            ok, output = send_command(
                server['ip'], 
                server['module_port'], 
                server['auth_token'],
                f"who | grep -c '^{username} ' || echo 0"
            )
            if ok:
                total_connections += int(output.strip() or 0)
        except Exception:
            continue
    
    return total_connections


def _checkuser_response(username: str):
    """CheckUser response matching standard format used by VPN apps."""
    username = _clean_username(username)

    # 1) Try by username (SSH users)
    u = db.get_ssh_user_by_username(username)

    # 2) Fallback: V2ray/Xray sends UUID as identifier
    if not u:
        u = db.get_ssh_user_by_uuid(username)

    if not u:
        resp = {
            "id": "01",
            "username": username,
            "count_connections": 0,
            "limit_connections": 0,
            "expiration_date": "",
            "expiration_days": "0",
        }
        return jsonify(resp)

    # Converte sqlite3.Row para dict para facilitar (opcional)
    if hasattr(u, 'keys'):
        u_dict = {k: u[k] for k in u.keys()}
    else:
        u_dict = dict(u)
    
    # Calcula dias restantes
    d = days_until(u_dict['expires_at'])
    days_left = max(0, d)
    
    # Busca conexões ativas
    connections = _get_user_connections(u_dict['username'])
    
    try:
        from datetime import datetime as _dt
        exp_dt = _dt.fromisoformat(u_dict['expires_at'])
        expiration_date = exp_dt.strftime('%d/%m/%Y')
    except Exception:
        expiration_date = u_dict['expires_at'][:10] if u_dict['expires_at'] else ''

    resp = {
        "id": "01",
        "username": u_dict['username'],
        "count_connections": connections,
        "limit_connections": u_dict['connection_limit'],
        "expiration_date": expiration_date,
        "expiration_days": str(days_left),
    }
    return jsonify(resp)

    # Calcula dias restantes
    d = days_until(u['expires_at'])
    days_left = max(0, d)
    
    # Define status baseado na expiração e conexões
    if days_left <= 0:
        status = "expired"
        online_status = "expirado"
    else:
        status = "active"
        online_status = "online"
    
    # Busca conexões ativas (você precisa implementar esta função)
    connections = _get_user_connections(u['username'])
    
    # Verifica se excedeu o limite
    limit = u['connection_limit']
    if limit > 0 and connections >= limit:
        online_status = "limite_excedido"
        status = "limit_exceeded"
    
    try:
        from datetime import datetime as _dt
        exp_dt = _dt.fromisoformat(u['expires_at'])
        expiration_date = exp_dt.strftime('%d/%m/%Y')
        expires_at_formatted = exp_dt.strftime('%Y-%m-%d')
    except Exception:
        expiration_date = u['expires_at'][:10] if u['expires_at'] else ''
        expires_at_formatted = u['expires_at'][:10] if u['expires_at'] else ''

    # Resposta COMPLETA para compatibilidade
    resp = {
        # Formato original (para apps VPN)
        "id": "01",
        "username": u['username'],
        "count_connections": connections,
        "limit_connections": limit,
        "expiration_date": expiration_date,
        "expiration_days": str(days_left),
        
        # Formato moderno (para o frontend do painel)
        "exists": True,
        "status": online_status,
        "online": connections > 0,
        "expired": days_left <= 0,
        "blocked": False,
        "server": u['server_name'] if 'server_name' in u else 'N/A',
        "connections": connections,
        "limit": limit,
        "days_left": days_left,
        "expires_at": expires_at_formatted,
        "created_at": u.get('created_at', '')[:10] if u.get('created_at') else '',
        "last_login": u.get('last_login', ''),
        "v2ray_uuid": u.get('v2ray_uuid', ''),
    }
    return jsonify(resp)


@app.route('/checkuser/dtunnel.php', methods=['GET', 'POST'])
def checkuser_dtunnel():
    username = (request.args.get('user') or request.form.get('user') or '').strip()
    if not username:
        return jsonify({"error": "missing user parameter"})
    return _checkuser_response(username)


@app.route('/checkuser/', methods=['GET', 'POST'])
@app.route('/checkuser', methods=['GET', 'POST'])
def checkuser_index():
    """DTunnel/HTTP style: /checkuser?user=USERNAME"""
    username = (request.args.get('user') or request.args.get('username')
                or request.form.get('user') or request.form.get('username') or '').strip()
    if not username:
        return jsonify({
            "id": "01", "username": "", "count_connections": 0,
            "limit_connections": 0, "expiration_date": "", "expiration_days": "0",
            "exists": False, "connections": 0, "limit": 0
        })
    return _checkuser_response(username)


@app.route('/api/checkuser', methods=['GET', 'POST'])
def checkuser_api():
    username = (request.args.get('user') or request.args.get('username')
                or request.form.get('user') or request.form.get('username') or '').strip()
    if not username:
        return jsonify({"error": "missing user parameter"})
    return _checkuser_response(username)


@app.route('/checkuser/<path:username>', methods=['GET', 'POST'])
def checkuser(username):
    return _checkuser_response(username)


@app.route('/check/<path:username>', methods=['GET', 'POST'])
def checkuser_check_path(username):
    return _checkuser_response(username)



@app.route('/users/credentials/<int:user_id>')
@login_required
def user_credentials(user_id):
    current_user = get_current_user()
    u = db.get_ssh_user(user_id)
    if not u:
        return jsonify(success=False, message='Usuário não encontrado')
    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return jsonify(success=False, message='Sem permissão')
    return jsonify(
        success=True,
        username=u['username'],
        password=u['password'],
        v2ray_uuid=u['v2ray_uuid'],
        expires_at=u['expires_at'],
        connection_limit=u['connection_limit'],
    )

# ---------------------------------------------------------------------------
# Random generation API
# ---------------------------------------------------------------------------

@app.route('/api/random_credentials')
@login_required
def random_credentials():
    return jsonify(
        username=db.random_username('user'),
        password=db.random_password(5),   # 5 digits — easy to type, e.g. 12345
        uuid=db.random_uuid()
    )


# ---------------------------------------------------------------------------
# Admin: change panel user password
# ---------------------------------------------------------------------------

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    current_user = get_current_user()
    if request.method == 'POST':
        new_pw = request.form.get('new_password', '').strip()
        if len(new_pw) >= 4:
            db.update_panel_user(current_user['id'], password_hash=db.hash_password(new_pw))
            flash('Senha alterada com sucesso!', 'success')
        else:
            flash('Senha muito curta (mínimo 4 caracteres).', 'danger')
    return render_template('shared/profile.html')




# ---------------------------------------------------------------------------
# Dragon Core / Atlas Migration (admin only)
# ---------------------------------------------------------------------------

@app.route('/migration', methods=['GET', 'POST'])
@admin_required
def migration_page():
    result = None
    source_label = ''
    if request.method == 'POST':
        f = request.files.get('sql_file')
        if not f:
            flash('Selecione um arquivo SQL.', 'warning')
            return redirect(url_for('migration_page'))
        try:
            import gzip, io
            raw = f.read()
            if raw[:2] == b'\x1f\x8b':  # gzip magic
                sql_content = gzip.decompress(raw).decode('latin-1', errors='replace')
            else:
                sql_content = raw.decode('latin-1', errors='replace')

            from migration import migrate_dragon_core, migrate_gestor_ssh, detect_source
            source = detect_source(sql_content)

            if source == 'gestor':
                result = migrate_gestor_ssh(sql_content)
                source_label = 'GestorSSH'
                servers_msg = f", {result.get('servers', 0)} servidor(es)" if result.get('servers') else ''
                flash(
                    f"Migração GestorSSH concluída: {result['resellers']} revendas, "
                    f"{result['ssh_users']} usuários SSH{servers_msg} importados.",
                    'success'
                )
            else:
                result = migrate_dragon_core(sql_content)
                source_label = 'Dragon Core / Atlas'
                flash(
                    f"Migração Dragon Core concluída: {result['resellers']} revendas, "
                    f"{result['ssh_users']} usuários SSH importados.",
                    'success'
                )

            if result.get('errors'):
                for e in result['errors'][:10]:
                    flash(f'Aviso: {e}', 'warning')

        except Exception as e:
            flash(f'Erro na migração: {e}', 'danger')
            import traceback; traceback.print_exc()
        return redirect(url_for('migration_page'))
    return render_template('admin/migration.html')

# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

def job_backup():
    cfg = db.get_backup_config()
    if cfg and cfg['enabled'] and cfg['telegram_bot_token'] and cfg['telegram_chat_id']:
        ok, msg = bk.send_backup_telegram(cfg['telegram_bot_token'], cfg['telegram_chat_id'])
        if ok:
            db.update_last_backup(now_br().strftime('%Y-%m-%d %H:%M:%S'))


def job_cleanup_tests():
    """Delete expired test users from panel and ALL their servers."""
    expired = db.get_expired_test_users()
    for u in expired:
        sc.broadcast_delete(u, db)
        db.delete_ssh_user(u['id'])


def job_expire_regular_users():
    """Block expired regular (non-test) users on SSH servers.
    Sends suspend command so they stop working until renewed.
    Users are NOT deleted — they stay in the panel so they can be renewed.
    """
    conn = db.get_db()
    expired = conn.execute(
        "SELECT * FROM ssh_users WHERE is_test=0 AND status='active' AND expires_at <= ?",
        (now_br().strftime('%Y-%m-%d %H:%M:%S'),)
    ).fetchall()
    conn.close()
    for u in expired:
        # Lock on ALL servers (primary + extras)
        lock_cmd = (
            f"passwd -l {u['username']} 2>/dev/null; "
            f"/root/pmaster_agent blockuser {u['username']} 2>/dev/null || true"
        )
        sc.broadcast_command(u, lock_cmd, db)
        db.update_ssh_user(u['id'], status='suspended')


scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(job_backup, 'interval', hours=6, id='backup_job')
scheduler.add_job(job_cleanup_tests, 'interval', minutes=5, id='cleanup_tests')
scheduler.add_job(job_expire_regular_users, 'interval', minutes=10, id='expire_users')
scheduler.start()

# ---------------------------------------------------------------------------
# Telegram Bot — Long Polling (no webhook / no HTTPS required)
# ---------------------------------------------------------------------------
import threading as _threading

_tg_state: dict = {}
_tg_poll_thread: _threading.Thread = None
_tg_stop_event = _threading.Event()


def _tg_api(bot_token: str, method: str, **kwargs):
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot_token}/{method}",
                          json=kwargs, timeout=30)
        return r.json()
    except Exception:
        return {}


def _tg_send(bot_token, chat_id, text, reply_markup=None, parse_mode='HTML'):
    kw = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}
    if reply_markup:
        kw['reply_markup'] = reply_markup
    return _tg_api(bot_token, 'sendMessage', **kw)


def _tg_edit(bot_token, chat_id, message_id, text, reply_markup=None, parse_mode='HTML'):
    kw = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': parse_mode}
    if reply_markup:
        kw['reply_markup'] = reply_markup
    return _tg_api(bot_token, 'editMessageText', **kw)


def _tg_answer(bot_token, cq_id, text=''):
    _tg_api(bot_token, 'answerCallbackQuery', callback_query_id=cq_id, text=text)


def _main_menu_kb():
    return {'inline_keyboard': [
        [{'text': '👥 Ver Revendas',         'callback_data': 'mn_resellers'}],
        [{'text': '🖥️ Servidores',           'callback_data': 'mn_servers'}],
        [{'text': '🔑 Trocar Senha Admin',   'callback_data': 'mn_changepw'}],
        [{'text': '💾 Backup Agora',         'callback_data': 'mn_backup'}],
        [{'text': '🗑️ Reset do Sistema',     'callback_data': 'mn_reset'}],
    ]}


def _back_main():
    return {'inline_keyboard': [[{'text': '🔙 Menu Principal', 'callback_data': 'mn_main'}]]}


def _process_tg_update(bot_token: str, admin_chat: str, update: dict):
    """Process a single Telegram update (message or callback_query)."""
    # ── Callback queries ────────────────────────────────────────────────
    if 'callback_query' in update:
        cq      = update['callback_query']
        chat_id = str(cq['message']['chat']['id'])
        msg_id  = cq['message']['message_id']
        cb      = cq.get('data', '')
        cq_id   = cq['id']

        if chat_id != admin_chat:
            _tg_answer(bot_token, cq_id, '⛔ Sem permissão')
            return
        _tg_answer(bot_token, cq_id)

        if cb == 'mn_main':
            _tg_edit(bot_token, chat_id, msg_id, '🏠 <b>Menu Principal</b>', _main_menu_kb())

        elif cb == 'mn_resellers':
            resellers = db.get_resellers()
            if not resellers:
                body = '👥 <b>Revendas</b>\n\nNenhuma revenda cadastrada.'
            else:
                lines = ['👥 <b>Revendas</b>\n']
                for r in resellers:
                    d    = days_until(r['expires_at']) if r['expires_at'] else 0
                    icon = '🟢' if r['status'] == 'active' else '🔴'
                    used = r['accounts_used'] or 0
                    lim  = '∞' if r['account_limit'] == -1 else str(r['account_limit'])
                    warn = '⚠️' if d <= 7 else ''
                    lines.append(f"{icon} <b>{r['username']}</b> {warn}— {used}/{lim} — {d}d")
                body = '\n'.join(lines)
            _tg_edit(bot_token, chat_id, msg_id, body, _back_main())

        elif cb == 'mn_servers':
            servers = db.get_servers()
            if not servers:
                _tg_edit(bot_token, chat_id, msg_id,
                         '🖥️ Nenhum servidor cadastrado.', _back_main())
            else:
                kb = [[{'text': f'🖥️ {s["name"]} ({s["ip"]})',
                        'callback_data': f'srv_{s["id"]}'}] for s in servers]
                kb.append([{'text': '🔙 Voltar', 'callback_data': 'mn_main'}])
                _tg_edit(bot_token, chat_id, msg_id,
                         '🖥️ <b>Selecione o servidor:</b>', {'inline_keyboard': kb})

        elif cb.startswith('srv_') and cb[4:].isdigit():
            srv_id = int(cb[4:])
            srv    = db.get_server(srv_id)
            if srv:
                kb = {'inline_keyboard': [
                    [{'text': '🔥 Limpar iptables',      'callback_data': f'sa_ipt_{srv_id}'}],
                    [{'text': '🔄 Reiniciar SSH',        'callback_data': f'sa_ssh_{srv_id}'}],
                    [{'text': '📊 CPU / RAM',            'callback_data': f'sa_sts_{srv_id}'}],
                    [{'text': '🔃 Reiniciar Servidor',   'callback_data': f'sa_rbc_{srv_id}'}],
                    [{'text': '🔙 Servidores', 'callback_data': 'mn_servers'}],
                ]}
                _tg_edit(bot_token, chat_id, msg_id,
                         f'🖥️ <b>{srv["name"]}</b> ({srv["ip"]})', kb)

        elif cb.startswith('sa_'):
            parts  = cb.split('_')
            action = parts[1]
            srv_id = int(parts[2])
            srv    = db.get_server(srv_id)
            if srv:
                ip, pt, tok = srv['ip'], srv['module_port'], srv['auth_token']
                if action == 'ipt':
                    ok, out = sc.send_command(ip, pt, tok,
                        'iptables -F && iptables -X && iptables -Z && echo DONE')
                    result = '✅ iptables limpo!' if (ok and 'DONE' in out) else f'❌ {out[:300]}'
                elif action == 'ssh':
                    ok, out = sc.send_command(ip, pt, tok,
                        'systemctl restart sshd 2>/dev/null || service ssh restart && echo DONE')
                    result = '✅ SSH reiniciado!' if ok else f'❌ {out[:300]}'
                elif action == 'sts':
                    stats = sc.get_cpu_mem(ip, pt, tok)
                    result = (f'📊 <b>{srv["name"]}</b>\n🔲 CPU: {stats["cpu"]:.1f}%\n'
                              f'💾 RAM: {stats["mem_used"]}MB/{stats["mem_total"]}MB') \
                             if not stats.get('error') else f'❌ {stats["error"]}'
                elif action == 'rbc':
                    confirm_kb = {'inline_keyboard': [
                        [{'text': '✅ Confirmar reboot', 'callback_data': f'sa_rbcok_{srv_id}'}],
                        [{'text': '❌ Cancelar',         'callback_data': f'srv_{srv_id}'}],
                    ]}
                    _tg_edit(bot_token, chat_id, msg_id,
                             f'⚠️ Reiniciar <b>{srv["name"]}</b>?', confirm_kb)
                    return
                elif action == 'rbcok':
                    sc.send_command(ip, pt, tok,
                                    'nohup sh -c "sleep 2 && reboot" &>/dev/null &')
                    result = f'🔃 Reboot enviado para <b>{srv["name"]}</b>. Aguarde ~1 min.'
                else:
                    result = '❓ Ação desconhecida'
                back_kb = {'inline_keyboard': [
                    [{'text': '🔙 Servidor', 'callback_data': f'srv_{srv_id}'}],
                    [{'text': '🏠 Menu',     'callback_data': 'mn_main'}],
                ]}
                _tg_edit(bot_token, chat_id, msg_id, result, back_kb)

        elif cb == 'mn_changepw':
            _tg_state[chat_id] = {'state': 'await_pw'}
            _tg_edit(bot_token, chat_id, msg_id,
                     '🔑 <b>Trocar Senha Admin</b>\n\nDigite a nova senha (≥6 chars):',
                     {'inline_keyboard': [[{'text': '❌ Cancelar', 'callback_data': 'cancel_pw'}]]})

        elif cb == 'cancel_pw':
            _tg_state.pop(chat_id, None)
            _tg_edit(bot_token, chat_id, msg_id, '❌ Cancelado.')
            _tg_send(bot_token, chat_id, '🏠 Menu:', _main_menu_kb())

        elif cb == 'mn_backup':
            _tg_edit(bot_token, chat_id, msg_id, '⏳ Gerando backup…')
            ok, msg_txt = bk.send_backup_telegram(bot_token, admin_chat)
            _tg_send(bot_token, chat_id,
                     f'{"✅" if ok else "❌"} {msg_txt}', _back_main())

        elif cb == 'mn_reset':
            _tg_state[chat_id] = {'state': 'await_reset_pw'}
            _tg_edit(bot_token, chat_id, msg_id,
                     '🗑️ <b>Reset do Sistema</b>\n\n⚠️ Apaga todos os usuários SSH, revendas e pagamentos.\n\nDigite sua senha de admin para confirmar:',
                     {'inline_keyboard': [[{'text': '❌ Cancelar', 'callback_data': 'cancel_reset'}]]})

        elif cb == 'cancel_reset':
            _tg_state.pop(chat_id, None)
            _tg_edit(bot_token, chat_id, msg_id, '❌ Reset cancelado.')
            _tg_send(bot_token, chat_id, '🏠 Menu:', _main_menu_kb())

    # ── Messages ────────────────────────────────────────────────────────
    elif 'message' in update:
        msg     = update['message']
        chat_id = str(msg['chat']['id'])
        text    = msg.get('text', '').strip()

        if chat_id != admin_chat:
            _tg_send(bot_token, chat_id, '⛔ Acesso não autorizado.')
            return

        st = _tg_state.get(chat_id, {})

        if st.get('state') == 'await_pw':
            if len(text) < 6:
                _tg_send(bot_token, chat_id, '❌ Mínimo 6 caracteres. Tente novamente:')
                return
            conn = db.get_db()
            row  = conn.execute(
                "SELECT id FROM panel_users WHERE role='admin' LIMIT 1").fetchone()
            conn.close()
            if row:
                db.update_panel_user(row['id'],
                                     password_plain=text, password_hash=db.hash_password(text))
            _tg_state.pop(chat_id, None)
            _tg_send(bot_token, chat_id, '✅ Senha alterada!')
            _tg_send(bot_token, chat_id, '🏠 Menu:', _main_menu_kb())
            return

        if st.get('state') == 'await_reset_pw':
            conn     = db.get_db()
            admin_r  = conn.execute(
                "SELECT * FROM panel_users WHERE role='admin' LIMIT 1").fetchone()
            conn.close()
            ok_pw = admin_r and (text == admin_r['password_plain'] or
                                  db.check_password(text, admin_r['password_hash']))
            if not ok_pw:
                _tg_state.pop(chat_id, None)
                _tg_send(bot_token, chat_id, '❌ Senha incorreta. Reset cancelado.')
                _tg_send(bot_token, chat_id, '🏠 Menu:', _main_menu_kb())
                return
            try:
                conn = db.get_db()
                conn.execute("DELETE FROM ssh_users")
                conn.execute("DELETE FROM payments")
                conn.execute("DELETE FROM panel_users WHERE role != 'admin'")
                conn.execute("DELETE FROM reseller_servers")
                conn.commit()
                conn.close()
                _tg_send(bot_token, chat_id, '✅ Sistema zerado com sucesso!')
            except Exception as e:
                _tg_send(bot_token, chat_id, f'❌ Erro: {e}')
            _tg_state.pop(chat_id, None)
            _tg_send(bot_token, chat_id, '🏠 Menu:', _main_menu_kb())
            return

        if text.startswith('/start'):
            total_users = len(db.get_ssh_users(all_users=True))
            total_res   = len(db.get_resellers())
            servers     = db.get_servers()
            _tg_send(bot_token, chat_id,
                     f'👋 <b>Painel Master — Bot Admin</b>\n\n'
                     f'👤 Usuários: <b>{total_users}</b>\n'
                     f'👥 Revendas: <b>{total_res}</b>\n'
                     f'🖥️ Servidores: <b>{len(servers)}</b>\n\n'
                     f'Escolha uma opção:',
                     _main_menu_kb())
        else:
            _tg_send(bot_token, chat_id, '❓ Use /start para abrir o menu.')


def _tg_polling_loop():
    """Background long-polling loop. Runs in a daemon thread — no webhook needed."""
    offset = 0
    while not _tg_stop_event.is_set():
        try:
            cfg = db.get_backup_config()
            if not cfg or not cfg['telegram_bot_token'] or not cfg['telegram_chat_id']:
                _tg_stop_event.wait(30)
                continue

            bot_token  = cfg['telegram_bot_token']
            admin_chat = str(cfg['telegram_chat_id'])

            resp = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={'offset': offset, 'timeout': 25,
                        'allowed_updates': ['message', 'callback_query']},
                timeout=30
            )
            if not resp.ok:
                _tg_stop_event.wait(10)
                continue

            data = resp.json()
            if not data.get('ok'):
                _tg_stop_event.wait(10)
                continue

            for upd in data.get('result', []):
                offset = upd['update_id'] + 1
                try:
                    _process_tg_update(bot_token, admin_chat, upd)
                except Exception:
                    pass

        except Exception:
            _tg_stop_event.wait(15)


def start_tg_polling():
    """Start the Telegram polling thread (called once at app startup)."""
    global _tg_poll_thread
    if _tg_poll_thread and _tg_poll_thread.is_alive():
        return
    _tg_stop_event.clear()
    _tg_poll_thread = _threading.Thread(target=_tg_polling_loop,
                                        name='tg_polling', daemon=True)
    _tg_poll_thread.start()


# Keep the webhook route for backward compat (now just a no-op)
@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    return '', 200


# Remove set_webhook route since polling doesn't need it — keep for UI compat
@app.route('/telegram/set_webhook', methods=['POST'])
@admin_required
def telegram_set_webhook():
    start_tg_polling()
    return jsonify(success=True,
                   message='Bot iniciado via polling! Não precisa de HTTPS nem webhook.\n'
                           'Envie /start para o bot no Telegram.')


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

# Start Telegram bot polling thread AFTER all functions are defined
start_tg_polling()

if __name__ == '__main__':
    # Use `or` so that an empty-string PORT env-var also falls back to the default
    port = int(os.environ.get('PORT') or 2083)
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
