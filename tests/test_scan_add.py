import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "functions"))
from scan_add import merge_services, previous_services
from providers.common import write_routes


class ScanAddTests(unittest.TestCase):
    def test_merge_retains_old_offline_and_pending_routes(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "services.json"
            old = {"http_services": [{"port": 9000, "title": "Offline", "icon": "icons/9000.svg"}],
                   "other_ports": [{"port": 22}], "pending_add_ports": [9000]}
            path.write_text(json.dumps(old))
            new = {"http_services": [{"port": 8000}], "other_ports": [],
                   "https_only": False, "generated_at": "now"}
            merged = merge_services(path, new)
            self.assertEqual(merged["http_services"][1], old["http_services"][0])
            self.assertEqual(merged["other_ports"], old["other_ports"])
            self.assertEqual(merged["added_ports"], [8000, 9000])
            self.assertEqual(merged["pending_add_ports"], [8000, 9000])

    def test_invalid_existing_state_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, CITADEL_SCAN_ADD="1"):
            path = Path(raw) / "services.json"
            path.write_text("invalid")
            with self.assertRaises(ValueError):
                previous_services(path)
            with self.assertRaises(ValueError):
                write_routes(str(path), {"services": {"8000": {}}})
            self.assertEqual(path.read_text(), "invalid")
