from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "functions"))

from functions import core


class TailscaleDashboardTests(unittest.TestCase):
    def test_single_provider_keeps_https_urls_on_original_ports(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            (base / "CITADEL_DATA").mkdir()
            enabled = base / "extensions/enabled"
            provider = enabled / "tailscale"
            provider.mkdir(parents=True)
            (provider / "extension.json").write_text('{"label":"Tailscale"}')
            state = base / "providers_state.json"
            state.write_text("{}")
            services = {
                "2000": {"url": "https://node.example.ts.net:2000"},
                "4096": {"url": "https://node.example.ts.net:4096"},
            }
            for considered in (True, False):
                (base / "CITADEL_DATA/tailscale-routes.json").write_text(json.dumps({
                    "considered": considered, "available": considered,
                    "domain": "node.example.ts.net",
                    "services": services if considered else {},
                }))
                with patch.object(core, "ENABLED_EXT_DIR", enabled), patch.object(core, "PROVIDERS_STATE_FILE", state):
                    result = core._load_providers()
                self.assertEqual(result["provider_options"], {"tailscale": "Tailscale"} if considered else {})
                if considered:
                    self.assertEqual(result["provider_urls_by_port"]["tailscale"],
                                     {key: route["url"] for key, route in services.items()})
                    self.assertEqual(result["provider_header_meta"],
                                     [{"label": "Tailscale", "value": "node.example.ts.net"}])

    def test_failed_https_route_is_not_advertised(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            (base / "CITADEL_DATA").mkdir()
            provider = base / "extensions/enabled" / "tailscale"
            provider.mkdir(parents=True)
            (provider / "extension.json").write_text('{"label":"Tailscale"}')
            (base / "CITADEL_DATA/tailscale-routes.json").write_text(json.dumps({
                "considered": True, "available": False,
                "domain": "node.example.ts.net", "services": {},
                "errors": ["port 9090: HTTPS Serve configuration was not confirmed"],
            }))
            state = base / "providers_state.json"
            state.write_text("{}")
            with patch.object(core, "ENABLED_EXT_DIR", provider.parent), patch.object(core, "PROVIDERS_STATE_FILE", state):
                result = core._load_providers()
            self.assertNotIn("9090", result["provider_urls_by_port"].get("tailscale", {}))


if __name__ == "__main__":
    unittest.main()
