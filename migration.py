#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migration.py — Import from Dragon Core/Atlas or GestorSSH MySQL dump into Painel Master (SQLite)

Dragon Core / Atlas tables parsed:
  accounts     → panel_users  (resellers, with hierarchy)
  atribuidos   → panel_users  (limits + expiry)
  ssh_accounts → ssh_users    (SSH users)
  categorias   → informational only

GestorSSH tables parsed:
  accounts     → panel_users  (resellers, with hierarchy)
  atribuidos   → panel_users  (limits + expiry per reseller)
  categorias   → server_categories + informational
  servidores   → servers      (IP + credentials imported; module port/token need setup)
  ssh_accounts → ssh_users    (SSH users, linked to imported servers)
"""

import re
import db
import server_comm as sc


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _int(val) -> int:
    try:
        return int(str(val).strip())
    except Exception:
        return 0


def _clean(val) -> str:
    if val is None:
        return ''
    s = str(val).strip()
    return '' if s.upper() == 'NULL' else s


# ─────────────────────────────────────────────────────────────────────────────
# Dragon Core / Atlas extraction  (single-quoted, backtick table names)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_insert_values(sql: str, table: str) -> list:
    """Extract all rows from INSERT INTO `table` VALUES (...) statements."""
    pattern = rf"INSERT INTO `{table}` VALUES\s*(.*?);\s*(?:UNLOCK|/\*)"
    rows = []
    for match in re.finditer(pattern, sql, re.DOTALL | re.IGNORECASE):
        block = match.group(1).strip()
        for row_match in re.finditer(r'\(([^()]*(?:\([^()]*\)[^()]*)*)\)', block):
            raw = row_match.group(1)
            rows.append(_parse_row(raw))
    return rows


def _parse_row(raw: str) -> list:
    """Parse a CSV-like MySQL row (single-quoted) into a Python list."""
    fields = []
    i = 0
    current = ''
    while i < len(raw):
        c = raw[i]
        if c == "'":
            i += 1
            s = ''
            while i < len(raw):
                if raw[i] == '\\' and i + 1 < len(raw):
                    esc = raw[i + 1]
                    s += {'n': '\n', 't': '\t', 'r': '\r', "'": "'", '\\': '\\'}.get(esc, esc)
                    i += 2
                elif raw[i] == "'":
                    i += 1
                    break
                else:
                    s += raw[i]
                    i += 1
            current = s
        elif c == ',':
            fields.append(None if current is None or str(current).upper() == 'NULL' else current)
            current = ''
            i += 1
        elif c in (' ', '\n', '\r', '\t'):
            i += 1
        else:
            j = i
            while j < len(raw) and raw[j] not in (',',):
                j += 1
            val = raw[i:j].strip()
            current = None if val.upper() == 'NULL' else val
            i = j
    fields.append(None if current is None or str(current).upper() == 'NULL' else current)
    return fields


# ─────────────────────────────────────────────────────────────────────────────
# GestorSSH extraction  (double-quoted, no backticks, one INSERT per line)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_gestor_values(sql: str, table: str) -> list:
    """Extract rows from GestorSSH-format INSERT statements (double-quoted values)."""
    rows = []
    pattern = re.compile(
        rf'INSERT INTO `?{re.escape(table)}`? VALUES\s*\((.*?)\)\s*;',
        re.IGNORECASE
    )
    for match in pattern.finditer(sql):
        rows.append(_parse_gestor_row(match.group(1)))
    return rows


def _parse_gestor_row(raw: str) -> list:
    """Parse a GestorSSH VALUES row (double-quoted strings)."""
    fields = []
    i = 0
    current = ''
    while i < len(raw):
        c = raw[i]
        if c == '"':
            i += 1
            s = ''
            while i < len(raw):
                if raw[i] == '\\' and i + 1 < len(raw):
                    esc = raw[i + 1]
                    s += {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\'}.get(esc, esc)
                    i += 2
                elif raw[i] == '"':
                    i += 1
                    break
                else:
                    s += raw[i]
                    i += 1
            current = s
        elif c == ',':
            fields.append(None if str(current).upper() == 'NULL' else current)
            current = ''
            i += 1
        elif c in (' ', '\n', '\r', '\t'):
            i += 1
        else:
            j = i
            while j < len(raw) and raw[j] != ',':
                j += 1
            val = raw[i:j].strip()
            current = None if val.upper() == 'NULL' else val
            i = j
    fields.append(None if str(current).upper() == 'NULL' else current)
    return fields


# ─────────────────────────────────────────────────────────────────────────────
# Auto-detect source panel
# ─────────────────────────────────────────────────────────────────────────────

def detect_source(sql_content: str) -> str:
    """Return 'gestor' or 'dragon' based on SQL content."""
    if 'expiraadm' in sql_content or 'situacao' in sql_content:
        return 'gestor'
    return 'dragon'


# ─────────────────────────────────────────────────────────────────────────────
# Dragon Core / Atlas migration
# ─────────────────────────────────────────────────────────────────────────────

def migrate_dragon_core(sql_content: str) -> dict:
    """
    Parse MySQL dump from Dragon Core/Atlas and import into Painel Master.
    Returns dict with counts of imported records.
    """

    result = {'resellers': 0, 'ssh_users': 0, 'skipped': 0, 'errors': []}

    # ── 1. Parse accounts (panel users / resellers) ────────────────────────
    accounts_raw = _extract_insert_values(sql_content, 'accounts')
    accounts_map = {}
    for row in accounts_raw:
        if len(row) < 9:
            continue
        acc_id  = _int(row[0])
        login   = str(row[3] or '').strip()
        senha   = str(row[6] or '').strip()
        byid    = _int(row[7])
        accounts_map[acc_id] = {
            'id': acc_id, 'login': login, 'senha': senha, 'byid': byid
        }

    # ── 2. Parse atribuidos (limits + expiry per account) ─────────────────
    atrib_raw = _extract_insert_values(sql_content, 'atribuidos')
    atrib_map = {}
    for row in atrib_raw:
        if len(row) < 11:
            continue
        userid   = _int(row[3])
        limite   = _int(row[5]) or 10
        expira   = str(row[8] or '').strip()
        suspenso = _int(row[10])
        atrib_map[userid] = {
            'limite': limite,
            'expira': expira[:10] if expira else '2099-12-31',
            'suspenso': suspenso
        }

    # ── 3. Get admin user id in Painel Master ──────────────────────────────
    admin = db.get_panel_user_by_username('admin')
    admin_id = admin['id'] if admin else 1
    dragon_to_painel_id = {1: admin_id}

    # ── 4. Import resellers (multi-pass for deep hierarchy) ────────────────
    remaining = {k: v for k, v in accounts_map.items() if k != 1}
    for _pass in range(10):
        if not remaining:
            break
        made_progress = False
        for acc_id, acc in list(remaining.items()):
            byid = acc['byid']
            if byid == 0 or byid == 1:
                parent_painel_id = admin_id
            elif byid in dragon_to_painel_id:
                parent_painel_id = dragon_to_painel_id[byid]
            else:
                continue

            login = acc['login']
            if not login:
                del remaining[acc_id]
                continue

            existing = db.get_panel_user_by_username(login)
            if existing:
                dragon_to_painel_id[acc_id] = existing['id']
                del remaining[acc_id]
                made_progress = True
                continue

            atr        = atrib_map.get(acc_id, {})
            expires_at = atr.get('expira', '2099-12-31')
            account_limit = atr.get('limite', 10)
            status     = 'suspended' if atr.get('suspenso', 0) else 'active'
            password   = acc['senha'] or 'senha123'

            ok, msg, new_id = db.create_panel_user(
                login, password, 'reseller', parent_painel_id,
                expires_at, account_limit
            )
            if ok and new_id:
                if status == 'suspended':
                    db.update_panel_user(new_id, status='suspended')
                db.update_panel_user(new_id, password_plain=password)
                dragon_to_painel_id[acc_id] = new_id
                result['resellers'] += 1
            else:
                result['errors'].append(f"Reseller {login}: {msg}")

            del remaining[acc_id]
            made_progress = True

        if not made_progress:
            for acc_id, acc in remaining.items():
                result['errors'].append(f"Ignorado (pai órfão): {acc['login']}")
            break

    # ── 5. Parse ssh_accounts ──────────────────────────────────────────────
    ssh_raw = _extract_insert_values(sql_content, 'ssh_accounts')

    for row in ssh_raw:
        if len(row) < 9:
            continue
        byid   = _int(row[1])
        limite = _int(row[3]) or 1
        login  = str(row[5] or '').strip()
        senha  = str(row[6] or '').strip()
        expira = str(row[8] or '').strip()
        status = str(row[10] or '').strip()
        uuid   = str(row[14] or '').strip() if len(row) > 14 else ''
        uuid   = uuid if (uuid and uuid not in ('', 'NULL', '0')) else None

        if not login:
            result['skipped'] += 1
            continue

        if expira and expira not in ('', 'NULL', 'Suspenso', '0'):
            expires_at = expira[:10]
        else:
            expires_at = '2026-12-31'

        owner_painel_id = dragon_to_painel_id.get(byid, admin_id)
        is_suspended    = (str(status).lower() == 'suspenso')

        if db.ssh_user_exists(login):
            result['skipped'] += 1
            continue

        ok, msg, new_id = db.create_ssh_user(
            login, senha or '123456',
            owner_painel_id, None,
            expires_at, limite, uuid, 0
        )
        if ok:
            if is_suspended:
                db.update_ssh_user(new_id, status='suspended')
            result['ssh_users'] += 1
        else:
            result['skipped'] += 1

    db.recalculate_accounts_used()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GestorSSH migration
# ─────────────────────────────────────────────────────────────────────────────

def migrate_gestor_ssh(sql_content: str) -> dict:
    """
    Parse MySQL dump from GestorSSH and import into Painel Master.
    Returns dict with counts of imported records.

    GestorSSH column layout
    -----------------------
    accounts   : id(0) nome(1) contato(2) login(3) token(4) mb(5) senha(6) byid(7)
                 ... qtdrevenda(14) ... expiraadm(25) block(26)
    atribuidos : id(0) categoriaid(1) userid(2) byid(3) limite(4) tipo(5)
                 expira(6) suspenso(7) ...
    categorias : id(0) subid(1) nome(2)
    servidores : id(0) subid(1) nome(2) porta(3) usuario(4) senha(5) ip(6) ...
    ssh_accounts: id(0) byid(1) categoriaid(2) limite(3) bycredit(4) login(5)
                  senha(6) situacao(7) expira(8) ... uuid(14) tester(15)
    """

    result = {'resellers': 0, 'ssh_users': 0, 'servers': 0, 'skipped': 0, 'errors': []}

    # ── 1. Categorias → server_categories ─────────────────────────────────
    cat_raw = _extract_gestor_values(sql_content, 'categorias')
    cat_map = {}          # gestor cat_id → nome
    cat_to_panel_cat = {} # gestor cat_id → panel server_category id

    for row in cat_raw:
        if len(row) < 3:
            continue
        cat_id   = _int(row[0])
        cat_nome = _clean(row[2]) or f'Categoria {cat_id}'
        cat_map[cat_id] = cat_nome

    # Create server categories in panel
    for cat_id, cat_nome in cat_map.items():
        # Check if already exists
        conn = db.get_db()
        existing_cat = conn.execute(
            "SELECT id FROM server_categories WHERE name=?", (cat_nome,)
        ).fetchone()
        conn.close()
        if existing_cat:
            cat_to_panel_cat[cat_id] = existing_cat['id']
        else:
            new_cat_id = db.add_server_category(cat_nome)
            cat_to_panel_cat[cat_id] = new_cat_id

    # ── 2. Servidores → servers ────────────────────────────────────────────
    srv_raw = _extract_gestor_values(sql_content, 'servidores')
    # subid(1) = categoria id in GestorSSH
    cat_to_panel_server = {}  # gestor cat_id → panel server_id

    for row in srv_raw:
        if len(row) < 7:
            continue
        g_cat_id   = _int(row[1])   # subid = categoria
        srv_nome   = _clean(row[2]) or cat_map.get(g_cat_id, 'Servidor')
        srv_ip     = _clean(row[6])
        srv_user   = _clean(row[4]) or 'root'
        srv_pass   = _clean(row[5])

        if not srv_ip:
            continue

        # Avoid duplicate IPs
        conn = db.get_db()
        existing_srv = conn.execute(
            "SELECT id FROM servers WHERE ip=?", (srv_ip,)
        ).fetchone()
        conn.close()

        if existing_srv:
            panel_srv_id = existing_srv['id']
        else:
            panel_srv_id = db.add_server(
                srv_nome, srv_ip,
                7270,        # default module port — admin configures after import
                srv_user, srv_pass,
                db.random_password(22)  # placeholder auth_token
            )
            result['servers'] += 1

        # Link server to its category in the panel
        panel_cat_id = cat_to_panel_cat.get(g_cat_id)
        if panel_cat_id:
            db.update_server(panel_srv_id, category_id=panel_cat_id)

        # Map: gestor category → first panel server found in that category
        if g_cat_id not in cat_to_panel_server:
            cat_to_panel_server[g_cat_id] = panel_srv_id

    # ── 3. Accounts (panel users / resellers) ─────────────────────────────
    accounts_raw = _extract_gestor_values(sql_content, 'accounts')
    accounts_map = {}

    for row in accounts_raw:
        if len(row) < 8:
            continue
        acc_id  = _int(row[0])
        login   = _clean(row[3])
        senha   = _clean(row[6])
        byid    = _int(row[7])
        qtd     = _int(row[14]) if len(row) > 14 else 10
        expira  = _clean(row[25]) if len(row) > 25 else ''
        block   = _int(row[26])  if len(row) > 26 else 0

        accounts_map[acc_id] = {
            'id': acc_id, 'login': login, 'senha': senha,
            'byid': byid, 'qtdrevenda': qtd or 10,
            'expiraadm': expira[:10] if expira else '2099-12-31',
            'block': block,
        }

    # ── 4. Atribuidos (limits + expiry per reseller) ───────────────────────
    atrib_raw = _extract_gestor_values(sql_content, 'atribuidos')
    atrib_map = {}  # userid → {limite, expira, suspenso}

    for row in atrib_raw:
        if len(row) < 8:
            continue
        userid   = _int(row[2])
        limite   = _int(row[4]) or 10
        expira   = _clean(row[6])
        suspenso = _int(row[7])
        # Keep entry with the latest expiry if multiple entries per user
        existing = atrib_map.get(userid)
        if existing is None or expira > existing['expira']:
            atrib_map[userid] = {
                'limite':   limite,
                'expira':   expira[:10] if expira else '2099-12-31',
                'suspenso': suspenso,
            }

    # ── 5. Admin user resolution ───────────────────────────────────────────
    admin = db.get_panel_user_by_username('admin')
    admin_id = admin['id'] if admin else 1
    # GestorSSH: byid='0' = admin account (id=1)
    gestor_to_panel = {1: admin_id}

    # ── 6. Import resellers (multi-pass for hierarchy) ─────────────────────
    # Skip id=1 (admin) and accounts with byid=0 (also admin-level)
    remaining = {
        k: v for k, v in accounts_map.items()
        if k != 1 and v['byid'] != 0
    }

    for _pass in range(10):
        if not remaining:
            break
        made_progress = False
        for acc_id, acc in list(remaining.items()):
            byid = acc['byid']
            if byid in (0, 1):
                parent_panel_id = admin_id
            elif byid in gestor_to_panel:
                parent_panel_id = gestor_to_panel[byid]
            else:
                continue  # parent not yet imported, retry next pass

            login = acc['login']
            if not login:
                del remaining[acc_id]
                continue

            existing = db.get_panel_user_by_username(login)
            if existing:
                gestor_to_panel[acc_id] = existing['id']
                del remaining[acc_id]
                made_progress = True
                continue

            atr        = atrib_map.get(acc_id, {})
            expires_at = atr.get('expira') or acc['expiraadm'] or '2099-12-31'
            acc_limit  = atr.get('limite') or acc['qtdrevenda'] or 10
            is_susp    = bool(atr.get('suspenso', 0) or acc['block'])
            password   = acc['senha'] or 'senha123'

            ok, msg, new_id = db.create_panel_user(
                login, password, 'reseller', parent_panel_id,
                expires_at, acc_limit
            )
            if ok and new_id:
                if is_susp:
                    db.update_panel_user(new_id, status='suspended')
                db.update_panel_user(new_id, password_plain=password)
                gestor_to_panel[acc_id] = new_id
                result['resellers'] += 1
            else:
                result['errors'].append(f"Revenda {login}: {msg}")

            del remaining[acc_id]
            made_progress = True

        if not made_progress:
            for acc_id, acc in remaining.items():
                result['errors'].append(f"Ignorado (pai órfão): {acc['login']}")
            break

    # ── 7. Import ssh_accounts ──────────────────────────────────────────────
    ssh_raw = _extract_gestor_values(sql_content, 'ssh_accounts')

    # usuarios importados por categoria do painel — usados depois para
    # cadastra-los nos servidores (existentes + novos da mesma categoria)
    cat_imported = {}

    for row in ssh_raw:
        if len(row) < 9:
            continue
        byid     = _int(row[1])
        cat_id   = _int(row[2])
        limite   = _int(row[3]) or 1
        login    = _clean(row[5])
        senha    = _clean(row[6])
        situacao = _clean(row[7])
        expira   = _clean(row[8])
        uuid     = _clean(row[14]) if len(row) > 14 else ''
        uuid     = uuid if uuid and uuid.lower() not in ('', 'null', '0') else None
        is_test  = _int(row[15]) if len(row) > 15 else 0

        if not login:
            result['skipped'] += 1
            continue

        expires_at = expira[:10] if expira and expira not in ('', '0') else '2026-12-31'
        owner_id   = gestor_to_panel.get(byid, admin_id)
        server_id  = cat_to_panel_server.get(cat_id)

        # situacao: Ativo | Online | Limite Ultrapassado = active
        #           Suspenso                             = suspended
        is_suspended = situacao.lower() == 'suspenso'

        if db.ssh_user_exists(login):
            result['skipped'] += 1
            continue

        ok, msg, new_id = db.create_ssh_user(
            login, senha or '123456',
            owner_id, server_id,
            expires_at, limite, uuid, is_test
        )
        if ok:
            if is_suspended:
                db.update_ssh_user(new_id, status='suspended')
            result['ssh_users'] += 1

            # Vincula o usuario a TODOS os servidores da categoria (primario
            # incluso). Se o mapeamento de servidor nao veio na origem, usa o
            # primeiro servidor da categoria como primario (evita server_id NULL).
            panel_cat_id = cat_to_panel_cat.get(cat_id)
            if panel_cat_id:
                cat_servers = db.get_servers_by_category(panel_cat_id)
                if cat_servers:
                    if not server_id:
                        db.update_ssh_user(new_id, server_id=cat_servers[0]['id'])
                    for srv in cat_servers:
                        db.add_user_extra_server(new_id, srv['id'])
                    cat_imported.setdefault(panel_cat_id, []).append({
                        'id': new_id, 'username': login, 'password': senha or '123456',
                        'expires_at': expires_at, 'connection_limit': limite,
                        'v2ray_uuid': uuid,
                        'status': 'suspended' if is_suspended else 'active',
                    })
                else:
                    result['errors'].append(f"[{login}] categoria sem servidor ativo")
        else:
            result['skipped'] += 1

    # ── 8. Cadastra os usuarios migrados nos servidores das categorias ────
    # Para cada servidor do painel (ja existente OU importado agora nesta
    # migracao), envia os usuarios migrados da categoria dele. O sync pula
    # quem ja existe na VPS e cria apenas quem falta — assim usuários novos
    # numa categoria também são incluídos automaticamente em servidores novos.
    server_sync = {}
    for srv in db.get_all_servers():
        if not srv['category_id']:
            continue
        users = cat_imported.get(srv['category_id'], [])
        if not users:
            continue
        try:
            ok, msg = sc.sync_users_to_server(
                srv['ip'], srv['module_port'], srv['auth_token'],
                users, force=False
            )
            server_sync[srv['name']] = 'OK' if ok else 'FALHA'
            if not ok:
                result['errors'].append(
                    f"[{srv['name']}] cadastro de usuarios: {str(msg)[:120]}")
        except Exception as e:
            server_sync[srv['name']] = 'FALHA'
            result['errors'].append(
                f"[{srv['name']}] cadastro de usuarios: {str(e)[:120]}")
    result['server_sync'] = server_sync

    # Recalculate all account limits from real data
    db.recalculate_accounts_used()
    return result
