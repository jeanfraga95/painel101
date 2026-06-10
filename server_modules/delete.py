#!/usr/bin/python3
# -*- coding: utf-8 -*-
# delete.py — Batch delete users from SSH server

import os
import sys
import re

if len(sys.argv) != 2:
    print("Uso: python3 delete.py <arquivo>")
    sys.exit(1)

nome_arquivo = sys.argv[1]

# Detecta qual binário está disponível
agent_bin = None
for candidate in ['/root/pmaster_agent', '/root/dragonmodule', './pmaster_agent', './dragonmodule']:
    if os.path.exists(candidate):
        agent_bin = candidate
        break

if not agent_bin:
    agent_bin = '/root/pmaster_agent'  # fallback

def is_uuid(value):
    """Verifica se o valor parece um UUID válido"""
    return bool(re.match(r'[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}', value.lower()))

with open(nome_arquivo, 'r') as arquivo:
    linhas = arquivo.readlines()
    linhas = [l for l in linhas if l.strip()]
    
    for linha in linhas:
        colunas = linha.strip().split()
        
        if len(colunas) >= 2:
            # Verifica qual campo é o UUID
            primeiro_eh_uuid = is_uuid(colunas[0])
            segundo_eh_uuid = is_uuid(colunas[1])
            
            if primeiro_eh_uuid:
                # Formato: uuid username
                os.system(f"{agent_bin} v2raydel {colunas[0]} {colunas[1]}")
            elif segundo_eh_uuid:
                # Formato: username uuid
                os.system(f"{agent_bin} v2raydel {colunas[1]} {colunas[0]}")
            else:
                # Não identificou UUID, assume que o primeiro é username e tenta deletar só SSH
                os.system(f"{agent_bin} removessh {colunas[0]}")
        else:
            # Apenas username (SSH puro)
            username = linha.strip()
            os.system(f"{agent_bin} removessh {username}")

os.remove(nome_arquivo)
os.system("systemctl restart v2ray 2>/dev/null || true")
os.system("systemctl restart xray 2>/dev/null || true")
print("Remocao concluida.")
