#!/usr/bin/python3
# -*- coding: utf-8 -*-
# pmaster_delete.py — Batch delete users from SSH server
import os, sys

if len(sys.argv) != 2:
    sys.exit(1)

nome_arquivo = sys.argv[1]
with open(nome_arquivo, 'r') as f:
    linhas = [l.strip() for l in f.readlines() if l.strip()]

for linha in linhas:
    cols = linha.split()
    if len(cols) >= 2:
        os.system("/root/pmaster_agent v2raydel {} {}".format(cols[1], cols[0]))
    else:
        os.system("/root/pmaster_agent removessh {}".format(cols[0]))

os.remove(nome_arquivo)
for svc in ('xray', 'v2ray'):
    os.system(f"systemctl restart {svc} 2>/dev/null || true")
