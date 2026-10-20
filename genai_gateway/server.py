"""Local-only demonstration HTTP adapter for the in-memory gateway."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from .core import Gateway


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, gateway: Gateway | None = None):
        # Never accept a network-bind address in this no-auth demo server.
        if address[0] != '127.0.0.1':
            raise ValueError('Demo server must bind to 127.0.0.1')
        super().__init__(address, GatewayHandler)
        self.gateway = gateway or Gateway()


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayHTTPServer
    def log_message(self, format, *args):
        # Omit prompts, URL query parameters, headers and untrusted errors.
        return

    def _reply(self, value: dict, status: int = 200):
        body = json.dumps(value).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self._reply({'status': 'ok', 'local_only': True})
        elif self.path == '/metrics':
            self._reply(self.server.gateway.metrics())
        else:
            self._reply({'error': 'not_found'}, 404)

    def do_POST(self):
        if self.path != '/generate':
            return self._reply({'error': 'not_found'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                return self._reply({'error': 'invalid_body_length'}, 413)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                return self._reply({'error': 'invalid_json_object'}, 400)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self._reply({'error': 'invalid_json'}, 400)
        if set(payload) - {'prompt', 'client', 'idempotency_key'}:
            return self._reply({'error': 'unsupported_fields'}, 400)
        result = self.server.gateway.generate(payload.get('prompt'), client=payload.get('client', 'demo'),
                                              idempotency_key=payload.get('idempotency_key'))
        self._reply(result, result['status'])


def serve(port: int = 8765):
    server = GatewayHTTPServer(('127.0.0.1', port))
    print(f'Local demo gateway: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
