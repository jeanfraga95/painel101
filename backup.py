#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backup.py — Telegram backup and restore for Painel Master
"""

import os
import shutil
import sqlite3
import tarfile
import tempfile
import requests
from datetime import datetime
import pytz

DB_PATH = os.path.join(os.path.dirname(__file__), 'painel.db')
TZ = pytz.timezone('America/Sao_Paulo')


def create_backup_archive() -> str:
    """Create a .tar.gz of the entire panel data. Returns path to archive."""
    now = datetime.now(TZ).strftime('%Y%m%d_%H%M%S')
    tmp_dir = tempfile.mkdtemp()
    archive_path = os.path.join(tmp_dir, f'painel_backup_{now}.tar.gz')

    base_dir = os.path.dirname(os.path.abspath(__file__))

    with tarfile.open(archive_path, 'w:gz') as tar:
        # Database
        if os.path.exists(DB_PATH):
            tar.add(DB_PATH, arcname='painel.db')
        # Static uploads if any
        static_dir = os.path.join(base_dir, 'static')
        if os.path.exists(static_dir):
            tar.add(static_dir, arcname='static')

    return archive_path


def send_backup_telegram(bot_token: str, chat_id: str) -> tuple:
    """Send backup to Telegram. Returns (success, message)."""
    try:
        archive_path = create_backup_archive()
        now = datetime.now(TZ).strftime('%d/%m/%Y %H:%M:%S')

        with open(archive_path, 'rb') as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendDocument",
                data={
                    'chat_id': chat_id,
                    'caption': f'🗄️ *Backup Painel Master*\n📅 {now}',
                    'parse_mode': 'Markdown'
                },
                files={'document': (os.path.basename(archive_path), f, 'application/gzip')},
                timeout=60
            )

        os.unlink(archive_path)
        os.rmdir(os.path.dirname(archive_path))

        if resp.status_code == 200:
            return True, 'Backup enviado com sucesso'
        return False, f"Telegram error: {resp.text}"
    except Exception as e:
        return False, str(e)


def restore_backup(archive_file) -> tuple:
    """Restore from an uploaded .tar.gz archive."""
    try:
        tmp_dir = tempfile.mkdtemp()
        archive_path = os.path.join(tmp_dir, 'restore.tar.gz')

        if hasattr(archive_file, 'save'):
            archive_file.save(archive_path)
        else:
            with open(archive_path, 'wb') as f:
                f.write(archive_file.read())

        base_dir = os.path.dirname(os.path.abspath(__file__))

        with tarfile.open(archive_path, 'r:gz') as tar:
            tar.extractall(tmp_dir)

        # Restore database
        restored_db = os.path.join(tmp_dir, 'painel.db')
        if os.path.exists(restored_db):
            # Validate SQLite
            try:
                conn = sqlite3.connect(restored_db)
                conn.execute("SELECT count(*) FROM sqlite_master")
                conn.close()
            except Exception:
                shutil.rmtree(tmp_dir)
                return False, 'Arquivo de banco de dados inválido'

            shutil.copy2(restored_db, DB_PATH)

        shutil.rmtree(tmp_dir)
        return True, 'Backup restaurado com sucesso. Reinicie o painel.'
    except Exception as e:
        return False, str(e)
