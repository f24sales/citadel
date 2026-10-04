from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("citadel_caddy_export", ROOT / "functions/exporters/caddy.py")
caddy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caddy)


class CaddyExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "CITADEL_DATA").mkdir()
        self.provider = self.root / "extensions/enabled/caddy"
        self.provider.mkdir(parents=True)
        self.config = {"backend": "ucore", "hosts": ["node.example.ts.net", "localhost"]}
        self.write_config()
        self.services([9090, 11000])

    def write_config(self):
        (self.root / "CITADEL_DATA/caddy-config.json").write_text(json.dumps(self.config))

    def services(self, ports):
        (self.root / "CITADEL_DATA/services.json").write_text(json.dumps({
            "http_services": [{"port": port, "scheme": "http"} for port in ports]}))

    def run_export(self, start="4000", steps="1"):
        return caddy.export(self.root, self.provider, start, steps)

    def mapping(self):
        return json.loads((self.root / "CITADEL_DATA/CADDY/ports.json").read_text())["ports"]

    def test_missing_runtime_config_uses_shipped_example(self):
        (self.root / "CITADEL_DATA/caddy-config.json").rename(self.provider / "config.json.example")
        result = self.run_export()
        self.assertTrue(result["available"], result["errors"])
        self.assertIn("http://ucore:11000", (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_text())

    def test_disabled_does_not_create_directory_or_require_valid_settings(self):
        (self.root / "CITADEL_DATA/caddy-config.json").unlink()
        (self.root / "CITADEL_DATA/services.json").unlink()
        for start in ("", "0", "   ", "blank"):
            with self.subTest(start=start):
                result = self.run_export(start, "invalid")
                self.assertFalse(result["considered"])
                self.assertEqual(result["errors"], [])
                self.assertFalse((self.root / "CITADEL_DATA/CADDY").exists())

    def test_explicit_one_remembers_assignments_and_only_writes_files(self):
        with patch("subprocess.run", side_effect=AssertionError("No processes")):
            result = self.run_export()
        self.assertTrue(result["available"], result["errors"])
        self.assertEqual(result["services"], {})
        self.assertEqual(result["kind"], "export")
        self.assertEqual(self.mapping(), {"9090": 4000, "11000": 4001})
        text = (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_text()
        self.assertIn("https://node.example.ts.net:4000, https://localhost:4000", text)
        self.assertIn("reverse_proxy http://ucore:11000", text)
        self.assertNotIn("tls_insecure_skip_verify", text)
        self.assertNotIn("unix//", text)

    def test_blank_steps_discards_old_slots_and_rebuilds_without_a_ledger(self):
        for steps in ("", "   ", "blank"):
            with self.subTest(steps=steps):
                self.services([9090, 11000])
                self.run_export(steps="1")
                self.services([4096, 11000])
                result = self.run_export(steps=steps)
                self.assertEqual(result["errors"], [])
                self.assertFalse((self.root / "CITADEL_DATA/CADDY/ports.json").exists())
                text = (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_text()
                self.assertIn("# backend 4096 -> HTTPS 4000", text)
                self.assertIn("# backend 11000 -> HTTPS 4001", text)
                self.assertNotIn("9090", text)

    def test_fresh_mode_can_replace_corrupt_ledger_but_preserves_it_on_invalid_scan(self):
        self.run_export()
        ledger = self.root / "CITADEL_DATA/CADDY/ports.json"
        ledger.write_text("broken")
        self.services([4096])
        self.assertEqual(self.run_export(steps="")["errors"], [])
        self.assertFalse(ledger.exists())
        self.run_export()
        before = ledger.read_bytes()
        (self.root / "CITADEL_DATA/services.json").write_text("broken")
        self.assertTrue(self.run_export(steps="")["errors"])
        self.assertEqual(ledger.read_bytes(), before)

    def test_new_low_port_appends_and_missing_service_keeps_its_slot(self):
        self.run_export()
        self.services([4096, 11000])
        result = self.run_export()
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.mapping(), {"9090": 4000, "11000": 4001, "4096": 4002})
        text = (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_text()
        self.assertNotIn("ts.net:4000", text)
        self.services([4096, 9090, 11000])
        self.run_export()
        self.assertEqual(self.mapping()["9090"], 4000)

    def test_explicit_steps(self):
        self.assertEqual(self.run_export(steps="2")["errors"], [])
        self.assertEqual(self.mapping(), {"9090": 4000, "11000": 4002})

    def test_own_frontends_are_not_exported_again(self):
        for steps in ("", "1"):
            with self.subTest(steps=steps):
                self.services([9090, 11000])
                self.run_export(steps=steps)
                path = self.root / "CITADEL_DATA/CADDY/Caddyfile"
                before = path.read_text()
                self.services([4000, 4001, 9090, 11000])
                result = self.run_export(steps=steps)
                self.assertEqual(result["mappings_count"], 2)
                self.assertEqual(path.read_text(), before)

    def test_https_sni_can_differ_from_reachable_backend(self):
        self.config.update(backend="host.containers.internal", tls_server_name="node.example.ts.net")
        self.write_config()
        (self.root / "CITADEL_DATA/services.json").write_text(json.dumps({"http_services": [
            {"port": 2000, "scheme": "https"}, {"port": 5800, "scheme": "http"}]}))
        self.assertEqual(self.run_export()["errors"], [])
        text = (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_text()
        self.assertIn("reverse_proxy https://host.containers.internal:2000", text)
        self.assertEqual(text.count("tls_server_name node.example.ts.net"), 1)
        self.assertNotIn("insecure", text)

    def test_unchanged_file_not_rewritten_and_independent_of_transport(self):
        self.run_export()
        path = self.root / "CITADEL_DATA/CADDY/Caddyfile"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with patch.dict(os.environ, {"CITADEL_WEBUI_TRANSPORT": "unix", "CITADEL_WEBUI_SOCKET": "/not/used.sock"}):
            result = self.run_export()
        self.assertTrue(result["available"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_disabling_retains_existing_file_without_rewriting_or_deleting(self):
        self.run_export()
        path = self.root / "CITADEL_DATA/CADDY/Caddyfile"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertFalse(self.run_export(start="0")["considered"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_empty_scan_generates_empty_valid_import_without_releasing_slots(self):
        self.run_export()
        self.services([])
        result = self.run_export()
        self.assertTrue(result["available"])
        self.assertEqual(result["mappings_count"], 0)
        self.assertNotIn("reverse_proxy", (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_text())
        self.assertEqual(len(self.mapping()), 2)

    def test_changed_settings_require_explicit_mapping_reset(self):
        self.run_export()
        before = (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_bytes()
        for start, steps in (("5000", "1"), ("4000", "2")):
            result = self.run_export(start, steps)
            self.assertFalse(result["available"])
            self.assertTrue(result["errors"])
        self.assertEqual((self.root / "CITADEL_DATA/CADDY/Caddyfile").read_bytes(), before)

    def test_bad_values_do_not_generate_files(self):
        for start, steps in (("bad", "1"), ("-1", "1"), ("65536", "1"), ("4000", "0"), ("4000", "bad"), ("65535", "1")):
            result = self.run_export(start, steps)
            self.assertTrue(result["errors"], (start, steps))
            self.assertFalse((self.root / "CITADEL_DATA/CADDY").exists())

    def test_injection_in_config_is_rejected(self):
        for value in ("ucore\n}\n:80 {", "{$SECRET}", "https://ucore", "ucore:80", "ucore/path"):
            for key in ("hosts", "backend"):
                self.config = {"backend": "ucore", "hosts": ["localhost"]}
                self.config[key] = [value] if key == "hosts" else value
                self.write_config()
                self.assertTrue(self.run_export()["errors"], (key, value))
                self.assertFalse((self.root / "CITADEL_DATA/CADDY").exists())

    def test_ipv6_host_and_backend(self):
        self.config = {"backend": "::1", "hosts": ["[::1]"]}
        self.write_config()
        self.assertEqual(self.run_export()["errors"], [])
        self.assertIn("reverse_proxy http://[::1]:9090", (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_text())

    def test_https_upstream_is_not_downgraded_or_unverified(self):
        (self.root / "CITADEL_DATA/services.json").write_text(json.dumps({
            "https_only": True, "http_services": [{"port": 8443, "scheme": "https"}, {"port": 8080, "scheme": "http"}]}))
        self.assertEqual(self.run_export()["errors"], [])
        text = (self.root / "CITADEL_DATA/CADDY/Caddyfile").read_text()
        self.assertIn("https://ucore:8443", text)
        self.assertIn("http://ucore:8080", text)
        self.assertNotIn("insecure", text)

    def test_corrupt_scan_or_allocation_preserves_last_file(self):
        self.run_export()
        path = self.root / "CITADEL_DATA/CADDY/Caddyfile"
        before = path.read_bytes()
        for target in (self.root / "CITADEL_DATA/services.json", self.root / "CITADEL_DATA/CADDY/ports.json"):
            original = target.read_text()
            target.write_text("broken")
            self.assertTrue(self.run_export()["errors"])
            self.assertEqual(path.read_bytes(), before)
            target.write_text(original)


if __name__ == "__main__":
    unittest.main()
