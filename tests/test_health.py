import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import webui
from health import snapshot

class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.write("services.json", {"http_services": [{"port": 8000, "name": "Example", "urls": {"tailscale": "https://test.ts.net:8000"}}]})
        self.write("cache/8000.json", {"kind": "html"})
        self.write("extensions/providers_state.json", {"providers": {"tailscale": {"status": "ok"}}})
        self.write("extensions/enabled/tailscale/extension.json", {"enabled": True})
        self.write("extensions/enabled/tailscale/routes.json", {"considered": True, "available": True, "services": {"8000": {"url": "https://test.ts.net:8000"}}})
        (self.base / "last_scan.txt").write_text("2026-09-16 18:27:03")

    def write(self, name, data):
        p = self.base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))

    def test_read_only_selected_extensions(self):
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob("*") if p.is_file()}
        with patch("subprocess.run", side_effect=AssertionError("No processes")), patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            data = snapshot(self.base, ["tailscale", "cloudflare"])
        self.assertEqual(data["status"], "PASS")
        self.assertEqual([e["status"] for e in data["extensions"]], ["PASS", "SKIP"])
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_only_absent_extensions_never_pass(self):
        self.assertEqual(snapshot(self.base, ["cloudflare"])["status"], "NOT_TESTED")

    def test_missing_cache_fails(self):
        (self.base / "cache/8000.json").unlink()
        self.assertEqual(snapshot(self.base, ["tailscale"])["status"], "FAIL")

    def test_missing_index_means_not_tested(self):
        (self.base / "services.json").write_text("not json")
        result = snapshot(self.base, ["tailscale"])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["extensions"][0]["status"], "NOT_TESTED")

    def test_missing_assigned_route_fails(self):
        self.write("extensions/enabled/tailscale/routes.json", {"considered": True, "available": True, "services": {}})
        self.assertEqual(snapshot(self.base, ["tailscale"])["status"], "FAIL")

    def test_provider_failure_is_not_skip(self):
        self.write("extensions/providers_state.json", {"providers": {"tailscale": {"status": "error"}}})
        self.assertEqual(snapshot(self.base, ["tailscale"])["extensions"][0]["status"], "FAIL")

    def test_no_path_traversal(self):
        with self.assertRaises(ValueError):
            snapshot(self.base, ["../../other"])

    def test_variant_routes_are_deduplicated(self):
        self.write("extensions/enabled/tailscale/routes.json", {"considered": True, "available": True, "services": {"8000": {"url": "https://test.ts.net:8000"}}, "variants": {"https": {"considered": True, "services": {"8000": {"url": "https://test.ts.net:8000"}}}}})
        self.assertEqual(len(snapshot(self.base)["extensions"][0]["services"]), 1)

    def test_web_routes_and_html_escape(self):
        with patch.object(webui.core, "BASE_DIR", self.base):
            self.assertEqual(webui.health_json("tailscale")["status"], "PASS")
            page = webui.health_page("tailscale")
            self.assertIn("Index-Zustand", page)
            self.assertIn("✅", page)
            with self.assertRaises(webui.HTTPException) as error:
                webui.health_json("../bad")
            self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
