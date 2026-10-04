from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "functions"))
from runtime_state import data_directory, prepare_image, prepare_state, provider_output


class RuntimeStateTests(unittest.TestCase):
    def test_host_and_image_use_direct_identical_directories(self):
        for prepare in (prepare_state, prepare_image):
            with self.subTest(prepare=prepare.__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                prepare(root)
                data = data_directory(root)
                (data / "icons/keep.svg").write_text("keep")
                (data / "ports.filter.json").write_text('{"blacklist":[12345]}')
                prepare(root)
                self.assertTrue((data / "CADDY").is_dir())
                self.assertEqual((data / "icons/keep.svg").read_text(), "keep")
                self.assertIn("12345", (data / "ports.filter.json").read_text())
                self.assertFalse(any(path.is_symlink() for path in root.rglob("*")))
                for name in ("icons", "CADDY", "CITADEL", "ports.filter.json"):
                    self.assertFalse((root / name).exists())

    def test_missing_filter_needs_no_migration_or_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_state(root)
            self.assertFalse((data_directory(root) / "ports.filter.json").exists())

    def test_cloudflare_output_is_ephemeral(self):
        root = Path("/opt/safrano9999/CITADEL")
        self.assertEqual(provider_output(root, "cloudflare"), root / "cache/cloudflare-routes.json")
        self.assertEqual(provider_output(root, "caddy", "export"), root / "CITADEL_DATA/caddy-status.json")
        self.assertEqual(provider_output(root, "tailscale"), root / "CITADEL_DATA/tailscale-routes.json")

    def test_image_tailscale_state_is_separate_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_image(root)
            self.assertEqual((root / "CITADEL_TAILSCALE").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
