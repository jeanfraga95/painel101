#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Painel Master — SSH User Management Panel
"""

import json
import os
import secrets
import traceback
from datetime import datetime, timedelta
from functools import wraps

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, stream_with_context,
                   url_for)
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
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB for restore

TZ = pytz.timezone('America/Sao_Paulo')

db.init_db()
db.migrate_schema()

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
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash('Acesso negado.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


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
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        user = db.get_panel_user_by_username(username)
        if user and db.check_password(password, user['password_hash']):
            if user['status'] == 'suspended':
                flash('Conta suspensa. Contate o administrador.', 'danger')
                return redirect(url_for('login'))
            session['user_id'] = user['id']
            session['role'] = user['role']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        flash('Usuário ou senha inválidos.', 'danger')

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
        return render_template('reseller/dashboard.html', stats=stats, expiring=expiring)


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
    filter_type = request.args.get('filter', 'all')  # all, expiring, test

    users = _get_visible_users(user, sort=sort, search=search)

    if filter_type == 'expiring':
        users = [u for u in users if 0 <= days_until(u['expires_at']) <= 7]
    elif filter_type == 'test':
        users = [u for u in users if u['is_test']]

    servers = db.get_servers()
    server_map = {s['id']: s for s in servers}

    # Map owner names
    all_panel_users_map = {}
    for pu in db.get_db().execute("SELECT id, username FROM panel_users").fetchall():
        all_panel_users_map[pu['id']] = pu['username']

    return render_template('shared/users.html',
                           users=users, servers=servers, server_map=server_map,
                           owner_map=all_panel_users_map,
                           sort=sort, search=search, filter_type=filter_type)


@app.route('/users/create', methods=['GET', 'POST'])
@login_required
def create_user():
    current_user = get_current_user()
    servers = db.get_servers_for_user(current_user)

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '').strip()
        days = int(request.form.get('days', 30))
        limit = int(request.form.get('limit', 1))
        server_id = request.form.get('server_id', type=int)
        use_v2ray = request.form.get('use_v2ray') == '1'
        uuid = request.form.get('uuid', '').strip() or (db.random_uuid() if use_v2ray else None)

        # Check panel user limits
        if current_user['role'] != 'admin':
            avail = current_user['account_limit'] - current_user['accounts_used']
            if avail <= 0:
                return jsonify(success=False, message='Limite de contas atingido')

        if not username:
            return jsonify(success=False, message='Username inválido')

        expires_at = (now_br() + timedelta(days=days)).strftime('%Y-%m-%d')

        # Check if user already exists on the server
        if server_id:
            srv = db.get_server(server_id)
            if srv:
                chk_ok, chk_out = sc.send_command(
                    srv['ip'], srv['module_port'], srv['auth_token'],
                    f"id {username} 2>/dev/null && echo EXISTS || echo NOTFOUND"
                )
                if chk_ok and 'EXISTS' in chk_out:
                    return jsonify(success=False, message=f'Usuário "{username}" já existe no servidor SSH. Escolha outro nome.')

        ok, msg, new_id = db.create_ssh_user(
            username, password, current_user['id'], server_id,
            expires_at, limit, uuid if use_v2ray else None, 0
        )
        if not ok:
            return jsonify(success=False, message=msg)

        # Create on server
        server_msg = ''
        if server_id:
            srv = db.get_server(server_id)
            if srv:
                s_ok, s_msg = sc.create_ssh_user_on_server(
                    srv['ip'], srv['module_port'], srv['auth_token'],
                    username, password, days, limit, uuid if use_v2ray else None
                )
                server_msg = s_msg

        app_link = db.get_setting('app_link', '')
        return jsonify(
            success=True,
            message='Usuário criado com sucesso',
            user={
                'username': username,
                'password': password,
                'expires_at': expires_at,
                'uuid': uuid if use_v2ray else None,
                'app_link': app_link,
                'server_msg': server_msg,
            }
        )

    return render_template('shared/create_user.html', servers=servers, is_test=False)


@app.route('/users/test', methods=['POST'])
@login_required
def create_test():
    current_user = get_current_user()
    servers = db.get_servers_for_user(current_user)

    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '').strip()
    hours = int(request.form.get('hours', 2))
    limit = int(request.form.get('limit', 1))
    server_id = request.form.get('server_id', type=int)
    use_v2ray = request.form.get('use_v2ray') == '1'
    uuid = db.random_uuid() if use_v2ray else None

    if current_user['role'] != 'admin':
        avail = current_user['account_limit'] - current_user['accounts_used']
        if avail <= 0:
            return jsonify(success=False, message='Limite de contas atingido')

    if not username:
        return jsonify(success=False, message='Username inválido')

    expires_at = (now_br() + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')

    ok, msg, new_id = db.create_ssh_user(
        username, password, current_user['id'], server_id,
        expires_at, limit, uuid if use_v2ray else None, 1
    )
    if not ok:
        return jsonify(success=False, message=msg)

    # Create on server (minutes)
    server_msg = ''
    if server_id:
        srv = db.get_server(server_id)
        if srv:
            s_ok, s_msg = sc.create_test_user_on_server(
                srv['ip'], srv['module_port'], srv['auth_token'],
                username, password, hours * 60, limit, uuid if use_v2ray else None
            )
            server_msg = s_msg

    app_link = db.get_setting('app_link', '')
    return jsonify(
        success=True,
        message='Teste criado',
        user={
            'username': username,
            'password': password,
            'expires_at': expires_at,
            'uuid': uuid if use_v2ray else None,
            'app_link': app_link,
            'hours': hours,
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

    # Delete on server
    server_msg = ''
    if u['server_id']:
        srv = db.get_server(u['server_id'])
        if srv:
            s_ok, server_msg = sc.delete_user_on_server(
                srv['ip'], srv['module_port'], srv['auth_token'],
                u['username'], u['v2ray_uuid']
            )

    db.delete_ssh_user(user_id)
    return jsonify(success=True, message='Usuário deletado', server_msg=server_msg)


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
    db.renew_ssh_user(user_id, days)

    # Renew on server
    server_msg = ''
    if u['server_id']:
        srv = db.get_server(u['server_id'])
        if srv:
            s_ok, server_msg = sc.renew_user_on_server(
                srv['ip'], srv['module_port'], srv['auth_token'],
                u['username'], days
            )

    return jsonify(success=True, message=f'Renovado por {days} dias', server_msg=server_msg)


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

    allowed_fields = {'connection_limit', 'status', 'expires_at'}
    updates = {k: v for k, v in request.form.items() if k in allowed_fields}
    if 'connection_limit' in updates:
        try:
            updates['connection_limit'] = int(updates['connection_limit'])
        except ValueError:
            return jsonify(success=False, message='Limite inválido')
    if updates:
        db.update_ssh_user(user_id, **updates)
    return jsonify(success=True, message='Atualizado')


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

    # Kill sessions if suspending
    if new_status == 'suspended' and u['server_id']:
        srv = db.get_server(u['server_id'])
        if srv:
            sc.send_command(srv['ip'], srv['module_port'], srv['auth_token'],
                            f"pkill -u {u['username']}")

    return jsonify(success=True, message=f'Status: {new_status}', new_status=new_status)


# ---------------------------------------------------------------------------
# Resellers
# ---------------------------------------------------------------------------

@app.route('/resellers')
@login_required
def resellers_list():
    current_user = get_current_user()
    all_servers = db.get_servers()
    if current_user['role'] == 'admin':
        resellers = db.get_resellers()
        parent_map = {}
        for r in resellers:
            if r['parent_id']:
                p = db.get_panel_user(r['parent_id'])
                parent_map[r['id']] = p['username'] if p else '-'
    else:
        resellers = db.get_all_resellers_under(current_user['id'])
        parent_map = {}
        for r in resellers:
            if r['parent_id']:
                p = db.get_panel_user(r['parent_id'])
                parent_map[r['id']] = p['username'] if p else '-'

    # Build server assignments map
    server_assign_map = {}
    for r in resellers:
        server_assign_map[r['id']] = db.get_reseller_servers(r['id'])

    sort = request.args.get('sort', 'username')
    return render_template('shared/resellers.html',
                           resellers=resellers, parent_map=parent_map,
                           all_servers=all_servers,
                           server_assign_map=server_assign_map)


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

    # For non-admin, the account_limit comes from their own available limit
    if current_user['role'] != 'admin':
        avail = current_user['account_limit'] - current_user['accounts_used']
        if account_limit > avail:
            return jsonify(success=False, message=f'Limite insuficiente. Disponível: {avail}')

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

    db.delete_panel_user(reseller_id)
    return jsonify(success=True, message='Revenda deletada')


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
    db.update_panel_user(reseller_id, status=new_status)
    return jsonify(success=True, new_status=new_status)


@app.route('/resellers/renew/<int:reseller_id>', methods=['POST'])
@login_required
def renew_reseller(reseller_id):
    current_user = get_current_user()
    if current_user['role'] != 'admin':
        return jsonify(success=False, message='Apenas admin pode renovar revendas')
    days = int(request.form.get('days', 30))
    r = db.get_panel_user(reseller_id)
    if not r:
        return jsonify(success=False, message='Revenda não encontrada')
    try:
        exp = datetime.fromisoformat(r['expires_at'])
        if exp < now_br():
            exp = now_br()
    except Exception:
        exp = now_br()
    new_exp = (exp + timedelta(days=days)).strftime('%Y-%m-%d')
    db.update_panel_user(reseller_id, expires_at=new_exp)
    return jsonify(success=True, message=f'Renovado por {days} dias')


# ---------------------------------------------------------------------------
# Servers (admin only)
# ---------------------------------------------------------------------------

@app.route('/servers')
@admin_required
def servers_list():
    servers = db.get_servers()
    return render_template('admin/servers.html', servers=servers)


@app.route('/servers/add', methods=['POST'])
@admin_required
def add_server():
    name = request.form.get('name', '').strip()
    ip = request.form.get('ip', '').strip()
    module_port = int(request.form.get('module_port', 7277))
    root_user = request.form.get('root_user', 'root').strip()
    root_password = request.form.get('root_password', '').strip()
    auth_token = request.form.get('auth_token', db.random_password(22)).strip()

    if not name or not ip:
        return jsonify(success=False, message='Nome e IP são obrigatórios')

    new_id = db.add_server(name, ip, module_port, root_user, root_password, auth_token)
    return jsonify(success=True, message='Servidor adicionado', server_id=new_id)


@app.route('/servers/delete/<int:server_id>', methods=['POST'])
@admin_required
def delete_server(server_id):
    db.delete_server(server_id)
    return jsonify(success=True, message='Servidor removido')


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

    # Get all users assigned to this server
    conn = db.get_db()
    users = conn.execute("SELECT * FROM ssh_users WHERE server_id=? AND status='active'", (server_id,)).fetchall()
    conn.close()

    users_list = [dict(u) for u in users]
    ok, msg = sc.sync_users_to_server(srv['ip'], srv['module_port'], srv['auth_token'], users_list)
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
    servers = db.get_servers()
    return render_template('shared/online.html', servers=servers)


@app.route('/api/online/<int:server_id>')
@login_required
def api_online(server_id):
    current_user = get_current_user()
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(online=[])

    online = sc.get_online_users_ps(srv['ip'], srv['module_port'], srv['auth_token'])

    # Filter by permission
    if current_user['role'] != 'admin':
        # Only show users that belong to current user's tree
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        conn = db.get_db()
        my_usernames = [
            r['username'] for r in conn.execute(
                f"SELECT username FROM ssh_users WHERE owner_id IN ({','.join('?'*len(tree_ids))})",
                tree_ids
            ).fetchall()
        ]
        conn.close()
        online = [u for u in online if u in my_usernames]

    # Get connection counts and limit info
    result = []
    for uname in online:
        u = db.get_ssh_user_by_username(uname)
        count = sc.get_user_connections(srv['ip'], srv['module_port'], srv['auth_token'], uname)
        result.append({
            'username': uname,
            'connections': count,
            'limit': u['connection_limit'] if u else '?',
        })

    return jsonify(online=result)


# ---------------------------------------------------------------------------
# Settings (admin)
# ---------------------------------------------------------------------------

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
            chat_id = request.form.get('chat_id', '').strip()
            enabled = 1 if request.form.get('enabled') else 0
            db.set_backup_config(bot_token, chat_id, enabled)
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
                        # Auto-renew user 30 days
                        db.renew_ssh_user(payment['ssh_user_id'], 30)
                        u = db.get_ssh_user(payment['ssh_user_id'])
                        if u and u['server_id']:
                            srv = db.get_server(u['server_id'])
                            if srv:
                                sc.renew_user_on_server(srv['ip'], srv['module_port'],
                                                        srv['auth_token'], u['username'], 30)
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
    qr_code = None
    mp_init_point = None

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        u = db.get_ssh_user_by_username(username)

        if u and u['password'] == password:
            user_data = u
            days_left = days_until(u['expires_at'])

            # Generate MercadoPago payment link if owner has it configured
            owner = db.get_panel_user(u['owner_id'])
            if owner and owner['mercadopago_token'] and owner['mercadopago_price'] > 0:
                try:
                    payload = {
                        "items": [{
                            "title": f"Renovação {username}",
                            "quantity": 1,
                            "unit_price": float(owner['mercadopago_price']),
                            "currency_id": "BRL"
                        }],
                        "notification_url": request.host_url.rstrip('/') + url_for('mp_webhook'),
                        "external_reference": f"{u['id']}_{owner['id']}",
                    }
                    resp = requests.post(
                        "https://api.mercadopago.com/checkout/preferences",
                        headers={
                            'Authorization': f"Bearer {owner['mercadopago_token']}",
                            'Content-Type': 'application/json'
                        },
                        json=payload,
                        timeout=10
                    )
                    if resp.status_code in (200, 201):
                        pref = resp.json()
                        mp_init_point = pref.get('init_point')
                        pref_id = pref.get('id')
                        # Save payment record
                        db.add_payment(u['id'], owner['id'], owner['mercadopago_price'],
                                       pref_id, '', 'pending')
                        # Generate QR
                        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?data={mp_init_point}&size=200x200"
                        qr_code = qr_url
                except Exception:
                    pass
        else:
            error = 'Usuário ou senha inválidos'

    return render_template('user_login.html', user_data=user_data, error=error,
                           qr_code=qr_code, mp_init_point=mp_init_point)


# ---------------------------------------------------------------------------
# CheckUser API
# ---------------------------------------------------------------------------

def _checkuser_response(username: str) -> dict:
    """Build checkuser JSON response for a given username."""
    u = db.get_ssh_user_by_username(username)
    if not u:
        return {
            "username": username,
            "count_connections": 0,
            "expiry_date": "",
            "expiry_days": 0,
            "expiry_time": "",
            "limit_connections": 0,
            "status": "Offline"
        }
    d = days_until(u['expires_at'])
    try:
        from datetime import datetime
        exp_dt = datetime.fromisoformat(u['expires_at'])
        expiry_time = exp_dt.strftime('%d/%m/%Y')
    except Exception:
        expiry_time = u['expires_at'][:10] if u['expires_at'] else ''

    return {
        "username": username,
        "count_connections": 0,
        "expiry_date": u['expires_at'][:10] if u['expires_at'] else '',
        "expiry_days": d,
        "expiry_time": expiry_time,
        "limit_connections": u['connection_limit'],
        "status": "Offline"
    }


@app.route('/checkuser/<username>')
def checkuser(username):
    return jsonify(_checkuser_response(username))


@app.route('/checkuser/')
@app.route('/checkuser')
def checkuser_index():
    """DTunnel-style: /checkuser?user=USERNAME or /checkuser/"""
    username = request.args.get('user', '').strip()
    if not username:
        return jsonify({"error": "missing user parameter"})
    return jsonify(_checkuser_response(username))


@app.route('/checkuser/dtunnel.php')
def checkuser_dtunnel():
    """DTunnel app format: /checkuser/dtunnel.php?user=USERNAME"""
    username = request.args.get('user', '').strip()
    if not username:
        return jsonify({"error": "missing user parameter"})
    resp = _checkuser_response(username)
    # DTunnel expects text/html or application/json
    return jsonify(resp)


@app.route('/api/checkuser')
def checkuser_api():
    """Generic: /api/checkuser?user=USERNAME"""
    username = request.args.get('user', request.args.get('username', '')).strip()
    if not username:
        return jsonify({"error": "missing user parameter"})
    return jsonify(_checkuser_response(username))



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
        username=db.random_username('usr'),
        password=db.random_password(10),
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

            from migration import migrate_dragon_core
            result = migrate_dragon_core(sql_content)
            flash(f"Migração concluída: {result['resellers']} revendas, {result['ssh_users']} usuários SSH importados.", 'success')
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
    """Delete expired test users from panel and server."""
    expired = db.get_expired_test_users()
    for u in expired:
        if u['server_id']:
            srv = db.get_server(u['server_id'])
            if srv:
                sc.delete_user_on_server(srv['ip'], srv['module_port'], srv['auth_token'],
                                         u['username'], u['v2ray_uuid'])
        db.delete_ssh_user(u['id'])


scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(job_backup, 'interval', hours=6, id='backup_job')
scheduler.add_job(job_cleanup_tests, 'interval', minutes=5, id='cleanup_tests')
scheduler.start()

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
