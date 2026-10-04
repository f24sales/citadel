from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "functions"))
spec = importlib.util.spec_from_file_location("citadel_unroute", ROOT / "functions/unroute_tailscale.py")
unroute = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unroute)
from providers.tailscale import without_ports


def state(port=11000):
    return {"services": {str(port): {
        "mode": "proxy", "url": f"https://node.example.ts.net:{port}",
        "target": f"http://127.0.0.1:{port}", "owns_listener": True,
    }}, "available": True}


def live_config(port=11000):
    return {"TCP": {str(port): {"HTTPS": True}},
            "Web": {f"node.example.ts.net:{port}": {
                "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}}


class UnrouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = [self.root / name for name in unroute.STATE_PATHS]
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state()))
        (self.root / "config.conf").write_text("CITADEL_WEBUI_PORT=11000\n")
        (self.root / "services.json").write_text(json.dumps({"http_services": [
            {"port": 11000, "urls": {"tailscale": "https://node.example.ts.net:11000",
                                    "localhost": "http://127.0.0.1:11000"}}]}))
        (self.root / "cache").mkdir()
        (self.root / "cache/11000.json").write_text(json.dumps({
            "title": "Citadel", "tailscale_url": "https://node.example.ts.net:11000"}))
        (self.root / "icons").mkdir()
        (self.root / "icons/11000.svg").write_text("<svg/>")
        self.removals = []

    def run_unroute(self, live=None, ports=None, fail_ports=(), serve=True):
        self.live = copy.deepcopy(live if live is not None else live_config())
        def remove(config, keys):
            self.removals.append(keys)
            if keys & {str(port) for port in fail_ports}:
                raise ValueError("denied")
            self.live = without_ports(config, keys)
            return self.live
        with (patch.object(unroute, "read_live_serve", side_effect=lambda: copy.deepcopy(self.live)),
              patch.object(unroute, "serve_management_enabled", return_value=serve),
              patch.object(unroute, "remove_node_ports", side_effect=remove),
              patch.object(unroute.shutil, "which", return_value="tailscale"),
              patch.dict(os.environ, {"CITADEL_SCAN_LOCK_FILE": str(self.root / "scan.lock")})):
            return unroute.unroute(self.root, ports)

    def test_serve_disabled_leaves_all_routes_and_metadata_untouched(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with (patch.object(unroute, "read_live_serve", side_effect=AssertionError("No Serve access")),
              patch.object(unroute, "serve_management_enabled", return_value=False)):
            self.assertEqual(unroute.unroute(self.root), 0)
        self.assertEqual(self.removals, [])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_release_preserves_discovery_other_urls_and_logos(self):
        self.run_unroute()
        self.assertEqual(self.removals, [{"11000"}])
        for path in self.paths:
            payload = json.loads(path.read_text())
            self.assertEqual(payload["services"], {})
            self.assertFalse(payload["available"])
        row = json.loads((self.root / "services.json").read_text())["http_services"][0]
        self.assertEqual(row["urls"], {"localhost": "http://127.0.0.1:11000"})
        self.assertEqual(json.loads((self.root / "cache/11000.json").read_text()), {"title": "Citadel"})
        self.assertEqual((self.root / "icons/11000.svg").read_text(), "<svg/>")

    def test_requested_port_is_removed_without_saved_state_or_ownership(self):
        for path in self.paths:
            path.unlink()
        self.run_unroute()
        self.assertEqual(self.removals, [{"11000"}])
        self.assertEqual(unroute.node_ports(self.live), set())

    def test_changed_target_paths_funnel_tcp_foreground_are_all_removed(self):
        for kind in ("target", "path", "funnel", "foreground", "tcp"):
            with self.subTest(kind=kind):
                live = live_config()
                handlers = live["Web"]["node.example.ts.net:11000"]["Handlers"]
                if kind == "target":
                    handlers["/"]["Proxy"] = "http://127.0.0.1:9999"
                elif kind == "path":
                    handlers["/extra"] = {"Text": "old"}
                elif kind == "funnel":
                    live["AllowFunnel"] = {"node.example.ts.net:11000": True}
                elif kind == "foreground":
                    live = {"Foreground": {"session": live}}
                else:
                    live = {"TCP": {"11000": {"TCPForward": "127.0.0.1:22"}}}
                self.run_unroute(live)
                self.assertNotIn("11000", unroute.node_ports(self.live))

    def test_unrequested_ports_are_preserved(self):
        live = live_config()
        other = live_config(5800)
        live["TCP"].update(other["TCP"])
        live["Web"].update(other["Web"])
        self.run_unroute(live)
        self.assertEqual(unroute.node_ports(self.live), {"5800"})

    def test_missing_live_route_still_clears_stale_metadata(self):
        self.run_unroute({})
        self.assertEqual(self.removals, [])
        self.assertEqual(json.loads(self.paths[0].read_text())["services"], {})

    def test_partial_failure_only_prunes_successfully_released_ports(self):
        live = live_config()
        other = live_config(5800)
        live["TCP"].update(other["TCP"])
        live["Web"].update(other["Web"])
        for path in self.paths:
            payload = state()
            payload["services"].update(state(5800)["services"])
            path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(unroute.UnrouteError, "denied"):
            self.run_unroute(live, [11000, 5800], fail_ports={11000})
        self.assertEqual(set(json.loads(self.paths[0].read_text())["services"]), {"11000"})

    def test_corrupt_metadata_fails_before_removal(self):
        self.paths[0].write_text("{broken")
        with self.assertRaises(unroute.UnrouteError):
            self.run_unroute()
        self.assertEqual(self.removals, [])

    def test_invalid_ports_fail_before_removal(self):
        for port in (0, -1, 65536):
            with self.subTest(port=port), self.assertRaises(unroute.UnrouteError):
                self.run_unroute(ports=[port])
        self.assertEqual(self.removals, [])

    def test_scan_lock_prevents_concurrent_mutation(self):
        with (self.root / "scan.lock").open("a") as lock:
            unroute.fcntl.flock(lock, unroute.fcntl.LOCK_EX | unroute.fcntl.LOCK_NB)
            with self.assertRaisesRegex(unroute.UnrouteError, "scan is running"):
                self.run_unroute()
        self.assertEqual(self.removals, [])


if __name__ == "__main__":
    unittest.main()
