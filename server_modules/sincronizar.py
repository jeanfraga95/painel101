#!/usr/bin/python3
# -*- coding: utf-8 -*-
# sincronizar.py — Sync users to SSH server (CORRIGIDO)

import os
import sys
import re

if len(sys.argv) != 2:
    print("Uso: python3 sincronizar.py <arquivo>")
    sys.exit(1)

nome_arquivo = sys.argv[1]

# Detecta qual binário está disponível (prioriza pmaster_agent, depois dragonmodule)
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
        
        if len(colunas) >= 5:
            # Verifica onde está o UUID
            primeiro_eh_uuid = is_uuid(colunas[0])
            ultimo_eh_uuid = is_uuid(colunas[4])
            
            if primeiro_eh_uuid:
                # Formato: uuid username password days limit (já está correto)
                os.system(f"{agent_bin} v2rayadd {colunas[0]} {colunas[1]} {colunas[2]} {colunas[3]} {colunas[4]}")
            elif ultimo_eh_uuid:
                # Formato LEGADO: username password days limit uuid
                # Reordena para o formato correto do dragonmodule
                os.system(f"{agent_bin} v2rayadd {colunas[4]} {colunas[0]} {colunas[1]} {colunas[2]} {colunas[3]}")
            else:
                # Não identificou UUID, assume que é o formato legado
                os.system(f"{agent_bin} v2rayadd {colunas[4]} {colunas[0]} {colunas[1]} {colunas[2]} {colunas[3]}")
                
        elif len(colunas) >= 4:
            # SSH puro: username password days limit
            os.system(f"{agent_bin} createssh {colunas[0]} {colunas[1]} {colunas[2]} {colunas[3]}")

# Limpa o arquivo temporário e reinicia os serviços
os.remove(nome_arquivo)
os.system("systemctl restart v2ray 2>/dev/null || true")
os.system("systemctl restart xray 2>/dev/null || true")
print("Sincronizacao concluida.")
