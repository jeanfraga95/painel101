#!/usr/bin/python3
# -*- coding: utf-8 -*-
# pmaster_sync.py — Sync users to SSH server
import os, sys

if len(sys.argv) != 2:
    print("Uso: python3 pmaster_sync.py <arquivo>")
    sys.exit(1)

nome_arquivo = sys.argv[1]
with open(nome_arquivo, 'r') as f:
    linhas = [l.strip() for l in f.readlines() if l.strip()]

for linha in linhas:
    cols = linha.split()
    if len(cols) >= 5:
        # username password days limit uuid
        os.system("/root/pmaster_agent v2rayadd {} {} {} {} {}".format(*cols[:5]))
    elif len(cols) >= 4:
        os.system("/root/pmaster_agent createssh {} {} {} {}".format(*cols[:4]))

os.remove(nome_arquivo)
for svc in ('xray', 'v2ray'):
    os.system(f"systemctl restart {svc} 2>/dev/null || true")
print("Sincronizacao concluida.")
