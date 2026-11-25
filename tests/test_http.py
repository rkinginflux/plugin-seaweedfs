"""Real HTTP-server transport boundary tests (not real SeaweedFS)."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest
import time
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path
from test_inventory import plugin


class Handler(BaseHTTPRequestHandler):
    body = b'{"ok":true}'
    code = 200
    def do_GET(self):
        self.server.paths.append(self.path)
        if self.server.slow_headers or self.server.slow_body:
            try:
                if self.server.slow_headers:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n')
                    for _ in range(12):
                        self.wfile.write(b'X-Slow: true\r\n')
                        self.wfile.flush()
                        time.sleep(0.04)
                    self.wfile.write(b'\r\n{}')
                else:
                    self.send_response(200)
                    self.end_headers()
                    for chunk in b'{"slow":"12345678901234567890"}':
                        self.wfile.write(bytes([chunk]))
                        self.wfile.flush()
                        time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_response(self.code)
        if self.code == 302:
            self.send_header('Location', '/should-not-follow')
        self.end_headers()
        self.wfile.write(self.body)
    def log_message(self, *args):
        pass


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.mod = plugin()
        Handler.body = b'{"ok":true}'
        Handler.code = 200
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.paths = []
        self.server.slow_headers = False
        self.server.slow_body = False
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
    def test_slow_headers_obey_overall_deadline(self):
        self.server.slow_headers = True
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.mod.fetch_json(self.url, '/vol/status', 0.1, 1024)
        self.assertLess(time.monotonic() - start, 0.3)

    def test_slow_body_obeys_remaining_deadline(self):
        self.server.slow_body = True
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.mod.fetch_json(self.url, '/vol/status', 0.1, 1024)
        self.assertLess(time.monotonic() - start, 0.3)

    @unittest.skipUnless(shutil.which('openssl'), 'openssl required for local TLS fixture')
    def test_https_verifies_ca_and_host_before_bearer_request(self):
        with tempfile.TemporaryDirectory(prefix='seaweedfs-tls-') as temporary:
            cert, key = Path(temporary) / 'cert.pem', Path(temporary) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '1',
                            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                           check=True, capture_output=True)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.paths, server.slow_headers, server.slow_body = [], False, False
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'https://localhost:{server.server_port}'
                with self.assertRaises(ssl.SSLCertVerificationError):
                    self.mod.fetch_json(url, '/vol/status', 2, 1024)
                with self.assertRaises(ssl.SSLCertVerificationError):
                    self.mod.fetch_json(f'https://127.0.0.1:{server.server_port}', '/vol/status', 2, 1024, str(cert))
                self.assertTrue(self.mod.fetch_json(url, '/vol/status', 2, 1024, str(cert), 'synthetic-test-token')['ok'])
                self.assertEqual(server.paths, ['/vol/status'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_http_bearer_is_rejected_before_network(self):
        with self.assertRaises(ValueError):
            self.mod.fetch_json(self.url, '/vol/status', 2, 1024, bearer_token='synthetic-token')
        self.assertEqual(self.server.paths, [])

    def test_real_http_get_uses_only_allowed_path(self):
        self.assertTrue(self.mod.fetch_json(self.url, '/vol/status', 2, 1024)['ok'])
        self.assertEqual(self.server.paths, ['/vol/status'])
        with self.assertRaises(ValueError):
            self.mod.fetch_json(self.url, '/vol/grow', 2, 1024)
    def test_redirect_never_followed(self):
        Handler.code = 302
        with self.assertRaises(Exception):
            self.mod.fetch_json(self.url, '/vol/status', 2, 1024)
        self.assertEqual(self.server.paths, ['/vol/status'])
    def test_oversize_bad_json_and_error_status_fail(self):
        for body, code, limit in [(b'x' * 20, 200, 10), (b'not JSON', 200, 100), (b'{}', 503, 100), (b'{"a":1,"a":2}', 200, 100), (b'{"a":NaN}', 200, 100)]:
            Handler.body, Handler.code = body, code
            with self.subTest(body=body, code=code), self.assertRaises(Exception):
                self.mod.fetch_json(self.url, '/vol/status', 2, limit)
    def test_url_credentials_query_fragment_and_path_rejected(self):
        for url in ['http://user:secret@localhost', self.url+'/vol/grow', self.url+'?x=1', self.url+'#secret', 'file:///etc/passwd']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.mod.fetch_json(url, '/vol/status', 2, 100)
        self.assertEqual(self.server.paths, [])
