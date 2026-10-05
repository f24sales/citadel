import fcntl
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "functions"))
import reset_serve
import tailscale


class ResetTests(unittest.TestCase):
    def test_live_status_not_local_metadata(self):
        with patch.object(reset_serve, "read_live_serve", return_value={
            "TCP": {"11000": {}, "5800": {}}, "Services": {"other": {"TCP": {"443": {}}}},
        }):
            self.assertEqual(reset_serve.status(), {"ports": [5800, 11000]})

    def test_delete_exact_port_preserves_everything_else(self):
        live = {
            "TCP": {"443": {"HTTPS": True}, "5800": {"HTTPS": True}},
            "Web": {"node:443": {"Handlers": {}}, "node:5800": {"Handlers": {}}},
            "AllowFunnel": {"node:443": True, "node:5800": True},
            "Foreground": {"empty": {}, "keep": {"TCP": {"9090": {}}},
                           "mixed": {"TCP": {"5800": {}, "9091": {}}}},
            "Services": {"other": {"TCP": {"5800": {}}}},
        }
        expected = {
            **live, "TCP": {"443": {"HTTPS": True}}, "Web": {"node:443": {"Handlers": {}}},
            "AllowFunnel": {"node:443": True},
            "Foreground": {"empty": {}, "keep": {"TCP": {"9090": {}}}, "mixed": {"TCP": {"9091": {}}}},
        }
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
            "CITADEL_SCAN_LOCK_FILE": raw + "/lock", "CITADEL_TAILSCALE_SERVE": "off",
        }), patch.object(reset_serve, "read_live_serve", return_value=live), \
             patch.object(tailscale, "read_live_serve", return_value=expected), \
             patch.object(tailscale, "command") as command:
            self.assertEqual(reset_serve.delete("05800")["port"], 5800)
            command.assert_called_once()
            args = command.call_args.args[0]
            self.assertEqual(args[:5], ["tailscale", "debug", "localapi", "POST", "serve-config"])
            self.assertEqual(json.loads(args[5]), expected)
            self.assertEqual(sorted(p.name for p in Path(raw).iterdir()), ["lock"])

    def test_absent_delete_does_not_write(self):
        live = {"TCP": {"443": {}}, "Foreground": {"empty": {}}}
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"CITADEL_SCAN_LOCK_FILE": raw + "/lock"}), \
             patch.object(reset_serve, "read_live_serve", return_value=live), \
             patch.object(tailscale, "read_live_serve", return_value=live), \
             patch.object(tailscale, "command") as command:
            reset_serve.delete(5800)
            command.assert_not_called()

    def test_invalid_delete_has_no_side_effects(self):
        with patch.object(reset_serve, "serve_lock") as lock, patch.object(reset_serve, "read_live_serve") as read:
            for port in (None, True, 0, -1, 65536, "", "443 5800", "４４３", "abc"):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    reset_serve.delete(port)
            lock.assert_not_called()
            read.assert_not_called()

    def test_busy_scan_blocks_delete(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"CITADEL_SCAN_LOCK_FILE": raw + "/lock"}), \
             patch.object(reset_serve, "read_live_serve") as read, open(raw + "/lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "running"):
                reset_serve.delete(5800)
            read.assert_not_called()

    def test_scan_delete_dispatch_does_not_initialize_scanner(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw)
            (target / "scan.sh").write_text((root / "scan.sh").read_text())
            (target / "functions/providers").mkdir(parents=True)
            (target / "functions/reset_serve.py").write_text((root / "functions/reset_serve.py").read_text())
            (target / "functions/providers/tailscale.py").write_text(
                'def command(*args): raise AssertionError("reset not allowed")\n'
                'def read_live_serve(): return {}\n'
                'def node_ports(config): return set()\n'
                'def remove_node_ports(config, ports): assert ports == {"5800"}\n')
            env = {**os.environ, "CITADEL_SCAN_LOCK_FILE": raw + "/lock"}
            for args in (["--del"], ["--del", "abc"], ["--del", "0"], ["--del", "65536"],
                         ["--del", "5800", "443"], ["--del", "5800", "--del", "443"],
                         ["--add", "--del", "5800"], ["--del", "5800", "--provider", "tailscale"]):
                with self.subTest(args=args):
                    run = subprocess.run(["bash", str(target / "scan.sh"), *args], env=env, capture_output=True, text=True)
                    self.assertEqual(run.returncode, 2, run.stderr)
                    self.assertFalse((target / "lock").exists())
            run = subprocess.run(["bash", str(target / "scan.sh"), "--del", "5800"], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["port"], 5800)
            self.assertFalse((target / "cache").exists())
            self.assertFalse((target / "CITADEL_DATA").exists())

    def test_explicit_reset_only_touches_serve(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"CITADEL_SCAN_LOCK_FILE": raw + "/lock"}), \
             patch.object(reset_serve, "command") as command, patch.object(reset_serve, "read_live_serve", return_value={}):
            self.assertTrue(reset_serve.reset()["ok"])
            command.assert_called_once_with(["tailscale", "serve", "reset"])

    def test_busy_scan_blocks_reset(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"CITADEL_SCAN_LOCK_FILE": raw + "/lock"}), \
             patch.object(reset_serve, "command") as command, open(raw + "/lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "running"):
                reset_serve.reset()
            command.assert_not_called()

    def test_unconfirmed_reset_fails(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"CITADEL_SCAN_LOCK_FILE": raw + "/lock"}), \
             patch.object(reset_serve, "command"), patch.object(reset_serve, "read_live_serve", return_value={"TCP": {"443": {}}}):
            with self.assertRaisesRegex(ValueError, "not confirmed"):
                reset_serve.reset()
