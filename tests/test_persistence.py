from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PersistenceExampleTests(unittest.TestCase):
    def run_helper(self, command: str, value: str, logos: str = "0") -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            (config_dir / "config.conf_example").write_text(
                (ROOT / "config.conf_example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (config_dir / "citadel-test_config.conf").write_text(
                f"CITADEL_PERSISTENT={value}\nCITADEL_LOGO_PERSISTENT={logos}\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.pop("CITADEL_PERSISTENT", None)
            environment.pop("CITADEL_LOGO_PERSISTENT", None)
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "optional_persistence.sh"),
                    command,
                    "--config-dir",
                    str(config_dir),
                    "--container",
                    "citadel-test",
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**environment, "CONFIG_CONTAINER_NAME": "citadel-test"},
            )
            return result.stdout.splitlines()

    def test_enabled_persistence_emits_one_volume_and_state_links(self) -> None:
        self.assertEqual(
            self.run_helper("mounts", "1"),
            ["citadel-test-citadel:/named_volumes/CITADEL:Z"],
        )
        entries = self.run_helper("entries", "1")
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].startswith("NAMED_VOLUME_LINKS\t"))
        for relative in (
            "ports.filter.json",
            "extensions/enabled/cloudflare/routes.json",
        ):
            self.assertIn(
                f"/named_volumes/CITADEL/{relative}"
                f"|/opt/safrano9999/CITADEL/{relative}|file",
                entries[0],
            )
        self.assertIn(
            "/named_volumes/CITADEL/tailscale.json"
            "|/opt/safrano9999/CITADEL/tailscale.json|link",
            entries[0],
        )

    def test_disabled_persistence_emits_no_volume_or_links(self) -> None:
        self.assertEqual(self.run_helper("mounts", "0"), [])
        self.assertEqual(self.run_helper("entries", "0"), [])

    def test_logos_persist_independently(self):
        self.assertEqual(self.run_helper("mounts", "0", "1"),
                         ["citadel-test-citadel-logos:/named_volumes/CITADEL_LOGOS:Z"])
        self.assertEqual(len(self.run_helper("mounts", "1", "1")), 2)
        entries = "\\n".join(self.run_helper("entries", "0", "1"))
        self.assertIn("/named_volumes/CITADEL_LOGOS/icons|/opt/safrano9999/CITADEL/icons|dir", entries)
        self.assertNotIn("tailscale.json", entries)


if __name__ == "__main__":
    unittest.main()
