#!/usr/bin/env python3
import http.server, socketserver, os

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

print(f'Dashboard en http://64.227.144.230:{PORT}/obviobot_dashboard.html')
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.serve_forever()
