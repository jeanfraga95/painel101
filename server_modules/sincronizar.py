#!/usr/bin/python3
# -*- coding: utf-8 -*-
# sincronizar.py — Sync users to SSH server

import os
import sys

if len(sys.argv) != 2:
    print("Uso: python3 sincronizar.py <arquivo>")
    sys.exit(1)

nome_arquivo = sys.argv[1]

with open(nome_arquivo, 'r') as arquivo:
    linhas = arquivo.readlines()
    linhas = [l for l in linhas if l.strip()]
    for linha in linhas:
        colunas = linha.strip().split()
        if len(colunas) >= 5:
            # v2ray: username password days limit uuid
            os.system("./dragonmodule v2rayadd {} {} {} {} {}".format(*colunas[:5]))
        elif len(colunas) >= 4:
            os.system("./dragonmodule createssh {} {} {} {}".format(*colunas[:4]))

os.remove(nome_arquivo)
os.system("systemctl restart v2ray 2>/dev/null || true")
os.system("systemctl restart xray 2>/dev/null || true")
print("Sincronizacao concluida.")
