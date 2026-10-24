#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Painel Master — SSH User Management Panel
"""

import json
import os
import re
import secrets
import threading
import time
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
class _SecureCookieMiddleware:
    """Marca o cookie de sessao como Secure quando a requisicao e HTTPS.

    O painel atende tanto por http://IP quanto por https:// (Cloudflare ou SSL
    local).  Sem o atributo Secure o cookie trafegaria em claro quando o
    usuario entra pelo endereco http://, permitindo o roubo da sessao.

    Precisa ser um middleware de WSGI (e nao um after_request) porque o Flask
    so grava o cookie da sessao DEPOIS dos after_request.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        proto = (environ.get('HTTP_X_FORWARDED_PROTO') or '').lower()
        https = proto == 'https' or environ.get('wsgi.url_scheme') == 'https'
        if not https:
            return self.wsgi_app(environ, start_response)

        def wrapper(status, headers, exc_info=None):
            novos = []
            for k, v in headers:
                if (k.lower() == 'set-cookie' and v.startswith('session=')
                        and 'secure' not in v.lower()):
                    v = v + '; Secure'
                novos.append((k, v))
            return start_response(status, novos, exc_info)

        return self.wsgi_app(environ, wrapper)


app.wsgi_app = ProxyFix(_SecureCookieMiddleware(app.wsgi_app),
                        x_for=1, x_proto=1, x_host=1)

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
    db.migrate_schema_tickets()
    db.migrate_schema_v4()
    db.migrate_schema_informativos()
    db.migrate_schema_message_template()
    db.migrate_schema_appearance()
    db.migrate_schema_panel_lock()
    db.migrate_schema_security()
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
    """Dias restantes até o ÚLTIMO dia de uso.

    `expires_at` é o último dia em que o usuário pode usar o serviço, então a
    comparação é feita entre datas (não entre instantes): quem vence hoje
    retorna 0 e ainda está ativo; só é considerado expirado quando
    `expires_at < hoje`.

    Isso alinha o painel com o servidor: `createssh` roda `useradd -e` com
    `dias+1`, ou seja, a conta morre no dia SEGUINTE ao vencimento.
    """
    try:
        exp = datetime.fromisoformat(str(date_str)[:19])
    except Exception:
        return 0
    return (exp.date() - now_br().date()).days


def is_expired(date_str: str) -> bool:
    """True quando o último dia de uso já passou (venceu antes de hoje)."""
    return days_until(date_str) < 0


# ---------------------------------------------------------------------------
# Vencimento do PAINEL da revenda
# ---------------------------------------------------------------------------
# Regra combinada com o cliente:
#   days_until(expires_at) >= -1  → painel ATIVO (o dia -1, "venceu ontem",
#                                   ainda é tolerado)
#   days_until(expires_at) <= -2  → painel TRAVADO: a revenda não cria
#                                   usuário/teste, não renova, não edita e
#                                   não gerencia sub-revendas; os usuários SSH
#                                   dela são suspensos no servidor.
# Ao voltar para dias positivos (renovação pelo admin), tudo é liberado e os
# usuários SSH são reativados automaticamente.

PANEL_LOCK_GRACE_DAYS = -1  # bloqueia quando days_until < este valor (<= -2)


def panel_is_locked_for(expires_at) -> bool:
    """True se, pela data de vencimento do painel, o acesso deve ser travado."""
    if not expires_at:
        return False  # sem data definida = não expira
    return days_until(expires_at) <= PANEL_LOCK_GRACE_DAYS - 1


def panel_lock_info(user) -> dict:
    """Estado de travamento do painel para `user` (admin nunca trava).

    Retorna dict com: locked (bool), days (int|None), expires (str|None),
    contact (str) — nome do painel pai a quem a revenda deve pedir renovação.
    """
    if not user or user['role'] == 'admin':
        return {'locked': False, 'days': None, 'expires': None, 'contact': ''}
    exp = user['expires_at'] if 'expires_at' in user.keys() else None
    days = days_until(exp) if exp else None
    contact = ''
    try:
        parent_id = user['parent_id'] if 'parent_id' in user.keys() else None
        if parent_id:
            parent = db.get_panel_user(parent_id)
            if parent:
                contact = parent['username']
        if not contact:
            contact = db.get_setting('panel_name', 'seu fornecedor')
    except Exception:
        contact = db.get_setting('panel_name', 'seu fornecedor')
    return {
        'locked': panel_is_locked_for(exp),
        'days': days,
        'expires': exp,
        'contact': contact,
    }


app.jinja_env.globals['is_expired'] = is_expired


app.jinja_env.globals['days_until'] = days_until
app.jinja_env.globals['now_br'] = now_br


# ---------------------------------------------------------------------------
# Estilos do painel (presets de aparência)
# ---------------------------------------------------------------------------
# Cada preset é um conjunto de variáveis CSS em static/styles/presets.css.
# As famílias são só organizacionais na UI: os presets "Tailwind" e "Sass"
# são CSS puro que emula o visual dessas ferramentas — NÃO carregamos o
# Tailwind CDN junto com o Bootstrap (o preflight do Tailwind quebraria o
# layout actual). O admin e cada revenda escolhem o próprio estilo.
STYLE_PRESETS = [
    {'id': 'default', 'name': 'Padrão', 'family': 'Bootstrap', 'desc': 'Visual atual do painel', 'dot': '#0d6efd'},
    {'id': 'bootstrap-cosmo', 'name': 'Cosmo', 'family': 'Bootstrap', 'desc': 'Verde-petróleo, cartões retos', 'dot': '#26a69a'},
    {'id': 'bootstrap-flatly', 'name': 'Flatly', 'family': 'Bootstrap', 'desc': 'Verde-água sobre azul-noite', 'dot': '#18bc9c'},
    {'id': 'tailwind-indigo', 'name': 'Indigo', 'family': 'Tailwind', 'desc': 'Índigo vivo, cantos bem arredondados', 'dot': '#4f46e5'},
    {'id': 'tailwind-emerald', 'name': 'Emerald', 'family': 'Tailwind', 'desc': 'Esmeralda, superfícies chapadas', 'dot': '#059669'},
    {'id': 'flat-slate', 'name': 'Slate', 'family': 'CSS Puro', 'desc': 'Cinza-ardósia, minimalista', 'dot': '#64748b'},
    {'id': 'glass-night', 'name': 'Glass', 'family': 'CSS Puro', 'desc': 'Vidro fosco translúcido', 'dot': '#38bdf8'},
    {'id': 'sass-neon', 'name': 'Neon', 'family': 'SCSS/Sass', 'desc': 'Preto com neon verde', 'dot': '#a3e635'},
    {'id': 'sass-pastel', 'name': 'Pastel', 'family': 'SCSS/Sass', 'desc': 'Rosa-claro suave', 'dot': '#f472b6'},
]
STYLE_PRESET_IDS = {p['id'] for p in STYLE_PRESETS}
app.jinja_env.globals['STYLE_PRESETS'] = STYLE_PRESETS


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------

def get_csrf_token() -> str:
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


# Rotas chamadas por terceiros (webhooks) ou que só leem/expõem dados não
# fazem parte do "estado autenticado" e não podem carregar token de sessão.
CSRF_EXEMPT = frozenset({
    'mp_webhook', 'telegram_webhook',
    'checkuser_dtunnel', 'checkuser_index', 'checkuser_api',
    'checkuser', 'checkuser_check_path',
})


@app.before_request
def csrf_protect():
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    if request.endpoint in CSRF_EXEMPT:
        return None
    if session.get('csrf_token') is None:
        session['csrf_token'] = secrets.token_hex(32)
    sent = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token') or ''
    if not (sent and secrets.compare_digest(sent, session['csrf_token'])):
        return jsonify(success=False, message='Requisição inválida. Recarregue a página e tente novamente.'), 403
    return None


@app.context_processor
def inject_csrf():
    return {'csrf_token': get_csrf_token}





# ---------------------------------------------------------------------------
# Bloqueio de bots de automação por User-Agent
# ---------------------------------------------------------------------------
# Só navegador real (User-Agent com "mozilla") passa. Ferramentas comuns de
# automação (curl, wget, python-requests, httpx, axios, scrapy, bots de busca
# etc.) recebem 403. Webhooks internos e a rota estática ficam isentos porque
# são chamados por outros serviços, não por humanos no navegador.

_UA_ALLOW = re.compile(r'mozilla', re.I)
_UA_BLOCK = re.compile(
    r'\b(?:curl|wget|python-?requests|urllib|httpx|aiohttp|node-?fetch|axios|'
    r'go-http-client|okhttp|libwww-perl|scrapy|apache-httpclient|httpclient|'
    r'postman|headless|phantomjs|selenium|playwright|puppeteer|crawler|spider|'
    r'slurp|yahoo|yandex|baiduspider|sogou|exabot|facebot|petalbot)\b|'
    r'(?:googlebot|bingbot|duckduckbot|ia_archiver)\b',
    re.I,
)

_UA_BLOCK_HTML = (
    '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>Acesso negado</title>'
    '<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
    'background:#0f1222;color:#e8eaf6;display:flex;align-items:center;'
    'justify-content:center;height:100vh;margin:0}.box{max-width:420px;'
    'text-align:center;padding:24px}h1{color:#ff8a80;font-size:22px}'
    'p{color:#b0b6d4;line-height:1.5}code{background:#1c2140;padding:2px 8px;'
    'border-radius:6px;font-size:13px}</style></head>'
    '<body><div class="box"><h1>Acesso negado</h1>'
    '<p>Este painel só aceita acessos de navegadores reais. '
    'Automações (%%TOOL%%) são bloqueadas.</p></div></body></html>'
)


def _ua_blocked_response():
    tool = 'User-Agent ausente'
    ua = (request.headers.get('User-Agent') or '').strip()
    if ua:
        tool = ua[:80] or 'não-reconhecido'
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return jsonify(success=False,
                       message='Acesso de bot/automação não permitido.'), 403
    return Response(_UA_BLOCK_HTML.replace('%%TOOL%%', _escape_html(tool)), 403,
                    mimetype='text/html')


def _escape_html(s: str) -> str:
    return (s.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


@app.before_request
def ua_protect():
    if request.endpoint in CSRF_EXEMPT or request.endpoint == 'static':
        return None
    ua = (request.headers.get('User-Agent') or '').strip()
    if not ua or _UA_BLOCK.search(ua) or not _UA_ALLOW.search(ua):
        return _ua_blocked_response()
    return None


# Endpoints de ESCRITA que a revenda perde quando o painel dela está travado
# por vencimento. Tudo relacionado a criar usuário/teste, renovar, editar,
# suspender/deletar usuário e criar/gerenciar sub-revendas.
PANEL_LOCK_BLOCKED_ENDPOINTS = frozenset({
    # usuários (SSH)
    'create_user', 'create_test',
    'renew_user', 'renew_users_bulk', 'update_user', 'suspend_user',
    'delete_user', 'bulk_delete_users', 'recreate_user_on_server',
    'user_add_extra_server', 'user_remove_extra_server',
    # revendas (sub-revendas)
    'create_reseller', 'set_reseller_password', 'set_reseller_servers_route',
    'set_reseller_categories_route', 'delete_reseller', 'suspend_reseller',
    'renew_reseller', 'update_reseller', 'transfer_reseller',
})

_PANEL_LOCK_MSG = (
    'Seu painel está vencido. As funções foram desativadas. '
    'Contate quem forneceu o painel a você para renovar.'
)


def _sync_panel_lock_suspension(user):
    """Suspende (ou reativa) no servidor os usuários SSH da revenda conforme o
    estado de travamento do painel dela.

    Usa a coluna panel_locked para agir só na TRANSIÇÃO (travar/destravar),
    evitando repetir o broadcast a cada request. A suspensão real é feita em
    background para não travar a navegação.
    """
    try:
        locked_now = panel_is_locked_for(user['expires_at'] if 'expires_at' in user.keys() else None)
        was_locked = db.get_panel_locked(user['id'])
    except Exception:
        return
    if locked_now == was_locked:
        return

    # pega todos os usuários SSH da revenda (dela e de toda a árvore abaixo)
    try:
        tree = db.get_all_resellers_under(user['id'])
        owner_ids = [user['id']] + [r['id'] for r in tree]
        users = db.get_ssh_users_for_tree(owner_ids)
    except Exception:
        users = []
    users = [dict(u) for u in users]

    if locked_now:
        # Marca o flag imediatamente para não reentrar em cada request. A
        # suspensão no servidor roda em background; se algum servidor falhar,
        # basta o admin renovar (que marca o flag de volta) e, ao travar de
        # novo, o broadcast é reenviado.
        db.set_panel_locked(user['id'], True)
    else:
        db.set_panel_locked(user['id'], False)

    if not users:
        return

    def _apply():
        for u in users:
            try:
                cmd = sc.build_block_cmd(u) if locked_now else sc.build_unblock_cmd(u)
                sc.broadcast_command(u, cmd, db)
                # marca no banco local para refletir na lista
                db.update_ssh_user(u['id'], status='suspended' if locked_now else 'active')
            except Exception:
                pass

    try:
        threading.Thread(target=_apply, daemon=True).start()
    except Exception:
        pass


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

        # Vencimento do painel da revenda: sincroniza suspensão dos usuários
        # dela (na transição) e bloqueia endpoints de escrita enquanto travado.
        if user['role'] != 'admin':
            try:
                _sync_panel_lock_suspension(user)
            except Exception:
                pass
            if request.endpoint in PANEL_LOCK_BLOCKED_ENDPOINTS \
                    and panel_is_locked_for(user['expires_at'] if 'expires_at' in user.keys() else None):
                if request.method == 'POST':
                    return jsonify(success=False, message=_PANEL_LOCK_MSG, panel_locked=True), 403
                flash(_PANEL_LOCK_MSG, 'danger')
                return redirect(url_for('dashboard'))
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
    app.logger.error('Erro 500:\n%s', ''.join(tb))
    if request.method == 'POST':
        return jsonify(success=False, message='Erro interno no servidor. Tente novamente.'), 500
    return ('<h3>Erro interno no servidor</h3>'
            '<p>Ocorreu um erro inesperado. Tente novamente em instantes.</p>', 500)


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
    unread = 0
    pendentes = []
    msg_template = ''
    user_appearance = None
    panel_lock = {'locked': False, 'days': None, 'expires': None, 'contact': ''}
    try:
        cu = get_current_user()
        if cu:
            if cu['role'] == 'admin':
                unread = db.count_informativos_nao_lidos(cu['id'])
            else:
                pendentes = [i for i in db.get_informativos_para_usuario(cu['id']) if not i['read_at']]
                unread = len(pendentes)
            msg_template = db.get_message_template(cu['id'])
            user_appearance = db.get_appearance(cu['id'])
            panel_lock = panel_lock_info(cu)
    except Exception:
        unread = 0
        pendentes = []
        msg_template = ''
        user_appearance = None
        panel_lock = {'locked': False, 'days': None, 'expires': None, 'contact': ''}
    return dict(
        settings=get_settings(),
        current_user=get_current_user(),
        panel_name=db.get_setting('panel_name', 'Painel Master'),
        panel_color=db.get_setting('panel_color', '#0d6efd'),
        panel_theme=db.get_setting('panel_theme', 'dark'),
        informativos_nao_lidos=unread,
        informativos_pendentes=pendentes,
        msg_template=msg_template,
        user_appearance=user_appearance,
        panel_lock=panel_lock,
    )


# ---------------------------------------------------------------------------
# Proteção contra brute-force / rate-limit nos logins
# ---------------------------------------------------------------------------
# Janela deslizante em memória. O painel roda em processo único (atrás do
# nginx), então o estado é compartilhado por todas as requisições.
# Bloqueios:
#   - (ip + usuário): após 5 falhas seguidas na janela
#   - (ip):          após 25 falhas na janela (evita credential stuffing
#                    trocando o usuário a cada tentativa)
# O tempo de bloqueio dobra a cada reincidência (15min, 30min, 60min...).

LOGIN_FAILS_MAX_KEY = 5
LOGIN_FAILS_MAX_IP = 25
LOGIN_WINDOW_SEC = 300
LOGIN_LOCK_SEC = 900
LOGIN_LOCK_MAX_SEC = 3600
LOGIN_MAX_TRACKED = 5000

# Só estes IPs podem mandar X-Forwarded-For (o nginx local). Qualquer outro
# acesso usa o IP real da conexão.
TRUSTED_PROXY_IPS = {'127.0.0.1', '::1'}

# Token opcional para o endpoint /checkuser (usado pelos apps de VPN).
# Definido em CHECKUSER_TOKEN (env) para não ficar no código.
CHECKUSER_TOKEN = os.environ.get('CHECKUSER_TOKEN', '').strip()

# Limite de consultas no /checkuser (evita enumerar usuários em massa).
CHECKUSER_MAX_PER_MIN = 60
_checkuser_hits = {}

_login_fails = {}     # chave -> [timestamps das falhas]
_login_locks = {}     # chave -> timestamp em que libera
_login_strikes = {}   # chave -> nº de bloqueios já aplicados
_login_state_lock = threading.Lock()

# Hash descartável: usado só para gastar o mesmo tempo de hash quando o
# usuário não existe (evita enumeração de usuários por diferença de tempo).
_DUMMY_HASH = db.hash_password(secrets.token_hex(16))


def _client_ip() -> str:
    """IP real do cliente.

    Só confia no X-Forwarded-For quando a conexão vem do proxy local (nginx).
    Sem isso, quem alcança a porta do app direto forja o header e escapa do
    rate-limit trocando o IP a cada tentativa.
    """
    orig = request.environ.get('werkzeug.proxy_fix.orig') or {}
    peer = orig.get('REMOTE_ADDR') or request.environ.get('REMOTE_ADDR') or ''
    if peer in TRUSTED_PROXY_IPS:
        xff = request.headers.get('X-Forwarded-For', '') or ''
        if xff:
            # o nginx sempre APPENDA o IP real no fim da lista
            ip = xff.split(',')[-1].strip()
            if ip:
                return ip
    return peer or 'unknown'


def _login_keys(username: str):
    ip = _client_ip()
    user = (username or '').strip().lower()
    return f"{ip}|{user}", ip


def _prune_login_state(now: float):
    """Descarta registros expirados para o estado não crescer sem limite."""
    for key in [k for k, t in _login_locks.items() if t <= now]:
        _login_locks.pop(key, None)
    for key in [k for k, t in _login_strikes.items() if now - t > LOGIN_WINDOW_SEC * 4]:
        _login_strikes.pop(key, None)
    if len(_login_fails) > LOGIN_MAX_TRACKED:
        for key in [k for k, v in _login_fails.items() if not v or now - v[-1] > LOGIN_WINDOW_SEC]:
            _login_fails.pop(key, None)


def _login_lock_state(username: str):
    """Retorna (bloqueado, segundos_restantes)."""
    now = time.time()
    key, ip = _login_keys(username)
    with _login_state_lock:
        _prune_login_state(now)
        for k in (key, ip):
            until = _login_locks.get(k)
            if until and until > now:
                return True, int(until - now)
    return False, 0


def _login_register_fail(username: str):
    """Registra uma tentativa inválida. Retorna (bloqueou_agora, segundos)."""
    now = time.time()
    key, ip = _login_keys(username)
    with _login_state_lock:
        _prune_login_state(now)
        for k, limit in ((key, LOGIN_FAILS_MAX_KEY), (ip, LOGIN_FAILS_MAX_IP)):
            fails = [t for t in _login_fails.get(k, []) if now - t < LOGIN_WINDOW_SEC]
            fails.append(now)
            _login_fails[k] = fails
            if len(fails) >= limit:
                strikes = _login_strikes.get(k, 0) + 1
                _login_strikes[k] = strikes
                dur = min(LOGIN_LOCK_SEC * (2 ** (strikes - 1)), LOGIN_LOCK_MAX_SEC)
                _login_locks[k] = now + dur
                _login_fails.pop(k, None)
                return True, dur
    return False, 0


def _login_register_success(username: str):
    """Zera os contadores no login bem-sucedido."""
    key, ip = _login_keys(username)
    with _login_state_lock:
        _login_fails.pop(key, None)
        _login_locks.pop(key, None)
        _login_fails.pop(ip, None)
        _login_locks.pop(ip, None)


def _login_lock_message(seconds: int) -> str:
    mins = max(1, int(seconds) // 60)
    return ('Muitas tentativas inválidas. Acesso temporariamente bloqueado '
            f'para proteger sua conta. Tente novamente em {mins} minuto(s).')


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

            blocked, remaining = _login_lock_state(username)
            if blocked:
                app.logger.warning('Login bloqueado (brute-force): ip=%s user=%s',
                                   _client_ip(), username[:60])
                flash(_login_lock_message(remaining), 'danger')
                return redirect(url_for('login'))

            user = db.get_panel_user_by_username(username)
            if user and db.check_password(password, user['password_hash']):
                if user['status'] == 'suspended':
                    flash('Conta suspensa. Contate o administrador.', 'danger')
                    return redirect(url_for('login'))
                _login_register_success(username)
                # senha em formato antigo (sha256 sem salt) -> re-hash agora
                if db.password_needs_upgrade(user['password_hash']):
                    try:
                        db.update_panel_user(user['id'],
                                             password_hash=db.hash_password(password))
                    except Exception:
                        pass
                # sessão nova: evita carregar dados de sessão anteriores
                session.clear()
                session['user_id'] = int(user['id'])
                session['role']    = str(user['role'])
                session['username']= str(user['username'])
                return redirect(url_for('dashboard'))

            if not user:
                # gasta o mesmo tempo de hash para não revelar se o usuário existe
                db.check_password(password, _DUMMY_HASH)
            locked_now, dur = _login_register_fail(username)
            if locked_now:
                app.logger.warning('Brute-force detectado: bloqueio de %ss ip=%s user=%s',
                                   dur, _client_ip(), username[:60])
                flash(_login_lock_message(dur), 'danger')
            else:
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
        expiring = [u for u in my_users if 0 <= days_until(u['expires_at']) <= 7]
        days_panel = days_until(user['expires_at']) if user['expires_at'] else 0
        stats = {
            'total_ssh': len(my_users),
            'total_resellers': len(sub_resellers),
            'account_limit': user['account_limit'],
            'accounts_used': user['accounts_used'],
            'days_panel': days_panel,
            'expiring_soon': len(expiring),
        }
        servers = db.get_servers_for_user(user)
        all_cats      = db.get_server_categories()
        avail_cat_ids = set(s['category_id'] for s in servers if s['category_id'])
        avail_cat_ids.update(db.get_reseller_categories(user['id']))
        user_categories = [c for c in all_cats if c['id'] in avail_cat_ids]
        return render_template('reseller/dashboard.html', stats=stats, expiring=expiring,
                               servers=servers, categories=user_categories)


@app.route('/api/dashboard/calendar')
@login_required
def dashboard_calendar():
    """Eventos de vencimento para o calendário do dashboard (FullCalendar)."""
    user = get_current_user()
    if user['role'] == 'admin':
        users = db.get_ssh_users(all_users=True)
    else:
        # Cada usuário vê só a árvore dele: os próprios usuários finais + os
        # dos revendas abaixo dele. Usuários finais de revendas de fora da
        # árvore nunca aparecem.
        sub_ids = [s['id'] for s in db.get_all_resellers_under(user['id'])]
        users = db.get_ssh_users_for_tree([user['id']] + sub_ids)
    today = now_br().date()
    events = []
    for u in users:
        try:
            exp = datetime.fromisoformat(str(u['expires_at'])[:19])
        except Exception:
            continue
        d = days_until(u['expires_at'])
        if d > 120:
            continue
        if d < 0:
            color = '#dc3545'
        elif d == 0:
            color = '#fd7e14'
        elif d <= 3:
            color = '#ffc107'
        else:
            color = '#198754'
        events.append({
            'id': u['id'],
            'title': u['username'],
            'start': exp.strftime('%Y-%m-%d'),
            'color': color,
            'textColor': '#fff',
            'extendedProps': {'days': d, 'id': u['id']},
        })
    return jsonify(events)


# ---------------------------------------------------------------------------
# Anotações / lembretes (bloco de notas privado de cada usuário)
# ---------------------------------------------------------------------------

NOTA_CORES = ('amarelo', 'rosa', 'azul', 'verde', 'cinza')


@app.route('/notes')
@login_required
def notes():
    user = get_current_user()
    notas = db.get_notes(user['id'])
    return render_template('notes.html', notas=notas, cores=NOTA_CORES)


@app.route('/notes/create', methods=['POST'])
@login_required
def note_create():
    user = get_current_user()
    titulo = (request.form.get('titulo') or '').strip()[:120]
    conteudo = (request.form.get('conteudo') or '').strip()
    cor = request.form.get('cor') or 'amarelo'
    if cor not in NOTA_CORES:
        cor = 'amarelo'
    if not titulo and not conteudo:
        return jsonify(success=False, message='Escreva algo na anotação')
    new_id = db.create_note(user['id'], titulo, conteudo, cor)
    return jsonify(success=True, message='Anotação criada', id=new_id)


@app.route('/notes/update/<int:note_id>', methods=['POST'])
@login_required
def note_update(note_id):
    user = get_current_user()
    n = db.get_note(note_id, user['id'])
    if not n:
        return jsonify(success=False, message='Anotação não encontrada')
    titulo = (request.form.get('titulo') or '').strip()[:120]
    conteudo = (request.form.get('conteudo') or '').strip()
    cor = request.form.get('cor') or n['cor']
    if cor not in NOTA_CORES:
        cor = 'amarelo'
    if not titulo and not conteudo:
        return jsonify(success=False, message='Escreva algo na anotação')
    db.update_note(note_id, user['id'], titulo, conteudo, cor)
    return jsonify(success=True, message='Anotação atualizada')


@app.route('/notes/delete/<int:note_id>', methods=['POST'])
@login_required
def note_delete(note_id):
    user = get_current_user()
    if not db.delete_note(note_id, user['id']):
        return jsonify(success=False, message='Anotação não encontrada')
    return jsonify(success=True, message='Anotação excluída')


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
        phone_digits = ''.join(ch for ch in search if ch.isdigit())
        users = [u for u in users
                 if search_l in u['username'].lower()
                 or (phone_digits and phone_digits in
                     ''.join(ch for ch in (u['telefone'] or '') if ch.isdigit()))]
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

    counts = {
        'all':       len(users),
        'expiring':  sum(1 for u in users if 0 <= days_until(u['expires_at']) <= 7),
        'expiring3': sum(1 for u in users if 0 <= days_until(u['expires_at']) <= 3),
        'expired':   sum(1 for u in users if days_until(u['expires_at']) < 0),
        'test':      sum(1 for u in users if u['is_test']),
    }

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
                           counts=counts,
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
        telefone    = request.form.get('telefone', '').strip()

        if current_user['role'] != 'admin':
            if current_user['account_limit'] != -1:
                avail = current_user['account_limit'] - current_user['accounts_used']
                if avail <= 0:
                    return jsonify(success=False, message='Limite de contas atingido')

        if not username:
            return jsonify(success=False, message='Username inválido')
        err_pw = db.ssh_password_error(password, username)
        if err_pw:
            return jsonify(success=False, message=err_pw)
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
            expires_at, limit, uuid if use_v2ray else None, 0, telefone
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
                'telefone':   telefone,
                'limit':      limit,
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
    telefone    = request.form.get('telefone', '').strip()

    if not category_id:
        return jsonify(success=False, message='Selecione uma categoria para o teste')

    if current_user['role'] != 'admin':
        if current_user['account_limit'] != -1:
            avail = current_user['account_limit'] - current_user['accounts_used']
            if avail <= 0:
                return jsonify(success=False, message='Limite de contas atingido')

    if not username:
        return jsonify(success=False, message='Username inválido')
    err_pw = db.ssh_password_error(password, username)
    if err_pw:
        return jsonify(success=False, message=err_pw)

    target_servers = db.get_servers_by_category(category_id)
    if not target_servers:
        return jsonify(success=False, message='Nenhum servidor ativo nesta categoria')

    primary_server_id = target_servers[0]['id']
    expires_at = (now_br() + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')

    ok, msg, new_id = db.create_ssh_user(
        username, password, current_user['id'], primary_server_id,
        expires_at, limit, uuid if use_v2ray else None, 1, telefone
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
            'telefone':   telefone,
            'limit':      limit,
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
    Renova o usuário SSH em TODOS os servidores da categoria do seu servidor
    primário — não só nos já vinculados em ssh_user_servers. Isso resolve o
    caso de servidores adicionados à categoria depois que o usuário já
    existia no painel.
    Vincula automaticamente qualquer servidor novo da categoria (evita ter
    que ir em 'editar usuário' e adicionar manualmente).

    Estratégia: EXCLUI e RECRIA o usuário do zero em cada servidor, com os
    dados atuais do painel (senha, dias, limite, uuid), em vez de tentar só
    atualizar a validade (chage) do usuário existente. Isso garante que a
    VPS fique sempre idêntica ao painel — mesmo que o usuário já não
    existisse mais lá, estivesse com senha diferente, ou o chage estivesse
    falhando silenciosamente. O próprio pmaster_agent já faz "removessh"
    antes de criar, então não precisamos checar existência antes.

    Retorna lista de (server_name, ok, msg).
    """
    results = []
    if not u['server_id']:
        # Servidor primário ausente (usuário criado antes do campo server_id
        # existir, ou o servidor primário foi removido → ON DELETE SET NULL).
        # Renova pelos vínculos em ssh_user_servers, onde o usuário existe.
        return sc.broadcast_renew(u, _days_left(u), db)

    primary_srv = db.get_server(u['server_id'])
    if not primary_srv or not primary_srv['category_id']:
        # Sem categoria: mantém comportamento antigo (só primário + extras)
        return sc.broadcast_renew(u, _days_left(u), db)

    cat_servers = db.get_servers_by_category(primary_srv['category_id'])
    if not cat_servers:
        return sc.broadcast_renew(u, _days_left(u), db)

    known_ids = set(db.get_user_all_server_ids(u['id']))
    exp_days = _days_left(u)

    def _create(srv):
        # Exclui e recria com os dados atuais do painel — garante que a VPS
        # fique idêntica ao painel independentemente do estado anterior.
        ok, msg = sc.create_ssh_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            u['username'], db.ssh_plain_password(u['password']), exp_days, u['connection_limit'],
            uuid=u['v2ray_uuid']
        )
        return {'ok': ok, 'msg': msg, 'srv': srv}

    results = []
    for srv in cat_servers:
        # Vincula o servidor ao usuário se ainda não estiver registrado
        if srv['id'] != u['server_id'] and srv['id'] not in known_ids:
            db.add_user_extra_server(u['id'], srv['id'])
        results.append(_create(srv))

    # 1 tentativa extra nos servidores que falharam (rede/transição) antes de
    # repassar o erro para a interface — evita "falso negativo" na renovação.
    for r in [r for r in results if not r['ok']]:
        retry = _create(r['srv'])
        r['ok'], r['msg'] = retry['ok'], retry['msg']

    # Resposta perdida ≠ não aplicado: timeout de conexão pode ter vindo DEPOIS
    # do módulo ter executado o create. Se a validade real na VPS já está na
    # alvo (painel + 1), considera aplicado — se desfazer, cria VPS renovada e
    # painel desatualizado.
    try:
        from datetime import date as _date, timedelta as _td
        target = _date.fromisoformat(u['expires_at'][:10]) + _td(days=1)
    except Exception:
        target = None
    if target is not None:
        for r in [r for r in results if not r['ok']]:
            real = sc.verify_user_expiry(r['srv']['ip'], r['srv']['module_port'],
                                         r['srv']['auth_token'], u['username'])
            if real == target:
                r['ok'] = True
                r['msg'] = (r['msg'] or '') + ' (aplicado na VPS; resposta perdida no timeout)'

    return [(r['srv']['name'], r['ok'], r['msg']) for r in results]


def _days_left(u) -> int:
    """Dias a enviar ao servidor para a conta morrer DEPOIS do último dia de uso.

    `createssh` já soma +1 (`useradd -e` com dias+1), então passamos exatamente
    a diferença em dias até expires_at. Clampar em 1 fazia quem vence hoje
    nascer com 1 dia a mais na VPS do que o painel dizia.
    """
    try:
        from datetime import date as _date
        exp_date = _date.fromisoformat(u['expires_at'][:10])
        return max(0, (exp_date - now_br().date()).days)
    except Exception:
        return 30


def _renew_one_core(user_id, current_user, days):
    """
    Lógica de renovação de UM usuário SSH. Usada por /users/renew (individual)
    e por /users/bulk_renew (lote) — um único lugar com as regras de
    reativação automática, conversão de teste e rollback em falha total da VPS.
    Retorna um dict pronto para jsonify().
    """
    u = db.get_ssh_user(user_id)
    if not u:
        return dict(success=False, message='Usuário não encontrado')

    if current_user['role'] != 'admin':
        tree_ids = [current_user['id']] + [s['id'] for s in db.get_all_resellers_under(current_user['id'])]
        if u['owner_id'] not in tree_ids:
            return dict(success=False, message='Sem permissão')

    was_suspended = u['status'] == 'suspended'
    was_test      = bool(u['is_test'])
    db.renew_ssh_user(user_id, days)

    # Teste renovado vira usuário definitivo — não exibe mais o selo "teste"
    if was_test:
        db.update_ssh_user(user_id, is_test=0)

    # Auto-reativa se estava suspenso — sem precisar clicar no botão de suspender
    if was_suspended:
        db.update_ssh_user(user_id, status='active')
        sc.broadcast_command(u, sc.build_unblock_cmd(u), db)

    updated = db.get_ssh_user(user_id)
    srv_results = _sync_renew_to_category(updated)
    ok_count = sum(1 for _, ok, _ in srv_results if ok)
    fails = [(name, msg) for name, ok, msg in srv_results if not ok]

    new_exp = (updated['expires_at'] or '')[:10]
    reativado  = ' e reativado' if was_suspended else ''
    convertido = ' (convertido de teste)' if was_test else ''

    # A VPS falhou por completo → desfaz o aumento no painel para não "mentir"
    # que renovou. O usuário continua com o vencimento antigo e sem queda de
    # consistência entre painel e servidor.
    if srv_results and ok_count == 0:
        db.update_ssh_user(user_id, expires_at=u['expires_at'])
        if was_test:
            db.update_ssh_user(user_id, is_test=1)
        fail_msg = '; '.join(f'{n}:{m}' for n, m in fails)
        return dict(success=False,
                    message='Renovação FALHOU no servidor. Nada foi alterado.',
                    server_msg=fail_msg,
                    new_expiry=(u['expires_at'] or '')[:10],
                    was_suspended=was_suspended)

    if fails:
        fds = ', '.join(n for n, _ in fails)
        return dict(success=True,
                    message=(f'Renovado{reativado}{convertido} +{days} dias '
                             f'em {ok_count}/{len(srv_results)} servidor(es). '
                             f'FALHOU na VPS: {fds}. Novo vencimento: {new_exp}'),
                    server_msg='; '.join(f'{n}:{m}' for n, m in fails),
                    new_expiry=new_exp, was_suspended=was_suspended)

    return dict(success=True,
                message=f'Renovado{reativado}{convertido} +{days} dias. Novo vencimento: {new_exp}',
                server_msg='', new_expiry=new_exp, was_suspended=was_suspended)


@app.route('/users/renew/<int:user_id>', methods=['POST'])
@login_required
def renew_user(user_id):
    current_user = get_current_user()
    days = int(request.form.get('days', 30))
    if days < 1:
        return jsonify(success=False, message='Informe quantos dias renovar')
    return jsonify(**_renew_one_core(user_id, current_user, days))


@app.route('/users/bulk_renew', methods=['POST'])
@login_required
def renew_users_bulk():
    """Renova vários usuários de uma vez (lote).
    Recebe `ids` como string separada por vírgula e `days`.
    Devolve o resultado detalhado por usuário (renovado / falhou) para a
    interface confirmar visualmente quem foi renovado."""
    current_user = get_current_user()
    raw = request.form.get('ids', '')
    ids = []
    for part in raw.replace(';', ',').split(','):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))

    if not ids:
        return jsonify(success=False, message='Nenhum usuário selecionado')
    if len(ids) > 50:
        return jsonify(success=False, message='Máximo de 50 usuários por lote')
    days = int(request.form.get('days', 30))
    if days < 1:
        return jsonify(success=False, message='Informe quantos dias renovar')

    def _run(uid):
        u = db.get_ssh_user(uid)
        nome = u['username'] if u else f'#{uid}'
        return uid, nome, _renew_one_core(uid, current_user, days)

    renewed, failed = [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(_run, uid) for uid in ids]
        try:
            for f in as_completed(futs, timeout=120):
                uid, nome, res = f.result()
                if res.get('success'):
                    renewed.append({'id': uid, 'user': nome, 'new_expiry': res.get('new_expiry', '')})
                else:
                    failed.append({'id': uid, 'user': nome, 'error': res.get('message', 'erro desconhecido')})
        except Exception:
            # Tempo esgotado: conta o que já terminou e marca o resto como falha
            for f in futs:
                if f.done() and not f.cancelled():
                    try:
                        uid, nome, res = f.result()
                    except Exception:
                        continue
                    if res.get('success'):
                        if not any(r['id'] == uid for r in renewed):
                            renewed.append({'id': uid, 'user': nome, 'new_expiry': res.get('new_expiry', '')})
                    elif not any(r['id'] == uid for r in failed):
                        failed.append({'id': uid, 'user': nome, 'error': res.get('message', 'tempo esgotado')})
                else:
                    f.cancel()

    renewed.sort(key=lambda r: r['user'].lower())
    failed.sort(key=lambda r: r['user'].lower())

    msg = f'{len(renewed)} usuário(s) renovado(s)'
    if failed:
        msg += f' · {len(failed)} falha(s)'
    return jsonify(success=bool(renewed), message=msg, days=days,
                   renewed=renewed, failed=failed)


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

    allowed_fields = {'connection_limit', 'status', 'expires_at', 'password', 'telefone'}
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
        err_pw = db.ssh_password_error(new_password, u['username'])
        if err_pw:
            return jsonify(success=False, message=err_pw)
        # grava cifrada no banco (Fernet)
        db.update_ssh_user(user_id, password=db.encrypt_ssh_password(new_password))
        # Aplica em TODOS os servidores do usuário: se ficasse só no primário,
        # o cliente continuaria com a senha antiga nos servidores extras.
        applied, failed = [], []
        for sid in db.get_user_all_server_ids(user_id):
            srv = db.get_server(sid)
            if not srv:
                continue
            s_ok, s_msg = sc.change_ssh_password_on_server(
                srv['ip'], srv['module_port'], srv['auth_token'],
                u['username'], new_password
            )
            if s_ok:
                applied.append(srv['name'])
            else:
                failed.append(f"{srv['name']}: {s_msg}")
        if applied:
            server_msg = 'senha aplicada em: ' + ', '.join(applied)
        if failed:
            server_msg = (server_msg + '; ' if server_msg else '') + 'falhou em: ' + '; '.join(failed)

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
        # Lock account, kill live sessions and drop the VLESS UUID — on ALL servers
        sc.broadcast_command(u, sc.build_block_cmd(u), db)
    else:
        # Unlock account on ALL servers
        sc.broadcast_command(u, sc.build_unblock_cmd(u), db)

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
        exp_days = _days_left(u)
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
            # Já existe na máquina: só sincroniza a EXPIRAÇÃO com o painel.
            # O usuário pode existir mas com data divergente (a máquina mostra
            # "expirado" enquanto o painel ainda tem validade). O botão
            # "Sincronizar" força a data da máquina a bater com a do painel.
            exp_date = (u.get('expires_at') or '')[:10]
            if exp_date:
                ch_ok, ch_out = sc.send_command(
                    srv['ip'], srv['module_port'], srv['auth_token'],
                    f"chage -E {exp_date} {u['username']} 2>&1 && echo EXP_SYNC_OK || echo EXP_SYNC_FAIL"
                )
                if ch_ok and 'EXP_SYNC_OK' in (ch_out or ''):
                    results.append({'server': srv['name'], 'action': f'expiração sincronizada p/ {exp_date}', 'ok': True})
                    continue
            # Sem data no painel, ou chage falhou (ex.: só existe no Xray,
            # sem conta Linux) → recria embaixo para garantir.

        # ── Cria o usuário (ou adiciona UUID ao Xray se estiver faltando) ──
        c_ok, c_msg = sc.create_ssh_user_on_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            u['username'], db.ssh_plain_password(u['password']), exp_days, u['connection_limit'],
            uuid=u['v2ray_uuid']
        )
        action = 'criado' if c_ok else f'erro: {c_msg}'
        # Se o usuário já existia e só estávamos re-sincronizando a data,
        # mantém a ação clara no resumo.
        results.append({'server': srv['name'], 'action': action, 'ok': c_ok})

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
        exp_days = _days_left(u)
    except Exception:
        exp_days = 30

    s_ok, s_msg = sc.create_ssh_user_on_server(
        srv['ip'], srv['module_port'], srv['auth_token'],
        u['username'], db.ssh_plain_password(u['password']), exp_days, u['connection_limit'],
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

    r_counts = {
        'all':       len(resellers),
        'mine':      sum(1 for r in resellers if r['parent_id'] == current_user['id']),
        'expiring':  sum(1 for r in resellers if r['expires_at'] and 0 <= days_until(r['expires_at']) <= 7),
    }

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
                           f_type=f_type, f_parent=f_parent,
                           r_counts=r_counts)


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

    err_pw = db.password_policy_error(password, username)
    if err_pw:
        return jsonify(success=False, message=err_pw)

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
    err_pw = db.password_policy_error(new_pw, r['username'])
    if err_pw:
        return jsonify(success=False, message=err_pw)
    db.update_panel_user(reseller_id,
                         password_hash=db.hash_password(new_pw),
                         must_change_password=0)
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
            sc.broadcast_command(u, sc.build_block_cmd(u), db)
    else:
        # Reactivate: unlock accounts on ALL servers
        for u in affected_users:
            db.update_ssh_user(u['id'], status='active')
            sc.broadcast_command(u, sc.build_unblock_cmd(u), db)

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


def _auto_attach_category_users(server_id: int) -> tuple:
    """
    Vincula e cadastra na VPS todos os usuários ativos da categoria do servidor.
    Quando um servidor é adicionado (ou muda de categoria), os usuários da
    mesma categoria passam a incluir o servidor automaticamente:
      - cria os vínculos em ssh_user_servers (renovar/excluir passa a atuar nele)
      - cria na VPS quem ainda não existe (sync pula duplicatas)
    Returns (users_count, message).
    """
    srv = db.get_server(server_id)
    if not srv or not srv['category_id']:
        return 0, 'Servidor sem categoria'
    users = db.get_active_users_by_category(srv['category_id'])
    if not users:
        return 0, 'Nenhum usuário ativo na categoria'

    for u in users:
        db.add_user_extra_server(u['id'], server_id)

    try:
        ok, msg = sc.sync_users_to_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            [dict(u) for u in users], force=False
        )
    except Exception as e:
        return len(users), f'Vínculo ok, mas envio falhou: {e}'
    return len(users), 'OK' if ok else f'Vínculo ok, mas envio falhou: {msg}'


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
    sync_note = ''
    cat_id = request.form.get('category_id', '').strip()
    if cat_id and cat_id.isdigit():
        db.update_server(new_id, category_id=int(cat_id))
        n, m = _auto_attach_category_users(new_id)
        if n:
            sync_note = f' | {n} usuário(s) da categoria vinculados ({m})'
    return jsonify(success=True, message='Servidor adicionado' + sync_note, server_id=new_id)


    refresh_vnc_proxy()

@app.route('/servers/delete/<int:server_id>', methods=['POST'])
@admin_required
def delete_server(server_id):
    db.delete_server(server_id)
    return jsonify(success=True, message='Servidor removido')


    refresh_vnc_proxy()

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
    # Se a categoria mudou, inclui automaticamente os usuários da nova categoria
    sync_note = ''
    if 'category_id' in updates:
        n, m = _auto_attach_category_users(server_id)
        if n:
            sync_note = f' | {n} usuário(s) da categoria vinculados ({m})'
    return jsonify(success=True, message='Servidor atualizado' + sync_note)


    refresh_vnc_proxy()

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


@app.route('/servers/diagnose/<int:server_id>', methods=['POST'])
@admin_required
def diagnose_server(server_id):
    """Diagnostica conectividade SSH + módulo 7270 + agent no servidor."""
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')

    try:
        result = sc.diagnose_server(
            srv['ip'], srv['module_port'], srv['auth_token'],
            srv['root_user'], srv['root_password'])
    except Exception as e:
        app.logger.error('Erro ao diagnosticar servidor %s: %s', srv['ip'], e)
        return jsonify(success=False, message='Falha ao diagnosticar: {}'.format(e))

    checks = result['checks']
    failed = [c for c in checks if not c['ok']]
    warned = [c for c in checks if c.get('warn')]
    healthy = len(failed) == 0
    if healthy and warned:
        summary = ('Servidor operante com {} alerta(s).'.format(len(warned)))
    elif healthy:
        summary = 'Servidor OK.'
    else:
        summary = '{} problema(s) encontrado(s).'.format(len(failed))
    return jsonify(success=True, message=summary, checks=checks,
                   healthy=healthy, warned=len(warned),
                   ssh=result['ssh'], module=result['module'])


@app.route('/servers/cleanup/<int:server_id>', methods=['POST'])
@admin_required
def server_cleanup(server_id):
    """Limpeza segura de espaço na VPS: apt clean, logs antigos/grandes e /tmp."""
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')
    try:
        ok, info = sc.cleanup_vps(srv['ip'], srv['module_port'], srv['auth_token'])
    except Exception as e:
        app.logger.error('Erro na limpeza do servidor %s: %s', srv['ip'], e)
        return jsonify(success=False, message='Falha na limpeza: {}'.format(e))

    if not ok:
        return jsonify(success=False, message='Módulo não respondeu: {}'.format(
            (info.get('detail') or 'erro').strip()[:200]))

    freed = info.get('freed_mb')
    before = info.get('before_mb')
    after = info.get('after_mb')
    if freed is None:
        msg = 'Limpeza executada, mas não deu para medir o espaço liberado.'
    elif freed <= 0:
        msg = 'Nada de sobra para limpar (limpeza segura já estava em dia).'
    else:
        msg = 'Limpeza concluída: {} MB liberados.'.format(freed)
    return jsonify(success=True, message=msg, freed_mb=freed,
                   before_mb=before, after_mb=after,
                   detail=(info.get('detail') or '')[:600])


def refresh_vnc_proxy() -> None:
    """Regenera o mapa de upstreams do nginx a partir dos servidores cadastrados.

    Chamado depois de adicionar, editar ou remover servidor, e no start do
    painel. Sem isso o mapa ficaria desatualizado e o console de um servidor
    novo cairia no default, abrindo em branco.
    """
    try:
        conn = db.get_db()
        rows = conn.execute("SELECT id, ip FROM servers WHERE ip IS NOT NULL AND ip != ''").fetchall()
        sc.install_vnc_proxy([dict(r) for r in rows])
    except Exception:
        pass


def panel_public_ip() -> str:
    """IP publico do proprio painel, usado para restringir o VNC no servidor.

    O servidor so aceita conexao do VNC vinda deste IP, que e o caminho normal
    de acesso (pelo navegador, via este painel).
    """
    return (os.environ.get('PANEL_PUBLIC_IP') or '200.234.207.46').strip()


@app.route('/servers/setup_vnc/<int:server_id>', methods=['POST'])
@admin_required
def setup_vnc(server_id):
    """Instala/configura um servidor VNC (Xvfb + x11vnc) para recuperação da máquina
    quando o SSH (porta 22) estiver corrompido ou indisponível."""
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')
    # nova senha só quando pedida explicitamente; por padrão preserva a atual,
    # senão cada clique invalidava a senha que o usuário já tinha anotado
    new_pass = (request.form.get('new_password') or '').strip() or None
    ok, info = sc.setup_vnc_ssh(srv['ip'], srv['root_user'], srv['root_password'],
                                new_password=new_pass, panel_ip=panel_public_ip())
    info['vnc_ip'] = srv['ip']
    return jsonify(**info)


@app.route('/servers/vnc_status/<int:server_id>', methods=['POST'])
@admin_required
def vnc_status(server_id):
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')
    ok, info = sc.vnc_status_ssh(srv['ip'], srv['root_user'], srv['root_password'])
    info['vnc_ip'] = srv['ip']
    info['vnc_name'] = srv['name']
    info.setdefault('success', False)
    return jsonify(**info)


@app.route('/servers/vnc_firewall/<int:server_id>', methods=['POST'])
@admin_required
def vnc_firewall(server_id):
    """Liga/desliga o acesso externo ao console VNC.

    restrict = só o painel acessa (padrão, e o caminho do navegador).
    open = libera 5900/6080 para a internet, para cliente VNC externo.
    """
    srv = db.get_server(server_id)
    if not srv:
        return jsonify(success=False, message='Servidor não encontrado')
    mode = (request.form.get('mode') or 'restrict').strip().lower()
    if mode not in ('restrict', 'open'):
        return jsonify(success=False, message="Modo inválido. Use 'restrict' ou 'open'.")
    if mode == 'open' and not request.form.get('confirm'):
        return jsonify(success=False, needs_confirm=True,
                       message=('Liberar o VNC para a internet expõe o servidor a '
                                'força bruta: o x11vnc usa VNC auth clássica (DES de 56 bits, '
                                'senha de 8 caracteres) e o prêmio é o root da máquina. '
                                'Confirme para continuar.'))
    ok, info = sc.vnc_firewall_ssh(srv['ip'], srv['root_user'], srv['root_password'],
                                   mode, panel_ip=panel_public_ip())
    info['firewall_mode'] = mode if ok else info.get('firewall_mode')
    return jsonify(**info)


@app.route('/_vnc_authz')
def _vnc_authz():
    """Usado pelo auth_request do nginx para o proxy do VNC.

    O nginx nao entende a sessao do Flask, entao pergunta aqui antes de
    encaminhar o WebSocket. Devolve 200 (pode) ou 401/403 (nao pode), sem
    corpo e sem redirecionar. Fica marcado como internal no nginx, ou seja,
    nao da para chamar pela internet.
    """
    if 'user_id' not in session:
        return '', 401
    user = db.get_panel_user(session['user_id'])
    if not user or user['status'] == 'suspended':
        session.clear()
        return '', 401
    if session.get('role') != 'admin':
        return '', 403
    return '', 200



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
        # The admin's own accounts have owner_id = admin.id, but get_resellers()
        # only returns role='reseller'. Without including the admin here the
        # "Dono" dropdown has no way to select the admin's own online users.
        all_resellers = [dict(current_user)] + [dict(r) for r in db.get_resellers()]
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
                        # Teste pago vira usuário definitivo — não exibe mais o selo "teste"
                        db.update_ssh_user(payment['ssh_user_id'], is_test=0)
                        # Reactivate user (may have been suspended due to expiry)
                        db.update_ssh_user(payment['ssh_user_id'], status='active')
                        u = db.get_ssh_user(payment['ssh_user_id'])
                        if u:
                            # Renova/cria em TODOS os servidores da categoria (não só nos já vinculados)
                            srv_results = _sync_renew_to_category(u)
                            for name, ok, msg in srv_results:
                                if not ok:
                                    app.logger.warning(
                                        f"[MP webhook] Falha ao renovar '{u['username']}' "
                                        f"no servidor '{name}': {msg}"
                                    )
                            sc.broadcast_command(u, sc.build_unblock_cmd(u), db)
                except Exception as e:
                    app.logger.error(f"[MP webhook] Erro ao processar payment_id={payment_id}: {e}")

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

        blocked, remaining = _login_lock_state(username)
        if blocked:
            app.logger.warning('Login de usuario bloqueado (brute-force): ip=%s user=%s',
                               _client_ip(), username[:60])
            error = _login_lock_message(remaining)
            return render_template('user_login.html', user_data=None, error=error), 429

        u = db.get_ssh_user_by_username(username)

        # A senha do cliente final é comparada via Fernet (barato), mas o
        # caminho "usuário não existe" queima um PBKDF2. Sem queimar o PBKDF2
        # nos DOIS caminhos o tempo de resposta revelaria quais usuários
        # existem (existente ~40ms x inexistente ~590ms) — enumeração de
        # usernames. Por isso o KDF falso roda sempre, exista o usuário ou não.
        db.check_password(password, _DUMMY_HASH)

        if u and secrets.compare_digest(db.ssh_plain_password(u['password']), password):
            _login_register_success(username)
            user_data = dict(u)
            # Sessão nova: o cliente final NÃO pode herdar poderes de uma
            # sessão de admin/revenda anterior no mesmo navegador (computador
            # compartilhado). Sem o session.clear() o login do cliente mantinha
            # user_id/role da sessão anterior e o cliente virava admin.
            _csrf = session.get('csrf_token')
            session.clear()
            if _csrf:
                session['csrf_token'] = _csrf
            # Store in session so generate_pix can use it without re-auth
            session['pix_user_id'] = u['id']
        else:
            locked_now, dur = _login_register_fail(username)
            if locked_now:
                app.logger.warning('Brute-force detectado em /user/login: bloqueio de %ss ip=%s user=%s',
                                   dur, _client_ip(), username[:60])
                error = _login_lock_message(dur)
                return render_template('user_login.html', user_data=None, error=error), 429
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


_CONN_CACHE = {}
_CONN_CACHE_TTL = 25  # segundos: apps de VPN consultam com frequência


def _get_user_connections(username: str) -> int:
    """Conexões ativas de um usuário em TODAS as VPSes.

    Otimizado para o CheckUser ficar leve:
    - cache curto (TTL 25s) por username → consultas repetidas não tocam nas VPSes;
    - consulta PARALELA às VPSes com timeout curto por servidor (≈1s típico,
      no máximo ~4s mesmo com servidor fora do ar), em vez de esperar servidor
      por servidor em sequência.
    """
    now = time.time()
    hit = _CONN_CACHE.get(username)
    if hit and now - hit[0] < _CONN_CACHE_TTL:
        return hit[1]

    servers = db.get_all_servers()

    def _count(srv):
        try:
            return sc.get_user_connections(
                srv['ip'], srv['module_port'], srv['auth_token'], username)
        except Exception:
            return 0

    total = 0
    if servers:
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(servers)))) as pool:
            total = sum(pool.map(_count, servers))

    _CONN_CACHE[username] = (now, total)
    return total


def _checkuser_rate_ok() -> bool:
    """Limite de consultas no /checkuser por IP (evita enumerar usuários)."""
    ip = _client_ip()
    now = time.time()
    with _login_state_lock:
        hits = [t for t in _checkuser_hits.get(ip, []) if now - t < 60]
        if len(hits) >= CHECKUSER_MAX_PER_MIN:
            _checkuser_hits[ip] = hits
            return False
        hits.append(now)
        _checkuser_hits[ip] = hits
        if len(_checkuser_hits) > 5000:
            for k in [k for k, v in _checkuser_hits.items() if not v or now - v[-1] > 60]:
                _checkuser_hits.pop(k, None)
    return True


def _checkuser_token_ok() -> bool:
    """Se houver token configurado, o cliente precisa enviá-lo."""
    if not CHECKUSER_TOKEN:
        return True
    sent = (request.args.get('token') or request.headers.get('X-Check-Token') or '').strip()
    return bool(sent) and secrets.compare_digest(sent, CHECKUSER_TOKEN)


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

    # `expires_at` é o último dia de uso: venceu só quando a data já passou.
    d = days_until(u_dict['expires_at'])
    days_left = max(0, d)
    expired   = d < 0

    connections = _get_user_connections(u_dict['username'])
    limit = u_dict['connection_limit']

    if expired:
        online_status = "expirado"
    elif connections > 0 and limit > 0 and connections >= limit:
        online_status = "limite_excedido"
    elif connections > 0:
        online_status = "online"
    else:
        online_status = "offline"

    try:
        from datetime import datetime as _dt
        exp_dt = _dt.fromisoformat(u_dict['expires_at'][:19])
        expiration_date = exp_dt.strftime('%d/%m/%Y')
        expires_at_formatted = exp_dt.strftime('%Y-%m-%d')
    except Exception:
        expiration_date = u_dict['expires_at'][:10] if u_dict['expires_at'] else ''
        expires_at_formatted = expiration_date

    resp = {
        # Formato original (apps VPN / DTunnel)
        "id": "01",
        "username": u_dict['username'],
        "count_connections": connections,
        "limit_connections": limit,
        "expiration_date": expiration_date,
        "expiration_days": str(days_left),

        # Formato moderno (frontend do painel)
        "exists": True,
        "status": online_status,
        "online": connections > 0,
        "expired": expired,
        "blocked": u_dict['status'] == 'suspended',
        "connections": connections,
        "limit": limit,
        "days_left": days_left,
        "expires_at": expires_at_formatted,
        "created_at": (u_dict.get('created_at') or '')[:10],
    }
    return jsonify(resp)


@app.route('/checkuser/dtunnel.php', methods=['GET', 'POST'])
def checkuser_dtunnel():
    if not _checkuser_token_ok():
        return jsonify({"error": "invalid token"}), 403
    if not _checkuser_rate_ok():
        return jsonify({"error": "too many requests"}), 429
    username = (request.args.get('user') or request.form.get('user') or '').strip()
    if not username:
        return jsonify({"error": "missing user parameter"})
    return _checkuser_response(username)


@app.route('/checkuser/', methods=['GET', 'POST'])
@app.route('/checkuser', methods=['GET', 'POST'])
def checkuser_index():
    """DTunnel/HTTP style: /checkuser?user=USERNAME"""
    if not _checkuser_token_ok():
        return jsonify({"error": "invalid token"}), 403
    if not _checkuser_rate_ok():
        return jsonify({"error": "too many requests"}), 429
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
    if not _checkuser_token_ok():
        return jsonify({"error": "invalid token"}), 403
    if not _checkuser_rate_ok():
        return jsonify({"error": "too many requests"}), 429
    username = (request.args.get('user') or request.args.get('username')
                or request.form.get('user') or request.form.get('username') or '').strip()
    if not username:
        return jsonify({"error": "missing user parameter"})
    return _checkuser_response(username)


@app.route('/checkuser/<path:username>', methods=['GET', 'POST'])
def checkuser(username):
    if not _checkuser_token_ok():
        return jsonify({"error": "invalid token"}), 403
    if not _checkuser_rate_ok():
        return jsonify({"error": "too many requests"}), 429
    return _checkuser_response(username)


@app.route('/check/<path:username>', methods=['GET', 'POST'])
def checkuser_check_path(username):
    if not _checkuser_token_ok():
        return jsonify({"error": "invalid token"}), 403
    if not _checkuser_rate_ok():
        return jsonify({"error": "too many requests"}), 429
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
        password=db.ssh_plain_password(u['password']),
        v2ray_uuid=u['v2ray_uuid'],
        expires_at=u['expires_at'],
        connection_limit=u['connection_limit'],
        telefone=u['telefone'] if u['telefone'] else '',
    )

# ---------------------------------------------------------------------------
# Random generation API
# ---------------------------------------------------------------------------

@app.route('/api/random_credentials')
@login_required
def random_credentials():
    return jsonify(
        username=db.random_username('user'),
        password=db.random_password(8),   # alfanumérica de 8 chars (letra+número)
        uuid=db.random_uuid()
    )


@app.route('/users/message-template', methods=['POST'])
@login_required
def save_message_template():
    current_user = get_current_user()
    template = request.form.get('template', '')
    db.set_message_template(current_user['id'], template)
    restored = not template.strip()
    return jsonify(
        success=True,
        template='' if restored else template,
        message='Formato padrão restaurado.' if restored else 'Formato da mensagem salvo.',
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
        old_pw = request.form.get('current_password', '').strip()
        # A troca de senha é OPCIONAL (ninguém é obrigado a trocar). Se o
        # usuário preencher a senha atual, ela é conferida; se deixar em
        # branco, a troca segue normal.
        if old_pw and not db.check_password(old_pw, current_user['password_hash']):
            flash('Senha atual incorreta.', 'danger')
            return redirect(url_for('profile'))
        err = db.password_policy_error(new_pw, current_user['username'])
        if err:
            flash(err, 'danger')
        elif new_pw == (old_pw or ''):
            flash('A nova senha deve ser diferente da atual.', 'danger')
        else:
            db.update_panel_user(current_user['id'], password_hash=db.hash_password(new_pw))
            db.set_must_change_password(current_user['id'], False)
            flash('Senha alterada com sucesso!', 'success')
    return render_template('shared/profile.html')


@app.route('/profile/appearance', methods=['POST'])
@login_required
def save_appearance():
    """Salva tema (dark/light) e estilo do painel na CONTA do usuário.
    Admin e revenda cada um fica com a própria escolha."""
    current_user = get_current_user()
    theme = (request.form.get('theme') or '').strip()
    style = (request.form.get('style') or '').strip()
    if theme not in ('', 'dark', 'light'):
        return jsonify(success=False, message='Tema inválido')
    if style and style not in STYLE_PRESET_IDS:
        return jsonify(success=False, message='Estilo inválido')
    db.set_appearance(current_user['id'], theme=theme, style=style)
    return jsonify(success=True)




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

    `expires_at` is the LAST usable day, so the user is only blocked once that
    day is over: date(expires_at) < date(today). Comparing raw strings with
    `<=` used to block everyone at 00:00 of their due date, one day early.
    """
    conn = db.get_db()
    expired = conn.execute(
        "SELECT * FROM ssh_users WHERE is_test=0 AND status='active' "
        "AND date(expires_at) < date(?)",
        (now_br().strftime('%Y-%m-%d'),)
    ).fetchall()
    conn.close()
    for u in expired:
        # Block on ALL servers (primary + extras): lock password, kill live
        # sessions and remove the VLESS UUID from Xray.
        results = sc.broadcast_command(u, sc.build_block_cmd(u), db)
        for name, ok, out in results:
            if not ok:
                app.logger.warning(
                    f"[expire] Falha ao bloquear '{u['username']}' "
                    f"no servidor '{name}': {out}"
                )
        db.update_ssh_user(u['id'], status='suspended')


def job_reconcile_suspended():
    """Re-assert the block on every suspended user that has a VLESS UUID.

    The expire job only looks at users still flagged 'active', so anyone
    suspended before the blockuser fix (or whose Xray entry came back) stays
    connectable through VLESS forever. This job is idempotent and self-healing:
    it re-sends the block command for every suspended VLESS user.
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM ssh_users WHERE status='suspended' "
        "AND v2ray_uuid IS NOT NULL AND v2ray_uuid != ''"
    ).fetchall()
    conn.close()
    for u in rows:
        for name, ok, out in sc.broadcast_command(u, sc.build_block_cmd(u), db):
            if not ok:
                app.logger.warning(
                    f"[reconcile] Falha ao re-bloquear '{u['username']}' "
                    f"no servidor '{name}': {out}"
                )


scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(job_backup, 'interval', hours=6, id='backup_job')
scheduler.add_job(job_cleanup_tests, 'interval', minutes=5, id='cleanup_tests')
scheduler.add_job(job_expire_regular_users, 'interval', minutes=10, id='expire_users')
scheduler.add_job(job_reconcile_suspended, 'interval', minutes=30, id='reconcile_suspended')
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
            err_pw = db.password_policy_error(text)
            if err_pw:
                _tg_send(bot_token, chat_id, f'❌ {err_pw} Tente novamente:')
                return
            conn = db.get_db()
            row  = conn.execute(
                "SELECT id FROM panel_users WHERE role='admin' LIMIT 1").fetchone()
            conn.close()
            if row:
                db.update_panel_user(row['id'],
                                     password_hash=db.hash_password(text),
                                     must_change_password=0)
            _tg_state.pop(chat_id, None)
            _tg_send(bot_token, chat_id, '✅ Senha alterada!')
            _tg_send(bot_token, chat_id, '🏠 Menu:', _main_menu_kb())
            return

        if st.get('state') == 'await_reset_pw':
            conn     = db.get_db()
            admin_r  = conn.execute(
                "SELECT * FROM panel_users WHERE role='admin' LIMIT 1").fetchone()
            conn.close()
            # só o HASH vale — nunca a senha em texto puro (podia estar
            # desatualizada no banco e virar backdoor de reset)
            ok_pw = admin_r and db.check_password(text, admin_r['password_hash'])
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
# Suporte / tickets
# ---------------------------------------------------------------------------
#
# Regra de visibilidade, valida sempre no servidor:
#   admin    -> ve todos os tickets
#   revenda  -> ve apenas os tickets que ela mesma abriu
#
# A checagem acontece em _ticket_acessivel() e nao no template, porque o id do
# ticket aparece na URL: esconder o link na tela nao impede ninguem de digitar
# /tickets/17 e ler o ticket de outro usuario.

def _ticket_ids_visiveis(user):
    """panel_users ids whose tickets this user may see."""
    if user['role'] == 'admin':
        return None                      # None = sem restricao
    return [user['id']]


def _ticket_acessivel(ticket_id, user):
    """Return the ticket if this user may see it, else None."""
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return None
    visiveis = _ticket_ids_visiveis(user)
    # O que decide e o DONO do ticket, nao quem esta perguntando: a revenda so
    # enxerga os tickets cujo user_id e ela propria.
    if visiveis is not None and ticket['user_id'] not in visiveis:
        return None
    return ticket


@app.route('/tickets')
@login_required
def tickets_list():
    user = get_current_user()
    visiveis = _ticket_ids_visiveis(user)

    f_status = request.args.get('status', '')
    busca    = request.args.get('q', '').strip()

    tickets = db.get_tickets(visible_user_ids=visiveis, status=f_status)
    if busca:
        alvo = busca.lower()
        tickets = [t for t in tickets if alvo in (t['subject'] or '').lower()]

    return render_template('shared/tickets.html',
                           tickets=tickets,
                           counts=db.ticket_counts(visiveis),
                           f_status=f_status, busca=busca,
                           categorias=db.TICKET_CATEGORIES,
                           prioridades=db.TICKET_PRIORITIES,
                           status_labels=db.TICKET_STATUSES,
                           informativos=(db.get_informativos(only_active=False)
                                         if user['role'] == 'admin'
                                         else db.get_informativos_para_usuario(user['id'])),
                           is_admin=(user['role'] == 'admin'))


@app.route('/tickets/create', methods=['POST'])
@login_required
def tickets_create():
    user = get_current_user()
    ok, msg, tid = db.create_ticket(
        user['id'],
        request.form.get('subject', ''),
        request.form.get('category', 'bug'),
        request.form.get('priority', 'normal'),
        request.form.get('body', ''),
    )
    if not ok:
        return jsonify(success=False, message=msg)
    return jsonify(success=True, message=msg, ticket_id=tid)


@app.route('/tickets/<int:ticket_id>')
@login_required
def ticket_detail(ticket_id):
    user = get_current_user()
    ticket = _ticket_acessivel(ticket_id, user)
    if not ticket:
        flash('Ticket não encontrado.', 'danger')
        return redirect(url_for('tickets_list'))

    return render_template('shared/ticket_detail.html',
                           ticket=ticket,
                           messages=db.get_ticket_messages(ticket_id),
                           status_labels=db.TICKET_STATUSES,
                           categorias=db.TICKET_CATEGORIES,
                           is_admin=user['role'] == 'admin')


@app.route('/tickets/<int:ticket_id>/reply', methods=['POST'])
@login_required
def ticket_reply(ticket_id):
    user = get_current_user()
    if not _ticket_acessivel(ticket_id, user):
        return jsonify(success=False, message='Ticket não encontrado.'), 404

    ok, msg = db.add_ticket_message(
        ticket_id, user['id'],
        request.form.get('body', ''),
        is_staff=(user['role'] == 'admin'),
    )
    return jsonify(success=ok, message=msg)


@app.route('/tickets/<int:ticket_id>/status', methods=['POST'])
@admin_required
def ticket_set_status(ticket_id):
    if not db.get_ticket(ticket_id):
        return jsonify(success=False, message='Ticket não encontrado.'), 404
    ok, msg = db.set_ticket_status(ticket_id, request.form.get('status', ''))
    return jsonify(success=ok, message=msg)


@app.route('/tickets/<int:ticket_id>/delete', methods=['POST'])
@admin_required
def ticket_delete(ticket_id):
    if not db.get_ticket(ticket_id):
        return jsonify(success=False, message='Ticket não encontrado.'), 404
    db.delete_ticket(ticket_id)
    return jsonify(success=True, message='Ticket removido.')


# ---------------------------------------------------------------------------
# Informativos (avisos do Suporte)
# ---------------------------------------------------------------------------
# Regras:
#   - Só o admin cria/edita/exclui informativos.
#   - TODAS as revendas (qualquer nível na árvore) enxergam os informativos
#     ativos, independente de parent_id — não há filtro por hierarquia.
#   - A revenda marca como lido; o admin vê o relatório de quem já leu.

@app.route('/informativos/create', methods=['POST'])
@admin_required
def informativo_create():
    user = get_current_user()
    ok, msg, iid = db.create_informativo(
        user['id'],
        request.form.get('titulo', ''),
        request.form.get('conteudo', ''),
    )
    return jsonify(success=ok, message=msg, id=iid)


@app.route('/informativos/update/<int:informativo_id>', methods=['POST'])
@admin_required
def informativo_update(informativo_id):
    if not db.get_informativo(informativo_id):
        return jsonify(success=False, message='Informativo não encontrado.'), 404
    ativo = 0 if request.form.get('ativo', '1') in ('0', 'false', 'off') else 1
    ok, msg = db.update_informativo(
        informativo_id,
        request.form.get('titulo', ''),
        request.form.get('conteudo', ''),
        ativo,
    )
    return jsonify(success=ok, message=msg)


@app.route('/informativos/delete/<int:informativo_id>', methods=['POST'])
@admin_required
def informativo_delete(informativo_id):
    db.delete_informativo(informativo_id)
    return jsonify(success=True, message='Informativo excluído.')


@app.route('/informativos/read/<int:informativo_id>', methods=['POST'])
@login_required
def informativo_read(informativo_id):
    """Marca o informativo como lido para o usuário logado."""
    user = get_current_user()
    if not db.get_informativo(informativo_id):
        return jsonify(success=False, message='Informativo não encontrado.'), 404
    db.mark_informativo_read(informativo_id, user['id'])
    return jsonify(success=True, message='Marcado como lido.',
                   restante=db.count_informativos_nao_lidos(user['id']))


@app.route('/informativos/<int:informativo_id>/reads')
@admin_required
def informativo_reads(informativo_id):
    """Relatório de leitura: quem leu (com data) e quem ainda não leu."""
    info = db.get_informativo(informativo_id)
    if not info:
        return jsonify(success=False, message='Informativo não encontrado.'), 404
    lidos = {r['username']: r['read_at']
             for r in db.get_informativo_reads(informativo_id)}
    todos = db.get_resellers(parent_id=None)  # todas as revendas, toda a árvore
    return jsonify(
        success=True,
        titulo=info['titulo'],
        total=len(todos),
        lidos=[{'username': u['username'], 'read_at': lidos[u['username']]}
               for u in todos if u['username'] in lidos],
        nao_lidos=[u['username'] for u in todos if u['username'] not in lidos],
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

# Start Telegram bot polling thread AFTER all functions are defined
start_tg_polling()

def _bootstrap_vnc_proxy():
    """Garante que o proxy do console VNC exista ao subir o painel.

    Precisa rodar no start porque uma instalacao nova em outra VPS nao tem nem
    o snippet do nginx nem o mapa de upstreams: sem isso o console simplesmente
    nao teria por onde passar, e o erro apareceria so como 'nao abre'.
    """
    if os.environ.get('VNC_PROXY_BOOTSTRAP', '1').lower() == '0':
        return
    try:
        conn = db.get_db()
        rows = conn.execute("SELECT id, ip FROM servers WHERE ip IS NOT NULL AND ip != ''").fetchall()
        ok, info = sc.install_vnc_proxy([dict(r) for r in rows])
        if ok:
            app.logger.info('proxy do console VNC pronto: %s', info.get('message'))
        else:
            app.logger.warning('proxy do console VNC nao instalado: %s', info.get('message'))
    except Exception as e:
        app.logger.warning('falha ao preparar o proxy do VNC: %s', e)


if __name__ == '__main__':
    # Use `or` so that an empty-string PORT env-var also falls back to the default
    port = int(os.environ.get('PORT') or 2083)
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    # Por padrão o app só escuta em 127.0.0.1 (o nginx local é o único
    # cliente). Escutar em 0.0.0.0 allow que se pule o nginx e falsifique
    # o X-Forwarded-For para escapar do rate-limit.
    host = os.environ.get('HOST', '127.0.0.1')
    _bootstrap_vnc_proxy()
    app.run(host=host, port=port, debug=debug)
