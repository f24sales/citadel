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
                f"CITADEL_TAILSCALE_SERVE={value}\nCITADEL_LOGO_PERSISTENT={logos}\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.pop("CITADEL_TAILSCALE_SERVE", None)
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

    def test_runtime_flags_never_generate_mounts_or_links(self):
        for serve in ("0", "1"):
            for logos in ("0", "1"):
                for command in ("mounts", "entries"):
                    with self.subTest(serve=serve, logos=logos, command=command):
                        self.assertEqual(self.run_helper(command, serve, logos), [])


if __name__ == "__main__":
    unittest.main()
