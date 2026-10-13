#!/usr/bin/env python3
import http.server, os

PORT = 8080
DIR = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()
    def log_message(self, format, *args):
        pass

# ThreadingHTTPServer: el server de un solo hilo (TCPServer) se saturaba con
# las conexiones concurrentes que abre el navegador al hacer polling del
# dashboard cada 5s, y terminaba rechazando conexiones nuevas.
print(f'Dashboard demo en http://localhost:{PORT}/dashboard_demo.html')
with http.server.ThreadingHTTPServer(("", PORT), Handler) as httpd:
    httpd.daemon_threads = True
    httpd.serve_forever()
