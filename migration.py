#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migration.py — Import Dragon Core / Atlas MySQL dump into Painel Master (SQLite)

Tables parsed:
  accounts        → panel_users  (resellers/sub-resellers)
  atribuidos      → panel_users  (limits + expiry)
  ssh_accounts    → ssh_users    (SSH user accounts)
  categorias      → servers      (server references, informational only)
"""

import re
import db


def _extract_insert_values(sql: str, table: str) -> list:
    """Extract all rows from INSERT INTO `table` VALUES (...) statements."""
    pattern = rf"INSERT INTO `{table}` VALUES\s*(.*?);\s*(?:UNLOCK|/\*)"
    rows = []
    for match in re.finditer(pattern, sql, re.DOTALL | re.IGNORECASE):
        block = match.group(1).strip()
        # Split on ),( boundaries carefully
        for row_match in re.finditer(r'\(([^()]*(?:\([^()]*\)[^()]*)*)\)', block):
            raw = row_match.group(1)
            rows.append(_parse_row(raw))
    return rows


def _parse_row(raw: str) -> list:
    """Parse a CSV-like MySQL row into a Python list, handling quoted strings."""
    fields = []
    i = 0
    current = ''
    while i < len(raw):
        c = raw[i]
        if c == "'" :
            # read until closing quote (handling \' escapes)
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
        elif c == ',' :
            fields.append(None if current is None or str(current).upper() == 'NULL' else current)
            current = ''
            i += 1
        elif c in (' ', '\n', '\r', '\t'):
            i += 1
        else:
            # unquoted value (number, NULL, etc.)
            j = i
            while j < len(raw) and raw[j] not in (',',):
                j += 1
            val = raw[i:j].strip()
            current = None if val.upper() == 'NULL' else val
            i = j
    fields.append(None if current is None or str(current).upper() == 'NULL' else current)
    return fields


def migrate_dragon_core(sql_content: str) -> dict:
    """
    Parse MySQL dump from Dragon Core/Atlas and import into Painel Master.
    Returns dict with counts of imported records.
    """

    result = {'resellers': 0, 'ssh_users': 0, 'skipped': 0, 'errors': []}

    # ── 1. Parse accounts (panel users / resellers) ────────────────────────
    # accounts: id, nome, contato, login, token, mb, senha, byid, mainid, ...
    accounts_raw = _extract_insert_values(sql_content, 'accounts')
    # Build id→row map
    accounts_map = {}
    for row in accounts_raw:
        if len(row) < 9:
            continue
        acc_id   = _int(row[0])
        # login = row[3], senha = row[6], byid = row[7]
        login    = str(row[3] or '').strip()
        senha    = str(row[6] or '').strip()
        byid     = _int(row[7])   # parent account id (0 = root/admin)
        accounts_map[acc_id] = {
            'id': acc_id, 'login': login, 'senha': senha, 'byid': byid
        }

    # ── 2. Parse atribuidos (limits + expiry per account) ─────────────────
    # atribuidos: id, valor, categoriaid, userid, byid, limite, limitetest, tipo, expira, subrev, suspenso, ...
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

    # id=1 in Dragon Core is root admin — map to our admin
    dragon_to_painel_id = {1: admin_id}

    # ── 4. Import resellers (accounts with byid != 0, i.e. not root) ──────
    # First pass: direct children of root (byid=1 or byid=0)
    # We do multiple passes to handle deep hierarchies

    remaining = {k: v for k, v in accounts_map.items() if k != 1}
    max_passes = 10

    for _pass in range(max_passes):
        if not remaining:
            break
        made_progress = False
        for acc_id, acc in list(remaining.items()):
            byid = acc['byid']
            # Resolve parent
            if byid == 0 or byid == 1:
                parent_painel_id = admin_id
            elif byid in dragon_to_painel_id:
                parent_painel_id = dragon_to_painel_id[byid]
            else:
                continue  # parent not imported yet

            login = acc['login']
            if not login:
                del remaining[acc_id]
                continue

            # Skip if already exists
            existing = db.get_panel_user_by_username(login)
            if existing:
                dragon_to_painel_id[acc_id] = existing['id']
                del remaining[acc_id]
                made_progress = True
                continue

            atr = atrib_map.get(acc_id, {})
            expires_at = atr.get('expira', '2099-12-31')
            account_limit = atr.get('limite', 10)
            status = 'suspended' if atr.get('suspenso', 0) else 'active'

            password = acc['senha'] or 'senha123'
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
            # Give up on remaining
            for acc_id, acc in remaining.items():
                result['errors'].append(f"Skipped (orphan parent): {acc['login']}")
            break

    # ── 5. Parse ssh_accounts ──────────────────────────────────────────────
    # ssh_accounts: id, byid, categoriaid, limite, bycredit, login, senha, mainid,
    #               expira, lastview, status, valormensal, notificado, whatsapp, uuid, deviceid, deviceativo
    ssh_raw = _extract_insert_values(sql_content, 'ssh_accounts')

    for row in ssh_raw:
        if len(row) < 9:
            continue
        byid    = _int(row[1])
        limite  = _int(row[3]) or 1
        login   = str(row[5] or '').strip()
        senha   = str(row[6] or '').strip()
        expira  = str(row[8] or '').strip()
        status  = str(row[10] or '').strip()
        uuid    = str(row[14] or '').strip() if len(row) > 14 else ''
        uuid    = uuid if (uuid and uuid not in ('', 'NULL', '0')) else None

        if not login:
            result['skipped'] += 1
            continue

        # Normalize expiry
        if expira and expira not in ('', 'NULL', 'Suspenso', '0'):
            expires_at = expira[:10]
        else:
            expires_at = '2026-12-31'

        # Map owner
        owner_painel_id = dragon_to_painel_id.get(byid, admin_id)

        is_suspended = (str(status).lower() == 'suspenso')

        if db.ssh_user_exists(login):
            result['skipped'] += 1
            continue

        ok, msg, new_id = db.create_ssh_user(
            login, senha or '123456',
            owner_painel_id, None,
            expires_at, limite,
            uuid, 0
        )
        if ok:
            if is_suspended:
                db.update_ssh_user(new_id, status='suspended')
            result['ssh_users'] += 1
        else:
            result['skipped'] += 1

    return result


def _int(val) -> int:
    try:
        return int(str(val).strip())
    except Exception:
        return 0
