#!/usr/bin/python3
# -*- coding: utf-8 -*-
# delete.py — Batch delete users from SSH server

import os
import sys

if len(sys.argv) != 2:
    sys.exit(1)

nome_arquivo = sys.argv[1]

with open(nome_arquivo, 'r') as arquivo:
    linhas = arquivo.readlines()
    linhas = [l for l in linhas if l.strip()]
    for linha in linhas:
        colunas = linha.strip().split()
        if len(colunas) >= 2:
            # username uuid
            os.system("./dragonmodule v2raydel {} {}".format(colunas[1], colunas[0]))
        else:
            username = linha.strip()
            os.system("./dragonmodule removessh {}".format(username))

os.remove(nome_arquivo)
os.system("systemctl restart v2ray 2>/dev/null || true")
