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
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "CITADEL"
        self.root.mkdir()
        for name in ("setup.sh", "Containerfile.example"):
            shutil.copy2(ROOT / name, self.root / name)
        for name in ("config.sh", "set_daemon.sh"):
            script = self.root / name
            script.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$0.called"\n')
            script.chmod(0o755)
        self.bin_dir = Path(self.temporary.name) / "bin"
        self.bin_dir.mkdir()
        for command in ("podman", "docker", "buildah", "systemctl", "loginctl"):
            stub = self.bin_dir / command
            stub.write_text('#!/bin/sh\ntouch "$FORBIDDEN_CALL_LOG"\nexit 99\n')
            stub.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment["PATH"] = f"{self.bin_dir}:{os.defpath}"
        self.environment["FORBIDDEN_CALL_LOG"] = str(Path(self.temporary.name) / "forbidden-call")

    def execute(self, *arguments, success=True):
        result = subprocess.run(["bash", str(self.root / "setup.sh"), *arguments],
                                cwd=self.root.parent, env=self.environment,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        self.assertFalse(Path(self.environment["FORBIDDEN_CALL_LOG"]).exists())
        return result

    def test_opt_in_renders_only_containerfile_without_build_or_config(self):
        before = {path.name for path in self.root.iterdir()}
        self.execute("--render-containerfile")
        self.assertEqual({path.name for path in self.root.iterdir()} - before, {"Containerfile"})
        self.assertEqual((self.root / "Containerfile").read_bytes(), (ROOT / "Containerfile.example").read_bytes())

    def test_matching_render_is_idempotent(self):
        self.execute("--render-containerfile")
        target = self.root / "Containerfile"
        before = target.stat()
        self.execute("--render-containerfile")
        self.assertEqual((target.stat().st_ino, target.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_custom_containerfile_is_preserved(self):
        target = self.root / "Containerfile"
        target.write_text("FROM custom-image\n")
        self.execute("--render-containerfile", success=False)
        self.assertEqual(target.read_text(), "FROM custom-image\n")

    def test_symlink_target_is_never_overwritten(self):
        target = self.root / "Containerfile"
        external = self.root.parent / "external"
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    external.write_bytes((ROOT / "Containerfile.example").read_bytes())
                target.symlink_to(external)
                self.execute("--render-containerfile", success=False)
                self.assertTrue(target.is_symlink())
                self.assertEqual(external.exists(), present)
                if present:
                    self.assertEqual(external.read_bytes(), (ROOT / "Containerfile.example").read_bytes())
                target.unlink()

    def test_missing_template_fails_before_any_render(self):
        (self.root / "Containerfile.example").unlink()
        self.execute("--render-containerfile", success=False)
        self.assertFalse((self.root / "Containerfile").exists())
        self.assertFalse((self.root / "config.sh.called").exists())

    def test_render_flag_cannot_be_mixed_with_config_options(self):
        for arguments in (("--render-containerfile", "--show"), ("--show", "--render-containerfile")):
            with self.subTest(arguments=arguments):
                self.execute(*arguments, success=False)
                self.assertFalse((self.root / "Containerfile").exists())
                self.assertFalse((self.root / "config.sh.called").exists())

    def test_default_setup_keeps_existing_host_workflow(self):
        self.execute("--show")
        self.assertEqual((self.root / "config.sh.called").read_text(), "--no-container\n--show\n")
        self.assertEqual((self.root / "set_daemon.sh.called").read_text(), "--render-only\n")
        self.assertFalse((self.root / "Containerfile").exists())

    def test_container_checkout_keeps_existing_config_workflow(self):
        parent = self.root.parent / "CONTAINER"
        parent.mkdir()
        self.root = self.root.rename(parent / "CITADEL")
        self.execute("--show")
        self.assertEqual((self.root / "config.sh.called").read_text(), "--show\n")
        self.assertFalse((self.root / "set_daemon.sh.called").exists())
        self.execute("--render-containerfile")
        self.assertTrue((self.root / "Containerfile").is_file())

    def test_help_does_not_configure_or_render(self):
        result = self.execute("--help")
        self.assertIn("--render-containerfile", result.stdout)
        self.assertFalse((self.root / "Containerfile").exists())
        self.assertFalse((self.root / "config.sh.called").exists())

    def test_template_copies_sources_not_runtime_state_and_uses_shared_startup(self):
        template = (ROOT / "Containerfile.example").read_text()
        forbidden = {".env", ".tunnel-token", "config.conf", "config.ini", "ports.filter.json",
                     "services.json", "routes.json", "status.json", "tailscale.json", "providers_state.json"}
        for line in template.splitlines():
            if not line.startswith("COPY "):
                continue
            for pattern in shlex.split(line)[1:-1]:
                self.assertNotIn(pattern, (".", "./", "*"))
                matches = list(ROOT.glob(pattern))
                self.assertTrue(matches, f"Missing build input: {pattern}")
                for path in matches:
                    copied = list(path.rglob("*")) if path.is_dir() else [path]
                    self.assertFalse(any(entry.name in forbidden for entry in copied), pattern)
        command = next(line.removeprefix("CMD ") for line in template.splitlines() if line.startswith("CMD "))
        self.assertEqual(json.loads(command), ["/usr/bin/python3", "-s", "webui.py"])
        self.assertNotIn("CITADEL_WEBUI_TRANSPORT=", template)
        self.assertNotIn("pip install", template)

    def test_real_container_generator_inherits_python_startup_for_both_transports(self):
        for name in ("config.sh", "config.conf_example", "container.example"):
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / "webui.py").write_text("# Fixture: generator detects a Python WebUI.\n")
        self.environment["CONFIG_CONTAINER_IMAGE"] = "localhost/citadel:test"
        self.execute("--render-containerfile")
        for transport in (None, "", "tcp", "unix"):
            with self.subTest(transport=transport):
                config = "FASTAPI_HOST=127.0.0.1\nCITADEL_WEBUI_PORT=11000\n"
                if transport is not None:
                    config += f"CITADEL_WEBUI_TRANSPORT={transport}\n"
                if transport == "unix":
                    config += "CITADEL_WEBUI_SOCKET=/run/citadel/citadel.sock\n"
                (self.root / "config.conf").write_text(config)
                # Exercise regeneration of old generated files, not only a
                # clean output directory. All writes stay inside this fixture.
                (self.root / "docker-compose.yml").write_text(
                    "services:\n  citadel:\n    command: uvicorn webui:app --host 0.0.0.0\n"
                )
                (self.root / "citadel.container").write_text(
                    "[Container]\nExec=uvicorn webui:app --host 0.0.0.0\n"
                )
                result = subprocess.run(
                    ["bash", str(self.root / "config.sh"), "--render-container"],
                    cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
                    text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                compose = (self.root / "docker-compose.yml").read_text()
                quadlet = (self.root / "citadel.container").read_text()
                self.assertNotIn("uvicorn", compose + quadlet)
                self.assertNotIn("command:", compose)
                self.assertNotIn("Exec=", quadlet)
                self.assertIn("dockerfile: Containerfile", compose)
                self.assertIn(f"EnvironmentFile={self.root}/config.conf", quadlet)
                self.assertIn(f"      - {self.root}/config.conf", compose)
                self.assertEqual((self.root / "config.conf").read_text(), config)
                self.assertFalse(Path(self.environment["FORBIDDEN_CALL_LOG"]).exists())


if __name__ == "__main__":
    unittest.main()
