#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backup.py — Telegram backup and restore for Painel Master

O arquivo de backup é CIFRADO (Fernet/AES) antes de sair do servidor: o
painel.db contém dados sensíveis (senhas de usuários SSH cifradas e hashes
de senha do painel) e trafegar em texto puro para o Telegram expõe tudo.
"""

import os
import shutil
import sqlite3
import tarfile
import tempfile
import requests
from datetime import datetime
import pytz

import db

DB_PATH = os.path.join(os.path.dirname(__file__), 'painel.db')
TZ = pytz.timezone('America/Sao_Paulo')
ENC_SUFFIX = '.enc'


def _encrypt_file(src: str) -> str:
    """Cifra um arquivo e devolve o caminho do arquivo cifrado."""
    dest = src + ENC_SUFFIX
    with open(src, 'rb') as f:
        data = f.read()
    token = db._fernet().encrypt(data)
    with open(dest, 'wb') as f:
        f.write(token)
    os.unlink(src)
    return dest


def _decrypt_file(src: str, dest: str) -> bool:
    """Desfaz a cifragem. Devolve False se o arquivo não for cifrado."""
    with open(src, 'rb') as f:
        data = f.read()
    try:
        plain = db._fernet().decrypt(data)
    except Exception:
        return False
    with open(dest, 'wb') as f:
        f.write(plain)
    return True


def create_backup_archive() -> str:
    """Create an encrypted .tar.gz of the entire panel data. Returns path."""
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

    return _encrypt_file(archive_path)


def send_backup_telegram(bot_token: str, chat_id: str) -> tuple:
    """Send (encrypted) backup to Telegram. Returns (success, message)."""
    try:
        archive_path = create_backup_archive()
        now = datetime.now(TZ).strftime('%d/%m/%Y %H:%M:%S')

        with open(archive_path, 'rb') as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendDocument",
                data={
                    'chat_id': chat_id,
                    'caption': (f'🗄️ *Backup Painel Master* (criptografado)\n'
                                f'📅 {now}\n'
                                f'🔐 Senha de descriptografia: a mesma do painel.'),
                    'parse_mode': 'Markdown'
                },
                files={'document': (os.path.basename(archive_path), f, 'application/octet-stream')},
                timeout=60
            )

        os.unlink(archive_path)
        os.rmdir(os.path.dirname(archive_path))

        if resp.status_code == 200:
            return True, 'Backup enviado com sucesso (criptografado)'
        return False, f"Telegram error: {resp.text}"
    except Exception as e:
        return False, str(e)


def restore_backup(archive_file) -> tuple:
    """Restore from an uploaded archive (cifrado .enc ou .tar.gz legado)."""
    try:
        tmp_dir = tempfile.mkdtemp()
        raw_path = os.path.join(tmp_dir, 'upload.bin')
        archive_path = os.path.join(tmp_dir, 'restore.tar.gz')

        if hasattr(archive_file, 'save'):
            archive_file.save(raw_path)
        else:
            with open(raw_path, 'wb') as f:
                f.write(archive_file.read())

        # arquivo cifrado? decifra; senão assume tar.gz legado
        if not _decrypt_file(raw_path, archive_path):
            shutil.copy2(raw_path, archive_path)

        base_dir = os.path.dirname(os.path.abspath(__file__))

        with tarfile.open(archive_path, 'r:gz') as tar:
            _safe_extract(tar, tmp_dir)

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


def _safe_extract(tar, dest):
    """Evita path traversal (member com ../) na extração do backup."""
    dest_abs = os.path.abspath(dest)
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(dest_abs, member.name))
        if not target.startswith(dest_abs + os.sep):
            raise ValueError('caminho inválido no arquivo de backup')
    tar.extractall(dest)
