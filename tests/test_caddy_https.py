"""Optional real-Caddy regression test; isolated ports/state, no live scans."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from test_caddy_export import caddy


@unittest.skipUnless(os.environ.get("CADDY_TEST_BIN"), "CADDY_TEST_BIN not supplied")
class CaddyHTTPSIntegrationTests(unittest.TestCase):
    def test_loopback_and_provider_hosts_receive_page_and_icon(self):
        page = b'<html><title>Example app</title><link rel="icon" href="/icon.svg"></html>'
        icon = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'

        class Backend(BaseHTTPRequestHandler):
            def do_GET(self):
                body = icon if self.path == "/icon.svg" else page
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml" if body == icon else "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        backend_port = server.server_port
        with socket.socket() as reserved, socket.socket() as certificate:
            reserved.bind(("127.0.0.1", 0))
            certificate.bind(("127.0.0.1", 0))
            frontend, certificate_port = reserved.getsockname()[1], certificate.getsockname()[1]
        generated = caddy.render([{"port": backend_port, "scheme": "http"}],
                                 {str(backend_port): frontend}, "127.0.0.1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "Caddyfile"
            # The local test CA is never installed into the host trust store.
            config.write_text("{\n admin off\n auto_https disable_redirects\n"
                              " default_bind 127.0.0.1\n default_sni localhost\n skip_install_trust\n}\n"
                              f"https://localhost:{certificate_port} {{\n tls internal\n respond unused\n}}\n"
                              + generated)
            env = {**os.environ, "XDG_DATA_HOME": str(root / "data"),
                   "XDG_CONFIG_HOME": str(root / "config")}
            binary = os.environ["CADDY_TEST_BIN"]
            adapted = subprocess.run([binary, "adapt", "--adapter", "caddyfile", "--config", str(config)],
                                     check=True, capture_output=True, text=True, env=env, timeout=10)
            servers = json.loads(adapted.stdout)["apps"]["http"]["servers"]
            route = next(row for row in servers.values() if f"127.0.0.1:{frontend}" in row["listen"])
            self.assertNotIn('"host":', json.dumps(route["routes"]))
            self.assertTrue(route["tls_connection_policies"])
            with (root / "caddy.log").open("wb") as log:
                process = subprocess.Popen([binary, "run", "--adapter", "caddyfile", "--config", str(config)],
                                           stdout=log, stderr=log, env=env)
                try:
                    # Like the scanner's loopback probe: TLS, without requiring
                    # the public hostname certificate to match a loopback IP.
                    context = ssl._create_unverified_context()
                    for attempt in range(100):
                        try:
                            connection = http.client.HTTPSConnection("127.0.0.1", frontend,
                                                                     context=context, timeout=1)
                            connection.request("GET", "/")
                            self.assertEqual(connection.getresponse().read(), page)
                            break
                        except (OSError, http.client.HTTPException):
                            if process.poll() is not None:
                                self.fail((root / "caddy.log").read_text())
                            time.sleep(0.1)
                        finally:
                            connection.close()
                    else:
                        self.fail((root / "caddy.log").read_text())
                    for host in ("127.0.0.1", "localhost", "node.example.ts.net", "7001.example.com"):
                        for path, expected in (("/", page), ("/icon.svg", icon)):
                            with self.subTest(host=host, path=path):
                                connection = http.client.HTTPSConnection("127.0.0.1", frontend,
                                                                         context=context, timeout=3)
                                try:
                                    connection.request("GET", path, headers={"Host": f"{host}:{frontend}"})
                                    response = connection.getresponse()
                                    self.assertEqual(response.status, 200)
                                    self.assertEqual(response.read(), expected)
                                finally:
                                    connection.close()
                finally:
                    process.terminate()
                    process.wait(timeout=10)
