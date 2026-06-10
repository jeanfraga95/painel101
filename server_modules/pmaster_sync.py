#!/usr/bin/python3
# -*- coding: utf-8 -*-
# pmaster_sync.py — Sync users to SSH server (CORRIGIDO)

import os
import sys
import re

if len(sys.argv) != 2:
    print("Uso: python3 pmaster_sync.py <arquivo>")
    sys.exit(1)

nome_arquivo = sys.argv[1]

def is_uuid(value):
    """Verifica se o valor parece um UUID válido"""
    return bool(re.match(r'[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}', value.lower()))

with open(nome_arquivo, 'r') as f:
    linhas = [l.strip() for l in f.readlines() if l.strip()]

for linha in linhas:
    cols = linha.split()
    
    if len(cols) >= 5:
        primeiro_eh_uuid = is_uuid(cols[0])
        ultimo_eh_uuid = is_uuid(cols[4])
        
        if primeiro_eh_uuid:
            # Formato correto: uuid username password days limit
            os.system("/root/pmaster_agent v2rayadd {} {} {} {} {}".format(
                cols[0], cols[1], cols[2], cols[3], cols[4]))
        elif ultimo_eh_uuid:
            # Formato legado: username password days limit uuid
            # Reordena para o formato correto
            os.system("/root/pmaster_agent v2rayadd {} {} {} {} {}".format(
                cols[4], cols[0], cols[1], cols[2], cols[3]))
        else:
            # Fallback: assume formato legado
            os.system("/root/pmaster_agent v2rayadd {} {} {} {} {}".format(
                cols[4], cols[0], cols[1], cols[2], cols[3]))
    elif len(cols) >= 4:
        # SSH puro: username password days limit
        os.system("/root/pmaster_agent createssh {} {} {} {}".format(
            cols[0], cols[1], cols[2], cols[3]))

os.remove(nome_arquivo)
for svc in ('xray', 'v2ray'):
    os.system(f"systemctl restart {svc} 2>/dev/null || true")
print("Sincronizacao concluida.")
