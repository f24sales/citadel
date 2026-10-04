from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SetupContainerfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "CITADEL"
        self.root.mkdir()
        shutil.copy2(ROOT / "setup.sh", self.root / "setup.sh")
        (self.root / "functions").mkdir()
        shutil.copy2(ROOT / "functions/runtime_state.py", self.root / "functions/runtime_state.py")
        shutil.copy2(ROOT / "functions/webui_transport.py", self.root / "functions/webui_transport.py")
        shutil.copy2(ROOT / "python_header.py", self.root / "python_header.py")
        for name in ("config.sh", "set_daemon.sh"):
            script = self.root / name
            script.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$0.called"\n')
            script.chmod(0o755)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("podman", "docker", "buildah", "systemctl", "loginctl"):
            script = self.bin / name
            script.write_text('#!/bin/sh\ntouch "$FORBIDDEN_LOG"\nexit 99\n')
            script.chmod(0o755)
        self.environment = {**{key: value for key, value in os.environ.items()
                               if not key.startswith(("CITADEL_", "CADDYFILE_"))},
                            "PATH": f"{self.bin}:{os.defpath}",
                            "FORBIDDEN_LOG": str(self.root / "forbidden")}

    def execute(self, *arguments):
        result = subprocess.run(["bash", str(self.root / "setup.sh"), *arguments],
                                cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=15)
        self.assertFalse((self.root / "forbidden").exists())
        return result

    def test_no_arguments_renders_both_and_prints_links_without_starting(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "config.sh.called").read_text(), "\n")
        self.assertEqual((self.root / "set_daemon.sh.called").read_text(), "--render-only\n")
        self.assertIn(str(self.root / "citadel.service"), result.stdout)
        self.assertIn(str(self.root / "citadel.container"), result.stdout)
        self.assertEqual(result.stdout.count("ln -s"), 2)
        self.assertTrue((self.root / "CADDY").is_dir())
        self.assertNotIn("Recommended bind mount", result.stdout)

    def test_caddy_hint_when_either_or_both_features_are_selected(self):
        for transport, port, enabled in (("tcp", "", False), ("tcp", "0", False),
                                         ("unix", "", True), ("tcp", "7000", True),
                                         ("unix", "7000", True)):
            with self.subTest(transport=transport, port=port):
                (self.root / "config.conf").write_text(
                    f"CITADEL_WEBUI_TRANSPORT={transport}\nCADDYFILE_START={port}\n")
                result = self.execute()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("Recommended bind mount" in result.stdout, enabled)
                if enabled:
                    self.assertIn(f"Volume={self.root}/CADDY:/CADDY:ro,z", result.stdout)
                    self.assertIn("unix//CADDY/citadel.sock", result.stdout)
                    self.assertIn("import /CADDY/Caddyfile", result.stdout)

    def test_every_argument_is_rejected_before_configuration(self):
        for argument in ("--help", "--show", "--render-containerfile", "--render-container", "host"):
            with self.subTest(argument=argument):
                self.assertEqual(self.execute(argument).returncode, 2)
                self.assertFalse((self.root / "config.sh.called").exists())

    def test_setup_symlink_keeps_generated_files_in_original_directory(self):
        link = self.root.parent / "configure-citadel"
        link.symlink_to(self.root / "setup.sh")
        result = subprocess.run(["bash", str(link)], cwd=self.root.parent,
                                env=self.environment, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.root / "citadel.service"), result.stdout)
        self.assertIn(str(self.root / "citadel.container"), result.stdout)
        self.assertTrue((self.root / "config.sh.called").exists())
        self.assertFalse((self.root.parent / "CADDY").exists())

    def test_recipe_copies_no_runtime_state_or_credentials(self):
        recipe = (ROOT / ".github/scripts/Containerfile").read_text()
        forbidden = {".env", "config.conf", "config.ini", "ports.filter.json",
                     "services.json", "routes.json", "tailscale.json", "images.json", "auth.json"}
        for line in recipe.splitlines():
            if not line.startswith("COPY "):
                continue
            for pattern in shlex.split(line)[1:-1]:
                self.assertNotIn(pattern, (".", "./", "*"))
                matches = list(ROOT.glob(pattern))
                self.assertTrue(matches, pattern)
                for path in matches:
                    contents = list(path.rglob("*")) if path.is_dir() else [path]
                    self.assertFalse(any(item.name in forbidden for item in contents), pattern)
        command = next(line.removeprefix("ENTRYPOINT ") for line in recipe.splitlines() if line.startswith("ENTRYPOINT "))
        self.assertEqual(json.loads(command)[-1], "/usr/local/bin/citadel-container")
        self.assertIn("alpine:", recipe)
        self.assertIn("@sha256:", recipe)
        self.assertIn("python3 -s functions/runtime_state.py --image", recipe)
        self.assertNotIn("pip install", recipe)

    def test_real_renderer_keeps_bootstrap_and_conditional_mounts(self):
        for name in ("config.sh", "config.conf_example", "container.example"):
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / "config.conf").write_text("CITADEL_WEBUI_PORT=11000\nCITADEL_WEBUI_TRANSPORT=unix\nCADDYFILE_START=7000\n")
        (self.root / "container.conf").write_text("CITADEL_WEBUI_PUBLISH_PORT=\nADDITIONAL_LINE=Pull=never\n")
        (self.root / ".env").write_text("TS_AUTHKEY=\n")
        result = subprocess.run(["bash", str(self.root / "config.sh"), "--render-container"],
                                cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        quadlet = (self.root / "citadel.container").read_text()
        self.assertNotIn("Exec=", quadlet)
        self.assertNotIn("PublishPort=", quadlet)
        self.assertIn("Pull=never", quadlet)
        self.assertNotIn("AutoUpdate=", quadlet)
        self.assertNotIn("cloudflare", quadlet.lower())
        self.assertIn("pull_policy: never", (self.root / "docker-compose.yml").read_text())
        self.assertNotIn("Volume=z", quadlet)
        self.assertIn(f"Volume={self.root}/CITADEL:/opt/safrano9999/CITADEL/CITADEL_DATA:z", quadlet)
        self.assertEqual(sum(line.startswith("Volume=") for line in quadlet.splitlines()), 1)
        self.assertIn("#Volume=citadel-tailscale:/opt/safrano9999/CITADEL/CITADEL_TAILSCALE:Z", quadlet)
        self.assertIn("EnvironmentFile=" + str(self.root / "config.conf"), quadlet)
        self.assertIn("EnvironmentFile=" + str(self.root / ".env"), quadlet)
        self.assertFalse((self.root / "forbidden").exists())

    def test_host_and_quadlet_share_the_original_configuration_files(self):
        for name in ("config.sh", "set_daemon.sh", "config.conf_example", "container.example"):
            shutil.copy2(ROOT / name, self.root / name)
        config = self.root / "config.conf"
        secrets = self.root / ".env"
        config.write_text("CITADEL_WEBUI_TRANSPORT=tcp\nCITADEL_WEBUI_PORT=12345\n")
        secrets.write_text("CITADEL_TOKEN=local-test-only\n")
        (self.root / "container.conf").write_text("CITADEL_WEBUI_PUBLISH_PORT=\n")
        before = {path: path.read_bytes() for path in (config, secrets)}
        for script, argument in (("config.sh", "--render-container"),
                                 ("set_daemon.sh", "--render-only")):
            result = subprocess.run(["bash", str(self.root / script), argument],
                                    cwd=self.root.parent, env=self.environment,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
        unit = (self.root / "citadel.service").read_text()
        quadlet = (self.root / "citadel.container").read_text()
        self.assertIn(f"WorkingDirectory={self.root}\n", unit)
        self.assertIn(str(self.root / "webui.py"), unit)
        for path, original in before.items():
            self.assertIn(f"EnvironmentFile={path}\n", quadlet)
            self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / ".systemd").exists())
        self.assertFalse((self.root / "forbidden").exists())


if __name__ == "__main__":
    unittest.main()
