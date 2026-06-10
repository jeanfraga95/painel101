#!/usr/bin/python3
# -*- coding: utf-8 -*-
# pmaster_delete.py — Batch delete users from SSH server

import os
import sys
import re

if len(sys.argv) != 2:
    print("Uso: python3 pmaster_delete.py <arquivo>")
    sys.exit(1)

nome_arquivo = sys.argv[1]

def is_uuid(value):
    """Verifica se o valor parece um UUID válido"""
    return bool(re.match(r'[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}', value.lower()))

with open(nome_arquivo, 'r') as f:
    linhas = [l.strip() for l in f.readlines() if l.strip()]

for linha in linhas:
    cols = linha.split()
    if len(cols) >= 2:
        # Detecta onde está o UUID
        primeiro_eh_uuid = is_uuid(cols[0])
        segundo_eh_uuid = is_uuid(cols[1])
        
        if primeiro_eh_uuid:
            # Formato: uuid username
            os.system(f"/root/pmaster_agent v2raydel {cols[0]} {cols[1]}")
        elif segundo_eh_uuid:
            # Formato: username uuid
            os.system(f"/root/pmaster_agent v2raydel {cols[1]} {cols[0]}")
        else:
            # Não identificou UUID, assume que é apenas username SSH
            os.system(f"/root/pmaster_agent removessh {cols[0]}")
    else:
        # Apenas username (SSH puro)
        os.system(f"/root/pmaster_agent removessh {cols[0]}")

os.remove(nome_arquivo)
for svc in ('xray', 'v2ray'):
    os.system(f"systemctl restart {svc} 2>/dev/null || true")
print("Remocao concluida.")
