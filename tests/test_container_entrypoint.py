import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from container.entrypoint import configure_optional_clients, enabled, start_tailscale


class OptionalClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "extensions/disabled").mkdir(parents=True)
        for provider in ("tailscale", "cloudflare"):
            directory = self.root / "extensions/enabled" / provider
            directory.mkdir(parents=True)
            (directory / "extension.json").write_text(json.dumps({"enabled": True}))

    def test_missing_credentials_disable_clients_on_overlay_filesystems(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "shutil.os.rename", side_effect=OSError(errno.EXDEV, "cross-device rename")
        ):
            configure_optional_clients(self.root)
            configure_optional_clients(self.root)
        for provider in ("tailscale", "cloudflare"):
            self.assertFalse(enabled(self.root, provider))
            self.assertTrue((self.root / "extensions/disabled" / provider / "extension.json").is_file())

    def test_existing_cloudflare_credentials_do_not_require_a_tailscale_key(self):
        for key in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_TUNNEL_TOKEN"):
            with self.subTest(key=key), patch.dict(os.environ, {key: "test-only"}, clear=True):
                configure_optional_clients(self.root)
                self.assertTrue(enabled(self.root, "cloudflare"))
                self.assertFalse(enabled(self.root, "tailscale"))

    def test_present_credentials_leave_both_extensions_enabled(self):
        with patch.dict(os.environ, {"TS_AUTHKEY": "test-only", "CLOUDFLARE_API_TOKEN": "test-only"}, clear=True):
            configure_optional_clients(self.root)
            self.assertTrue(enabled(self.root, "tailscale"))

    def test_tailscaled_gets_certificate_storage_as_well_as_identity_state(self):
        runtime = Mock()
        with patch.dict(os.environ, {"TS_AUTHKEY": "test-only"}, clear=True), \
                patch("container.entrypoint.Path.exists", return_value=False), \
                patch("container.entrypoint.tailscale_state", return_value="Running"), \
                patch("container.entrypoint.subprocess.run", return_value=Mock(returncode=0)):
            start_tailscale(runtime, self.root)
        runtime.start.assert_called_once_with(
            "tailscaled", f"--statedir={self.root / 'CITADEL_TAILSCALE'}")
        self.assertTrue(enabled(self.root, "cloudflare"))


if __name__ == "__main__":
    unittest.main()
