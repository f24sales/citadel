from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "functions/providers"))
from common import routable_services, route_record

spec = importlib.util.spec_from_file_location("citadel_route_helpers", ROOT / "functions/providers/tailscale.py")
tailscale = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tailscale)


def route(port="8080"):
    return {"TCP": {port: {"HTTPS": True}},
            "Web": {f"node.example.ts.net:{port}": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}}


class RouteHelperTests(unittest.TestCase):
    def test_https_only_filters_backends_without_removing_discovery(self):
        payload = {"https_only": True, "http_services": [
            {"port": 8080, "scheme": "http"}, {"port": 8443, "scheme": "https"}]}
        self.assertEqual([row["port"] for row in routable_services(payload)], [8443])
        self.assertEqual(len(payload["http_services"]), 2)

    def test_backend_port_and_protocol_are_preserved(self):
        self.assertEqual(tailscale.serve_target(8080, "http"), "http://127.0.0.1:8080")
        self.assertEqual(tailscale.serve_target(8443, "https"), "https+insecure://127.0.0.1:8443")

    def test_shared_route_schema_remains_compatible(self):
        record = route_record("proxy", "https://node.example.ts.net:8080",
                              target="http://127.0.0.1:8080", owns_listener=True)
        self.assertEqual(record["mode"], "proxy")
        self.assertEqual(record["url"], "https://node.example.ts.net:8080")

    def test_verification_requires_exact_https_frontend_and_backend(self):
        current = route()
        self.assertTrue(tailscale.https_route_matches(current, "node.example.ts.net", 8080, "http://127.0.0.1:8080"))
        self.assertFalse(tailscale.https_route_matches(current, "wrong.example.ts.net", 8080, "http://127.0.0.1:8080"))
        self.assertFalse(tailscale.https_route_matches(current, "node.example.ts.net", 8080, "https+insecure://127.0.0.1:8080"))
        for kind in ("http", "funnel", "foreground", "extra_path", "other_authority"):
            with self.subTest(kind=kind):
                current = route()
                if kind == "http":
                    current["TCP"]["8080"] = {"HTTP": True}
                elif kind == "funnel":
                    current["AllowFunnel"] = {"node.example.ts.net:8080": True}
                elif kind == "foreground":
                    current["Foreground"] = {"session": route()}
                elif kind == "extra_path":
                    current["Web"]["node.example.ts.net:8080"]["Handlers"]["/extra"] = {"Text": "old"}
                else:
                    current["Web"]["other.example.ts.net:8080"] = {"Handlers": {}}
                self.assertFalse(tailscale.https_route_matches(current, "node.example.ts.net", 8080, "http://127.0.0.1:8080"))

    def test_selected_port_removal_covers_protocols_paths_funnel_and_foreground(self):
        current = route()
        current["TCP"]["22"] = {"TCPForward": "127.0.0.1:22"}
        current["Web"]["other.example.ts.net:8080"] = {"Handlers": {"/extra": {"Text": "old"}}}
        current["AllowFunnel"] = {"node.example.ts.net:8080": True}
        current["Foreground"] = {"old": route(), "keep": route("9090")}
        current["Services"] = {"svc:other": {"TCP": {"8080": {"HTTP": True}}}}
        before = copy.deepcopy(current)
        updated = tailscale.without_ports(current, {"8080"})
        self.assertEqual(current, before)
        self.assertEqual(tailscale.node_ports(updated), {"22", "9090"})
        self.assertEqual(updated["Services"], current["Services"])
        self.assertEqual(updated["Web"], {})
        self.assertEqual(updated["AllowFunnel"], {})

    def test_remove_verifies_live_config_before_returning(self):
        with patch.object(tailscale, "command", return_value="") as command, patch.object(tailscale, "read_live_serve", return_value=route()):
            with self.assertRaisesRegex(ValueError, "remain configured"):
                tailscale.remove_node_ports(route(), {"8080"})
        self.assertEqual(command.call_args.args[0][:5], ["tailscale", "debug", "localapi", "POST", "serve-config"])

    def test_malformed_status_cannot_be_used_for_reconciliation(self):
        for payload in ("[]", "{broken", '{"TCP":[]}'):
            with self.subTest(payload=payload), patch.object(tailscale, "command", return_value=payload):
                with self.assertRaises(ValueError):
                    tailscale.read_live_serve()


if __name__ == "__main__":
    unittest.main()
