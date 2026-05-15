#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Database module — SQLite3 helpers for Painel Master
"""

import sqlite3
import hashlib
import os
import secrets
import string
from datetime import datetime, timedelta
import pytz

DB_PATH = os.path.join(os.path.dirname(__file__), 'painel.db')
TZ = pytz.timezone('America/Sao_Paulo')

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS panel_users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT    NOT NULL COLLATE NOCASE,
    password_hash       TEXT    NOT NULL,
    password_plain      TEXT    DEFAULT '',
    role                TEXT    NOT NULL DEFAULT 'reseller',
    parent_id           INTEGER,
    created_at          TEXT    DEFAULT (datetime('now')),
    expires_at          TEXT,
    account_limit       INTEGER DEFAULT 10,  -- -1 = unlimited (admin only)
    accounts_used       INTEGER DEFAULT 0,
    mercadopago_token   TEXT,
    mercadopago_price   REAL    DEFAULT 0,
    status              TEXT    DEFAULT 'active',
    UNIQUE(username),
    FOREIGN KEY (parent_id) REFERENCES panel_users(id)
);

CREATE TABLE IF NOT EXISTS ssh_users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT    NOT NULL COLLATE NOCASE,
    password         TEXT    NOT NULL,
    owner_id         INTEGER NOT NULL,
    server_id        INTEGER,
    created_at       TEXT    DEFAULT (datetime('now')),
    expires_at       TEXT    NOT NULL,
    connection_limit INTEGER DEFAULT 1,
    v2ray_uuid       TEXT,
    is_test          INTEGER DEFAULT 0,
    status           TEXT    DEFAULT 'active',
    UNIQUE(username),
    FOREIGN KEY (owner_id) REFERENCES panel_users(id),
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS reseller_servers (
    reseller_id INTEGER NOT NULL,
    server_id   INTEGER NOT NULL,
    PRIMARY KEY (reseller_id, server_id),
    FOREIGN KEY (reseller_id) REFERENCES panel_users(id) ON DELETE CASCADE,
    FOREIGN KEY (server_id)   REFERENCES servers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS server_categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    created_at TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS servers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    ip            TEXT    NOT NULL,
    module_port   INTEGER DEFAULT 7270,
    root_user     TEXT    DEFAULT 'root',
    root_password TEXT,
    auth_token    TEXT    NOT NULL,
    category_id   INTEGER REFERENCES server_categories(id) ON DELETE SET NULL,
    status        TEXT    DEFAULT 'active',
    created_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ssh_user_id   INTEGER NOT NULL,
    owner_id      INTEGER NOT NULL,
    amount        REAL    NOT NULL,
    payment_id    TEXT,
    payer_email   TEXT,
    status        TEXT    DEFAULT 'pending',
    created_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backup_config (
    id                  INTEGER PRIMARY KEY DEFAULT 1,
    telegram_bot_token  TEXT,
    telegram_chat_id    TEXT,
    enabled             INTEGER DEFAULT 0,
    last_backup         TEXT
);

INSERT OR IGNORE INTO backup_config (id) VALUES (1);

INSERT OR IGNORE INTO settings VALUES ('panel_name',  'Painel Master');
INSERT OR IGNORE INTO settings VALUES ('panel_color', '#0d6efd');
INSERT OR IGNORE INTO settings VALUES ('panel_theme', 'dark');
INSERT OR IGNORE INTO settings VALUES ('app_link',    '');
INSERT OR IGNORE INTO settings VALUES ('checkuser_url', '');
"""

def init_db():
    """Create tables and default admin if not exists."""
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()

    # Create default admin
    admin = conn.execute("SELECT id FROM panel_users WHERE role='admin' LIMIT 1").fetchone()
    if not admin:
        conn.execute(
            "INSERT INTO panel_users (username, password_hash, role, expires_at) VALUES (?,?,?,?)",
            ('admin', hash_password('admin123'), 'admin', '2099-12-31')
        )
        conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def check_password(pw: str, hashed: str) -> bool:
    return hash_password(pw) == hashed


def random_password(length: int = 10) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


def random_username(prefix: str = 'user', length: int = 6) -> str:
    suffix = ''.join(secrets.choice(string.digits + string.ascii_lowercase) for _ in range(length))
    return f"{prefix}{suffix}"


def random_uuid() -> str:
    import uuid
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = '') -> str:
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()


def get_all_settings() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}


# ---------------------------------------------------------------------------
# Panel users (admin / resellers)
# ---------------------------------------------------------------------------

def get_panel_user(user_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM panel_users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def get_panel_user_by_username(username: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM panel_users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    conn.close()
    return row


def get_resellers(parent_id: int = None, all_tree: bool = False):
    """Return direct children or full subtree of resellers."""
    conn = get_db()
    if parent_id is None:
        rows = conn.execute(
            "SELECT * FROM panel_users WHERE role='reseller' ORDER BY username"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM panel_users WHERE role='reseller' AND parent_id=? ORDER BY username",
            (parent_id,)
        ).fetchall()
    conn.close()
    return rows


def get_all_resellers_under(parent_id: int) -> list:
    """Recursively get all resellers under a given parent."""
    result = []
    direct = get_resellers(parent_id=parent_id)
    for r in direct:
        result.append(dict(r))
        result.extend(get_all_resellers_under(r['id']))
    return result


def create_panel_user(username: str, password: str, role: str, parent_id: int,
                      expires_at: str, account_limit: int) -> tuple:
    """Returns (success, message, new_id)."""
    if get_panel_user_by_username(username):
        return False, 'Usuário já existe no painel', None

    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO panel_users 
               (username, password_hash, password_plain, role, parent_id, expires_at, account_limit)
               VALUES (?,?,?,?,?,?,?)""",
            (username.lower(), hash_password(password), password, role, parent_id, expires_at, account_limit)
        )
        new_id = cur.lastrowid
        # Deduct the ACTUAL limit granted from parent's accounts_used
        # (e.g. if creating a reseller with limit=90, parent loses 90 slots)
        if parent_id:
            conn.execute(
                "UPDATE panel_users SET accounts_used = accounts_used + ? WHERE id=?",
                (account_limit, parent_id)
            )
        conn.commit()
        return True, 'Criado com sucesso', new_id
    except Exception as e:
        return False, str(e), None
    finally:
        conn.close()


def delete_panel_user(user_id: int):
    conn = get_db()
    user = conn.execute("SELECT * FROM panel_users WHERE id=?", (user_id,)).fetchone()
    if user and user['parent_id']:
        restore = user['account_limit'] if user['account_limit'] > 0 else 1
        conn.execute(
            "UPDATE panel_users SET accounts_used = MAX(0, accounts_used - ?) WHERE id=?",
            (restore, user['parent_id'])
        )
    conn.execute("DELETE FROM panel_users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def renew_panel_user(user_id: int, days: int):
    """Add days on top of current expiry for a panel user (reseller)."""
    conn = get_db()
    now = datetime.now(TZ)
    user = conn.execute("SELECT * FROM panel_users WHERE id=?", (user_id,)).fetchone()
    if user and user['expires_at']:
        try:
            current_exp = datetime.fromisoformat(user['expires_at'])
            if current_exp.tzinfo is None:
                current_exp = TZ.localize(current_exp)
            base = max(current_exp, now)
        except Exception:
            base = now
        new_exp = (base + timedelta(days=days)).strftime('%Y-%m-%d')
        conn.execute("UPDATE panel_users SET expires_at=? WHERE id=?", (new_exp, user_id))
        conn.commit()
    conn.close()


def update_panel_user(user_id: int, **kwargs):
    if not kwargs:
        return
    parts = ', '.join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    conn = get_db()
    conn.execute(f"UPDATE panel_users SET {parts} WHERE id=?", vals)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# SSH users
# ---------------------------------------------------------------------------

def ssh_user_exists(username: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT id FROM ssh_users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    conn.close()
    return row is not None


def create_ssh_user(username: str, password: str, owner_id: int, server_id: int,
                    expires_at: str, connection_limit: int,
                    v2ray_uuid: str = None, is_test: int = 0) -> tuple:
    if ssh_user_exists(username):
        return False, 'Username já existe', None

    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO ssh_users
               (username, password, owner_id, server_id, expires_at,
                connection_limit, v2ray_uuid, is_test)
               VALUES (?,?,?,?,?,?,?,?)""",
            (username.lower(), password, owner_id, server_id, expires_at,
             connection_limit, v2ray_uuid, is_test)
        )
        new_id = cur.lastrowid
        # update owner's accounts_used
        conn.execute("UPDATE panel_users SET accounts_used=accounts_used+1 WHERE id=?", (owner_id,))
        conn.commit()
        return True, 'Criado com sucesso', new_id
    except Exception as e:
        return False, str(e), None
    finally:
        conn.close()


def get_ssh_user(user_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM ssh_users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def get_ssh_user_by_username(username: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM ssh_users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    conn.close()
    return row


def get_ssh_users(owner_id: int = None, all_users: bool = False, sort: str = 'expires_at'):
    """Get SSH users. If all_users=True, ignore owner_id."""
    allowed_sorts = {'expires_at', 'created_at', 'username', 'connection_limit'}
    if sort not in allowed_sorts:
        sort = 'expires_at'
    conn = get_db()
    if all_users:
        rows = conn.execute(f"SELECT * FROM ssh_users ORDER BY {sort}").fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM ssh_users WHERE owner_id=? ORDER BY {sort}",
            (owner_id,)
        ).fetchall()
    conn.close()
    return rows


def get_ssh_users_for_tree(owner_ids: list, sort: str = 'expires_at'):
    """Get SSH users for a list of owner_ids."""
    allowed_sorts = {'expires_at', 'created_at', 'username', 'connection_limit'}
    if sort not in allowed_sorts:
        sort = 'expires_at'
    if not owner_ids:
        return []
    placeholders = ','.join('?' * len(owner_ids))
    conn = get_db()
    rows = conn.execute(
        f"SELECT * FROM ssh_users WHERE owner_id IN ({placeholders}) ORDER BY {sort}",
        owner_ids
    ).fetchall()
    conn.close()
    return rows


def delete_ssh_user(user_id: int):
    conn = get_db()
    user = conn.execute("SELECT * FROM ssh_users WHERE id=?", (user_id,)).fetchone()
    if user:
        conn.execute("UPDATE panel_users SET accounts_used=MAX(0,accounts_used-1) WHERE id=?",
                     (user['owner_id'],))
        conn.execute("DELETE FROM ssh_users WHERE id=?", (user_id,))
        conn.commit()
    conn.close()


def renew_ssh_user(user_id: int, days: int):
    """Always adds days ON TOP of current expiry (or today if already expired)."""
    conn = get_db()
    now = datetime.now(TZ)
    user = conn.execute("SELECT * FROM ssh_users WHERE id=?", (user_id,)).fetchone()
    if user:
        try:
            current_exp = datetime.fromisoformat(user['expires_at'])
            if current_exp.tzinfo is None:
                current_exp = TZ.localize(current_exp)
            # Always use max(current_expiry, today) so days are added on top
            base = max(current_exp, now)
        except Exception:
            base = now
        new_exp = base + timedelta(days=days)
        conn.execute("UPDATE ssh_users SET expires_at=? WHERE id=?",
                     (new_exp.strftime('%Y-%m-%d'), user_id))
        conn.commit()
    conn.close()


def update_ssh_user(user_id: int, **kwargs):
    if not kwargs:
        return
    parts = ', '.join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    conn = get_db()
    conn.execute(f"UPDATE ssh_users SET {parts} WHERE id=?", vals)
    conn.commit()
    conn.close()


def get_expiring_ssh_users(days: int = 7) -> list:
    """Users expiring within N days."""
    conn = get_db()
    limit_date = (datetime.now(TZ) + timedelta(days=days)).strftime('%Y-%m-%d')
    rows = conn.execute(
        "SELECT * FROM ssh_users WHERE expires_at <= ? ORDER BY expires_at",
        (limit_date,)
    ).fetchall()
    conn.close()
    return rows


def get_expired_test_users() -> list:
    """Test users that have expired (for deletion)."""
    conn = get_db()
    now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute(
        "SELECT * FROM ssh_users WHERE is_test=1 AND expires_at <= ?",
        (now_str,)
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------

def get_servers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM servers ORDER BY name").fetchall()
    conn.close()
    return rows


def get_server(server_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    return row


def add_server(name: str, ip: str, module_port: int, root_user: str,
               root_password: str, auth_token: str) -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO servers (name, ip, module_port, root_user, root_password, auth_token)
           VALUES (?,?,?,?,?,?)""",
        (name, ip, module_port, root_user, root_password, auth_token)
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


def delete_server(server_id: int):
    conn = get_db()
    conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def add_payment(ssh_user_id: int, owner_id: int, amount: float,
                payment_id: str, payer_email: str = '', status: str = 'pending') -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO payments (ssh_user_id, owner_id, amount, payment_id, payer_email, status)
           VALUES (?,?,?,?,?,?)""",
        (ssh_user_id, owner_id, amount, payment_id, payer_email, status)
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


def get_payments(owner_id: int = None):
    conn = get_db()
    if owner_id is None:
        rows = conn.execute("SELECT p.*, u.username as ssh_username FROM payments p LEFT JOIN ssh_users u ON p.ssh_user_id=u.id ORDER BY p.created_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT p.*, u.username as ssh_username FROM payments p LEFT JOIN ssh_users u ON p.ssh_user_id=u.id WHERE p.owner_id=? ORDER BY p.created_at DESC", (owner_id,)).fetchall()
    conn.close()
    return rows


def update_payment_status(payment_id: str, status: str):
    conn = get_db()
    conn.execute("UPDATE payments SET status=? WHERE payment_id=?", (status, payment_id))
    conn.commit()
    conn.close()


def delete_payment(payment_db_id: int):
    conn = get_db()
    conn.execute("DELETE FROM payments WHERE id=?", (payment_db_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Backup config
# ---------------------------------------------------------------------------

def get_backup_config():
    conn = get_db()
    row = conn.execute("SELECT * FROM backup_config WHERE id=1").fetchone()
    conn.close()
    return row


def set_backup_config(bot_token: str, chat_id: str, enabled: int):
    conn = get_db()
    conn.execute(
        """UPDATE backup_config SET telegram_bot_token=?, telegram_chat_id=?, enabled=?
           WHERE id=1""",
        (bot_token, chat_id, enabled)
    )
    conn.commit()
    conn.close()


def update_last_backup(dt: str):
    conn = get_db()
    conn.execute("UPDATE backup_config SET last_backup=? WHERE id=1", (dt,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Reseller server assignments
# ---------------------------------------------------------------------------

def get_reseller_servers(reseller_id: int) -> list:
    """Return list of server_ids assigned to a reseller."""
    conn = get_db()
    rows = conn.execute(
        "SELECT server_id FROM reseller_servers WHERE reseller_id=?",
        (reseller_id,)
    ).fetchall()
    conn.close()
    return [r['server_id'] for r in rows]


def set_reseller_servers(reseller_id: int, server_ids: list):
    """Replace all server assignments for a reseller."""
    conn = get_db()
    conn.execute("DELETE FROM reseller_servers WHERE reseller_id=?", (reseller_id,))
    for sid in server_ids:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO reseller_servers (reseller_id, server_id) VALUES (?,?)",
                (reseller_id, sid)
            )
        except Exception:
            pass
    conn.commit()
    conn.close()


def get_servers_for_user(panel_user) -> list:
    """Return servers available to a panel user (admin=all, reseller=assigned)."""
    conn = get_db()
    if panel_user['role'] == 'admin':
        rows = conn.execute("SELECT * FROM servers ORDER BY name").fetchall()
        conn.close()
        return rows
    
    server_ids = get_reseller_servers(panel_user['id'])
    if not server_ids:
        # walk up hierarchy
        parent_id = panel_user['parent_id']
        while parent_id:
            server_ids = get_reseller_servers(parent_id)
            if server_ids:
                break
            parent = conn.execute("SELECT parent_id FROM panel_users WHERE id=?", (parent_id,)).fetchone()
            parent_id = parent['parent_id'] if parent else None
    
    if not server_ids:
        conn.close()
        return []
    
    placeholders = ','.join('?' * len(server_ids))
    rows = conn.execute(
        f"SELECT * FROM servers WHERE id IN ({placeholders}) ORDER BY name",
        server_ids
    ).fetchall()
    conn.close()
    return rows


def migrate_schema():
    """Run schema migrations for existing databases (adds columns if missing)."""
    conn = get_db()
    # Add password_plain if missing
    try:
        conn.execute("ALTER TABLE panel_users ADD COLUMN password_plain TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    # Create reseller_servers if missing
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reseller_servers (
            reseller_id INTEGER NOT NULL,
            server_id   INTEGER NOT NULL,
            PRIMARY KEY (reseller_id, server_id)
        );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Server categories
# ---------------------------------------------------------------------------

def get_server_categories():
    conn = get_db()
    rows = conn.execute("SELECT * FROM server_categories ORDER BY name").fetchall()
    conn.close()
    return rows

def add_server_category(name: str) -> int:
    conn = get_db()
    cur = conn.execute("INSERT INTO server_categories (name) VALUES (?)", (name,))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id

def delete_server_category(cat_id: int):
    conn = get_db()
    conn.execute("DELETE FROM server_categories WHERE id=?", (cat_id,))
    conn.commit()
    conn.close()

def get_servers_by_category(cat_id: int) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM servers WHERE category_id=? AND status='active'", (cat_id,)).fetchall()
    conn.close()
    return rows

def update_server(server_id: int, **kwargs):
    if not kwargs:
        return
    allowed = {'name', 'ip', 'module_port', 'root_user', 'root_password', 'auth_token', 'status', 'category_id'}
    kwargs = {k: v for k, v in kwargs.items() if k in allowed}
    if not kwargs:
        return
    parts = ', '.join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [server_id]
    conn = get_db()
    conn.execute(f"UPDATE servers SET {parts} WHERE id=?", vals)
    conn.commit()
    conn.close()

def migrate_schema_v2():
    """Additional schema migrations."""
    conn = get_db()
    for stmt in [
        "CREATE TABLE IF NOT EXISTS server_categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT DEFAULT (datetime('now')))",
        "ALTER TABLE servers ADD COLUMN category_id INTEGER REFERENCES server_categories(id) ON DELETE SET NULL",
    ]:
        try:
            conn.executescript(stmt)
            conn.commit()
        except Exception:
            pass
    conn.close()
