import fcntl
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "functions"))
import reset_serve


class ResetTests(unittest.TestCase):
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
