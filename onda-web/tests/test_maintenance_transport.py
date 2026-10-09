"""Exercise urllib EOF and the shared GitHub deadline with real HTTP bodies."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import pytest
from api import maintenance


@pytest.mark.parametrize('padding', [0, 40000])
def test_connection_close_after_exact_content_length_preserves_json_evidence(padding):
    expected = {'state': 'active', 'padding': 'x' * padding}
    body = json.dumps(expected).encode()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert maintenance.read_json(f'http://127.0.0.1:{server.server_port}/evidence') == expected
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_expired_shared_deadline_never_opens_a_new_request(monkeypatch):
    def unavailable(*_args):
        raise AssertionError('The evidence budget already expired')
    monkeypatch.setattr(maintenance.urllib.request, 'build_opener', unavailable)
    token = maintenance._DEADLINE.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match='github_budget_exhausted'):
            maintenance.read_json('https://api.github.com/unopened')
    finally:
        maintenance._DEADLINE.reset(token)
