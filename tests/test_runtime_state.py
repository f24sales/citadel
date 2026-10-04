import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "runtime_state", Path(__file__).resolve().parents[1] / "functions/runtime_state.py")
runtime_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime_state)


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = self.root / "extensions/enabled/localhost"
        self.provider.mkdir(parents=True)
        (self.provider / "extension.json").write_text("{}")

    def test_preserves_existing_state_and_is_idempotent(self):
        policy = self.root / "ports.filter.json"
        policy.write_text('{"blacklist":[12345]}')
        routes = self.provider / "routes.json"
        routes.write_text('{"managed_hostnames":["test.example"]}')
        runtime_state.prepare_state(self.root)
        runtime_state.prepare_state(self.root)
        self.assertTrue(policy.is_symlink())
        self.assertIn("12345", policy.read_text())
        self.assertEqual(routes.resolve(), self.root / "CITADEL/localhost-routes.json")
        self.assertIn("test.example", routes.read_text())

    def test_missing_filter_is_allowed_and_shared_writes_are_visible(self):
        runtime_state.prepare_state(self.root)
        policy = self.root / "ports.filter.json"
        self.assertTrue(policy.is_symlink())
        self.assertFalse(policy.exists())
        (self.root / "CITADEL/ports.filter.json").write_text("{}")
        self.assertEqual(policy.read_text(), "{}")

    def test_conflicting_state_is_never_overwritten(self):
        (self.root / "ports.filter.json").write_text("host")
        (self.root / "CITADEL").mkdir()
        (self.root / "CITADEL/ports.filter.json").write_text("container")
        with self.assertRaises(ValueError):
            runtime_state.prepare_state(self.root)
        self.assertEqual((self.root / "ports.filter.json").read_text(), "host")

    def test_image_state_directory_may_be_a_volume_link(self):
        volume = self.root / "volume"
        volume.mkdir()
        (self.root / "CITADEL").symlink_to(volume, target_is_directory=True)
        runtime_state.prepare_state(self.root)
        (volume / "services.json").write_text("{}")
        self.assertEqual((self.root / "services.json").read_text(), "{}")

    def test_image_data_stays_below_the_repo_without_covering_code(self):
        code = self.root / "webui.py"
        code.write_text("# baked application")
        runtime_state.prepare_image(self.root)
        runtime_state.prepare_image(self.root)
        self.assertEqual((self.root / "icons").resolve(), self.root / "CITADEL_DATA/icons")
        self.assertEqual((self.root / "CADDY").resolve(), self.root / "CITADEL_DATA/CADDY")
        self.assertEqual((self.root / "ports.filter.json").resolve(), self.root / "CITADEL_DATA/ports.filter.json")
        self.assertEqual(code.read_text(), "# baked application")
        self.assertFalse((self.root / "CITADEL").exists())
        tailscale = self.root / "CITADEL_TAILSCALE"
        self.assertTrue(tailscale.is_dir())
        self.assertEqual(tailscale.stat().st_mode & 0o777, 0o700)

    def test_one_directory_holds_logos_caddy_and_exporter_settings(self):
        for directory in ("icons", "CADDY"):
            (self.root / directory).mkdir()
            (self.root / directory / "existing").write_text("keep")
        caddy = self.root / "extensions/enabled/caddy"
        caddy.mkdir()
        (caddy / "config.json").write_text('{"backend":"example.com"}')
        runtime_state.prepare_state(self.root)
        runtime_state.prepare_state(self.root)
        for directory in ("icons", "CADDY"):
            self.assertEqual((self.root / directory).resolve(), self.root / "CITADEL" / directory)
            self.assertEqual((self.root / directory / "existing").read_text(), "keep")
        self.assertEqual((caddy / "config.json").resolve(), self.root / "CITADEL/caddy-config.json")

    def test_cloudflare_output_is_not_in_the_persistent_mount(self):
        cloudflare = self.root / "extensions/enabled/cloudflare"
        cloudflare.mkdir()
        (cloudflare / "extension.json").write_text("{}")
        (cloudflare / "routes.json").write_text("{}")
        runtime_state.prepare_state(self.root)
        self.assertFalse((cloudflare / "routes.json").is_symlink())
        self.assertFalse((self.root / "CITADEL/cloudflare-routes.json").exists())


if __name__ == "__main__":
    unittest.main()
