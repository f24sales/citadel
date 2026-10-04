from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "functions"))
sys.path.insert(0, str(ROOT / "functions" / "providers"))

import core
import dispatch
import health

spec = importlib.util.spec_from_file_location("export_health_checker", ROOT / "functions/citadel-health-check.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class ExportFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.enabled = self.base / "extensions/enabled"
        self.enabled.mkdir(parents=True)
        self.write("CITADEL_DATA/services.json", {"http_services": []})
        self.write("CITADEL_DATA/providers_state.json", {"providers": {"caddy": {"status": "ok", "kind": "export"}}})
        (self.base / "CITADEL_DATA/last_scan.txt").write_text("2026-10-02T12:00:00Z")
        for target in ("subprocess.run", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("Live processes/network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def write(self, name, data):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def manifest(self, name="caddy", **overrides):
        data = {"provider": name, "kind": "export", "label": name.title(), "enabled": True}
        data.update(overrides)
        return self.write(f"extensions/enabled/{name}/extension.json", data)

    def export(self, *, content=b"example.invalid { respond 200 }\n", configured=True, **overrides):
        self.manifest()
        artifacts = []
        if configured:
            artifact = self.base / "CITADEL_DATA/CADDY/Caddyfile"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(content)
            artifacts = [{"path": "CITADEL_DATA/CADDY/Caddyfile", "sha256": hashlib.sha256(content).hexdigest()}]
        status = {
            "provider_id": "caddy", "label": "Caddy", "kind": "export",
            "considered": configured, "available": configured,
            "generated_at": "2026-10-02T12:00:00Z", "services": {}, "errors": [],
            "artifacts": artifacts,
        }
        if configured:
            status.update(generated_file="CITADEL_DATA/CADDY/Caddyfile", mappings_count=1)
        status.update(overrides)
        self.write("CITADEL_DATA/caddy-status.json", status)
        return status

    def provider(self):
        self.write("extensions/enabled/localhost/extension.json", {"provider": "localhost", "label": "Localhost"})
        self.write("CITADEL_DATA/localhost-routes.json", {
            "considered": True, "available": True, "errors": [],
            "services": {"8000": {"url": "http://localhost:8000"}},
        })
        self.write("CITADEL_DATA/services.json", {"http_services": [{
            "port": 8000, "name": "Example", "urls": {"localhost": "http://localhost:8000"},
        }]})
        self.write("cache/8000.json", {"kind": "html"})
        self.write("CITADEL_DATA/providers_state.json", {
            "providers": {"caddy": {"status": "ok", "kind": "export"}, "localhost": {"status": "ok"}},
            "considered_providers": ["localhost"], "available_providers": ["localhost"],
        })


class ExportDispatchTests(ExportFixture):
    def setUp(self):
        super().setUp()
        self.manifest()
        self.stub = self.base / "functions/exporters/caddy.py"
        self.stub.parent.mkdir(parents=True)
        self.stub.write_text("# Path fixture only: subprocess.run is always mocked.\n")
        self.status = {
            "provider_id": "caddy", "label": "Caddy", "kind": "export",
            "considered": True, "available": True, "services": {}, "errors": [],
            "generated_at": "2026-10-02T12:00:00Z", "mappings_count": 0,
            "generated_file": "CITADEL_DATA/CADDY/Caddyfile",
            "artifacts": [{"path": "CITADEL_DATA/CADDY/Caddyfile", "sha256": hashlib.sha256(b"").hexdigest()}],
        }

    def run_dispatch(self, *extra, returncode=0, payload=None):
        arguments = [
            "dispatch.py", "--enabled-dir", str(self.enabled),
            "--services-file", str(self.base / "CITADEL_DATA/services.json"),
            "--cache-dir", str(self.base / "cache"),
            "--config-ini", str(self.base / "config.ini"),
            "--state-file", str(self.base / "CITADEL_DATA/providers_state.json"),
            "--tailscale-file", str(self.base / "tailscale.json"), *extra,
        ]

        def fake_run(command, **kwargs):
            output = Path(command[command.index("--routes-out") + 1])
            # Exercise the same direct atomic writer as the real providers.
            dispatch.write_json(str(output), self.status if payload is None else payload)
            return subprocess.CompletedProcess(command, returncode, "", "export failed" if returncode else "")

        with (
            patch.object(sys, "argv", arguments),
            patch.object(dispatch, "__file__", str(self.base / "functions/providers/dispatch.py")),
            patch.object(dispatch.subprocess, "run", side_effect=fake_run) as run,
            redirect_stdout(io.StringIO()),
        ):
            code = dispatch.main()
        state = json.loads((self.base / "CITADEL_DATA/providers_state.json").read_text())
        return code, state, run

    def test_export_script_status_path_and_provider_metadata(self):
        code, state, run = self.run_dispatch("--provider", "caddy", "--strict")
        self.assertEqual(code, 0)
        command = run.call_args.args[0]
        self.assertEqual(Path(command[1]), self.stub)
        self.assertEqual(Path(command[command.index("--provider-dir") + 1]), self.enabled / "caddy")
        self.assertEqual(Path(command[command.index("--routes-out") + 1]), self.base / "CITADEL_DATA/caddy-status.json")
        for flag, path in (("--services-file", "CITADEL_DATA/services.json"), ("--cache-dir", "cache"),
                           ("--config-ini", "config.ini"), ("--tailscale-file", "tailscale.json")):
            self.assertEqual(Path(command[command.index(flag) + 1]), self.base / path)
        self.assertFalse((self.base / "CITADEL_DATA/caddy-routes.json").exists())
        for key in ("enabled", "considered", "available"):
            self.assertEqual(state[f"{key}_providers"], [])
            self.assertEqual(state[f"{key}_exports"], ["caddy"])
        self.assertEqual(state["providers"]["caddy"]["kind"], "export")
        self.assertEqual(state["providers"]["caddy"]["mappings_count"], 0)

    def test_export_status_uses_separate_runtime_directory(self):
        runtime = self.base / "runtime"
        code, _, run = self.run_dispatch("--routes-dir", str(runtime), "--strict")
        self.assertEqual(code, 0)
        command = run.call_args.args[0]
        self.assertEqual(Path(command[command.index("--routes-out") + 1]), runtime / "caddy-status.json")
        self.assertFalse((self.base / "CITADEL_DATA/caddy-status.json").exists())

    def test_missing_kind_still_dispatches_url_provider(self):
        self.write("extensions/enabled/caddy/extension.json", {"provider": "localhost"})
        script = self.base / "functions/providers/localhost.py"
        script.parent.mkdir(parents=True)
        script.write_text("# Mocked provider\n")
        code, state, run = self.run_dispatch("--strict", payload={
            "considered": True, "available": True, "services": {"8000": {"url": "http://localhost:8000"}},
            "errors": [],
        })
        self.assertEqual(code, 0)
        self.assertEqual(Path(run.call_args.args[0][1]), script)
        self.assertEqual(state["available_providers"], ["caddy"])
        self.assertEqual(state["available_exports"], [])
        self.assertTrue((self.base / "CITADEL_DATA/caddy-routes.json").exists())

    def test_strict_propagates_exit_and_status_errors(self):
        for returncode, errors in ((1, []), (0, ["invalid mapping"])):
            with self.subTest(returncode=returncode):
                self.status["errors"] = errors
                code, state, _ = self.run_dispatch("--strict", returncode=returncode)
                self.assertEqual(code, 1)
                self.assertEqual(state["providers"]["caddy"]["status"], "error")
                self.assertTrue(state["errors"])

    def test_unconfigured_export_has_no_artifact_or_provider_metadata(self):
        self.status.update(considered=False, available=False, artifacts=[])
        self.status.pop("generated_file")
        code, state, _ = self.run_dispatch("--strict")
        self.assertEqual(code, 0)
        self.assertEqual(state["considered_exports"], [])
        self.assertEqual(state["available_providers"], [])
        self.assertFalse((self.base / "CITADEL_DATA/CADDY/Caddyfile").exists())

    def test_disabled_export_is_not_executed(self):
        self.manifest(enabled=False)
        code, state, run = self.run_dispatch("--strict")
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertEqual(state["providers"]["caddy"]["status"], "disabled")

    def test_unknown_implementation_and_kinds_fail_without_execution(self):
        cases = [{"provider": "does-not-exist"}] + [
            {"kind": value} for value in ("unknown", "../providers", "", None, ["export"])
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.manifest(**overrides)
                code, state, run = self.run_dispatch("--strict")
                self.assertEqual(code, 1)
                self.assertTrue(state["errors"])
                run.assert_not_called()

    def test_provider_implementation_traversal_is_rejected(self):
        for implementation in ("../providers/localhost", "/tmp/escape", "a/b", "..", "", None, {}):
            with self.subTest(implementation=implementation):
                self.manifest(provider=implementation)
                code, _, run = self.run_dispatch("--strict")
                self.assertEqual(code, 1)
                run.assert_not_called()

    def test_symlinked_export_script_cannot_escape_exporter_directory(self):
        target = self.base / "outside.py"
        target.write_text("# Must not run\n")
        self.stub.unlink()
        self.stub.symlink_to(target)
        code, _, run = self.run_dispatch("--strict")
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_output_symlink_cannot_escape_runtime_directory(self):
        outside = self.write("outside.json", {"untouched": True})
        (self.base / "CITADEL_DATA/caddy-status.json").symlink_to(outside)
        code, _, run = self.run_dispatch("--strict")
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertEqual(json.loads(outside.read_text()), {"untouched": True})

    def cloudflare_provider(self):
        self.manifest("cloudflare", kind="provider")
        script = self.base / "functions/providers/cloudflare.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# Mocked provider only\n")
        return {
            "considered": True, "available": True, "errors": [],
            "services": {"8000": {"url": "https://app.example.invalid"}},
        }

    def test_provider_writes_direct_ephemeral_cloudflare_file(self):
        payload = self.cloudflare_provider()
        code, _, run = self.run_dispatch("--provider", "cloudflare", "--strict", payload=payload)
        self.assertEqual(code, 0)
        output = self.base / "cache/cloudflare-routes.json"
        self.assertEqual(json.loads(output.read_text()), payload)
        self.assertFalse(output.is_symlink())
        self.assertFalse((self.base / "CITADEL_DATA/cloudflare-routes.json").exists())

    def test_provider_file_symlinks_are_rejected(self):
        payload = self.cloudflare_provider()
        output = self.base / "cache/cloudflare-routes.json"
        output.parent.mkdir(exist_ok=True)
        target = self.write("outside.json", {"untouched": True})
        for link in (str(target), "../outside.json", str(self.base)):
            with self.subTest(link=link):
                output.symlink_to(link)
                code, _, run = self.run_dispatch("--provider", "cloudflare", "--strict", payload=payload)
                self.assertEqual(code, 1)
                run.assert_not_called()
                self.assertEqual(json.loads(target.read_text()), {"untouched": True})
                output.unlink()

    def test_provider_extension_directory_and_script_guards_remain_enforced(self):
        payload = self.cloudflare_provider()
        directory = self.enabled / "cloudflare"
        moved = self.base / "moved-cloudflare"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        code, _, run = self.run_dispatch("--provider", "cloudflare", "--strict", payload=payload)
        self.assertEqual(code, 1)
        run.assert_not_called()
        directory.unlink()
        moved.rename(directory)
        script = self.base / "functions/providers/cloudflare.py"
        script.unlink()
        outside = self.base / "outside.py"
        outside.write_text("# Must not execute\n")
        script.symlink_to(outside)
        code, _, run = self.run_dispatch("--provider", "cloudflare", "--strict", payload=payload)
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_cloudflare_health_reads_ephemeral_output_without_mutation(self):
        payload = self.cloudflare_provider()
        self.write("CITADEL_DATA/services.json", {"http_services": [{
            "port": 8000, "name": "Example", "urls": {"cloudflare": "https://app.example.invalid"},
        }]})
        self.write("cache/8000.json", {"kind": "html"})
        code, _, _ = self.run_dispatch("--provider", "cloudflare", "--strict", payload=payload)
        self.assertEqual(code, 0)
        output = self.base / "cache/cloudflare-routes.json"
        before = (output.read_bytes(), output.stat().st_mtime_ns)
        result = health.snapshot(self.base, ["cloudflare"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(before, (output.read_bytes(), output.stat().st_mtime_ns))

    def test_malformed_export_output_fails_strict_dispatch(self):
        for payload in ({}, [], {**self.status, "kind": "provider"},
                        {**self.status, "services": {"8000": {"url": "http://invalid"}}}):
            with self.subTest(payload=payload):
                code, state, _ = self.run_dispatch("--strict", payload=payload)
                self.assertEqual(code, 1)
                self.assertEqual(state["providers"]["caddy"]["status"], "error")


class ExportDashboardTests(ExportFixture):
    def dashboard(self):
        with patch.multiple(core, BASE_DIR=self.base, ENABLED_EXT_DIR=self.enabled,
                            PROVIDERS_STATE_FILE=self.base / "CITADEL_DATA/providers_state.json",
                            SERVICES_FILE=self.base / "CITADEL_DATA/services.json", LAST_SCAN_FILE=self.base / "CITADEL_DATA/last_scan.txt",
                            UI_CONFIG_FILE=self.base / "extensions/ui.json", PORT_FILTER_FILE=self.base / "CITADEL_DATA/ports.filter.json"):
            return core.build_dashboard(), core._load_providers()

    def test_successful_exports_never_enter_dropdown_headers_or_urls(self):
        status = self.export()
        self.provider()
        for name in ("caddy", "tailscale", "cloudflare"):
            self.manifest(name)
            self.write(f"extensions/enabled/{name}/status.json", status)
            self.write(f"extensions/enabled/{name}/routes.json", {
                "label": "Old export route", "considered": True, "available": True,
                "domain": "export.invalid", "services": {"8000": {"url": "https://export.invalid"}},
            })
        self.write("CITADEL_DATA/providers_state.json", {
            "considered_providers": ["localhost", "caddy", "tailscale", "cloudflare"],
            "available_providers": ["localhost", "caddy", "tailscale", "cloudflare"],
        })
        self.write("extensions/ui.json", {"default_provider": "caddy"})
        dashboard, providers = self.dashboard()
        self.assertEqual(dashboard["provider_options"], {"localhost": "Localhost"})
        self.assertEqual(dashboard["provider_order"], ["localhost"])
        self.assertEqual(dashboard["default_mode"], "localhost")
        self.assertEqual(dashboard["provider_header_meta"], [{"label": "Localhost", "value": "127.0.0.1"}])
        self.assertEqual(providers["provider_urls_by_port"], {"localhost": {"8000": "http://localhost:8000"}})
        self.assertEqual(dashboard["http_tiles"][0]["provider_urls"], {"localhost": "http://localhost:8000"})
        self.assertFalse(dashboard["cloudflare_available"])

    def test_export_errors_remain_visible_without_url_options(self):
        self.export(configured=False, errors=["invalid hostname"])
        self.write("CITADEL_DATA/providers_state.json", {"errors": ["Caddy process failed"]})
        dashboard, _ = self.dashboard()
        self.assertEqual(dashboard["provider_options"], {})
        self.assertIn("[caddy] invalid hostname", dashboard["alerts"])
        self.assertIn("[dispatch] Caddy process failed", dashboard["alerts"])


class ExportHealthTests(ExportFixture):
    def result(self):
        return health.snapshot(self.base, ["caddy"])

    def test_successful_export_is_read_only_and_has_no_routes(self):
        self.export()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob("*") if p.is_file()}
        result = self.result()
        self.assertEqual(result["status"], "PASS")
        entry = result["extensions"][0]
        self.assertEqual(entry["kind"], "export")
        self.assertEqual(entry["services"], [])
        self.assertEqual(entry["artifacts"][0]["status"], "PASS")
        self.assertEqual(entry["artifacts"][0]["sha256"], entry["artifacts"][0]["actual_sha256"])
        self.assertIn("running Caddy", entry["detail"])
        self.assertIn("NOT_TESTED", entry["detail"])
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_empty_caddyfile_and_zero_mappings_pass(self):
        self.export(content=b"", mappings_count=0)
        result = self.result()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["extensions"][0]["mappings_count"], 0)

    def test_unconfigured_export_skips_without_artifacts(self):
        self.export(configured=False)
        entry = self.result()["extensions"][0]
        self.assertEqual(entry["status"], "SKIP")
        self.assertEqual(entry["artifacts"], [])
        self.assertFalse((self.base / "CITADEL_DATA/CADDY/Caddyfile").exists())

    def test_disabled_or_absent_export_skips_without_status_file(self):
        self.manifest(enabled=False)
        self.assertEqual(self.result()["extensions"][0]["status"], "SKIP")
        self.assertEqual(health.snapshot(self.base, ["absent"])["extensions"][0]["status"], "SKIP")

    def test_export_failure_is_not_skip(self):
        self.export(configured=False, errors=["invalid configuration"])
        self.assertEqual(self.result()["extensions"][0]["status"], "FAIL")
        self.export(configured=False)
        self.write("CITADEL_DATA/providers_state.json", {"providers": {"caddy": {"status": "error"}}})
        self.assertEqual(self.result()["extensions"][0]["status"], "FAIL")

    def test_hash_mismatch_fails(self):
        self.export()
        (self.base / "CITADEL_DATA/CADDY/Caddyfile").write_text("changed")
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("mismatch", result["extensions"][0]["artifacts"][0]["detail"])

    def test_missing_or_directory_artifact_fails(self):
        self.export()
        artifact = self.base / "CITADEL_DATA/CADDY/Caddyfile"
        artifact.unlink()
        self.assertEqual(self.result()["status"], "FAIL")
        artifact.mkdir()
        self.assertEqual(self.result()["status"], "FAIL")

    def test_artifact_path_traversal_and_absolute_paths_fail(self):
        for path in ("../Caddyfile", "CITADEL_DATA/CADDY/../CADDY/Caddyfile", str(self.base / "CITADEL_DATA/CADDY/Caddyfile"),
                     "..\\Caddyfile", "", "bad\x00path"):
            with self.subTest(path=path):
                self.export(artifacts=[{"path": path, "sha256": hashlib.sha256(b"").hexdigest()}])
                self.assertEqual(self.result()["status"], "FAIL")

    def test_artifact_symlink_cannot_escape_repository(self):
        self.export()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "Caddyfile"
            target.write_bytes(b"")
            artifact = self.base / "CITADEL_DATA/CADDY/Caddyfile"
            artifact.unlink()
            artifact.symlink_to(target)
            result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("escapes", result["extensions"][0]["artifacts"][0]["detail"])

    def test_caddy_output_directory_cannot_escape_through_a_link(self):
        self.export()
        with tempfile.TemporaryDirectory() as shared:
            original = self.base / "CITADEL_DATA/CADDY"
            target = Path(shared) / "CITADEL_DATA/CADDY"
            target.parent.mkdir()
            original.rename(target)
            original.symlink_to(target, target_is_directory=True)
            self.assertEqual(self.result()["status"], "FAIL")
            outside = Path(shared) / "unrelated"
            outside.write_text("not exported")
            (target / "Caddyfile").unlink()
            (target / "Caddyfile").symlink_to(outside)
            self.assertEqual(self.result()["status"], "FAIL")

    def test_invalid_or_empty_artifact_metadata_fails(self):
        for artifacts in ([], None, {}, [None], [{}], [{"path": "CITADEL_DATA/CADDY/Caddyfile", "sha256": "bad"}]):
            with self.subTest(artifacts=artifacts):
                self.export(artifacts=artifacts)
                self.assertEqual(self.result()["status"], "FAIL")

    def test_all_artifacts_must_pass(self):
        status = self.export()
        status["artifacts"].append({"path": "missing.txt", "sha256": hashlib.sha256(b"").hexdigest()})
        self.write("CITADEL_DATA/caddy-status.json", status)
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual([a["status"] for a in result["extensions"][0]["artifacts"]], ["PASS", "FAIL"])

    def test_routes_file_cannot_substitute_for_export_status(self):
        status = self.export()
        (self.base / "CITADEL_DATA/caddy-status.json").unlink()
        self.write("CITADEL_DATA/caddy-routes.json", status)
        self.assertEqual(self.result()["status"], "FAIL")

    def test_invalid_status_or_unknown_kind_fails(self):
        for overrides in ({"available": False}, {"kind": "provider"}, {"services": {"8000": {}}}):
            with self.subTest(overrides=overrides):
                self.export(**overrides)
                self.assertEqual(self.result()["status"], "FAIL")
        self.manifest(kind="unknown")
        self.assertEqual(self.result()["status"], "FAIL")


class ExportCheckerTests(ExportFixture):
    def responses(self, data, *extra):
        return [(200, "https://citadel.invalid/", b"ok"),
                (200, "https://citadel.invalid/api/health", json.dumps(data).encode()), *extra]

    def test_export_pass_needs_no_network_jobs(self):
        self.export(content=b"", mappings_count=0)
        data = health.snapshot(self.base, ["caddy"])
        with (patch.object(checker, "fetch", side_effect=self.responses(data)) as fetch,
              patch.object(checker, "probe") as probe,
              patch.object(checker, "ThreadPoolExecutor") as pool):
            result = checker.check("https://citadel.invalid", ["caddy"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["extensions"][0]["kind"], "export")
        self.assertEqual(result["extensions"][0]["services"], [])
        self.assertIn("NOT_TESTED", result["extensions"][0]["detail"])
        self.assertEqual(fetch.call_count, 2)
        probe.assert_not_called()
        pool.assert_not_called()

    def test_cli_exits_successfully_for_export_without_routes(self):
        self.export()
        data = health.snapshot(self.base, ["caddy"])
        stdout = io.StringIO()
        with (patch.object(sys, "argv", ["check.py", "--url", "https://citadel.invalid", "--extensions", "caddy"]),
              patch.object(checker, "fetch", side_effect=self.responses(data)), redirect_stdout(stdout)):
            self.assertEqual(checker.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "PASS")

    def test_mixed_extensions_probe_only_url_provider(self):
        self.export()
        self.provider()
        data = health.snapshot(self.base, ["caddy", "localhost"])
        with patch.object(checker, "fetch", side_effect=self.responses(data, (200, "http://localhost:8000", b"ok"))) as fetch:
            result = checker.check("https://citadel.invalid", ["caddy", "localhost"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(fetch.call_args.args[0], "http://localhost:8000")
        self.assertEqual([r["extension"] for r in result["results"]], ["localhost"])

    def test_export_cannot_hide_routes_or_failed_artifacts_under_pass(self):
        for overrides in ({"services": [{"url": "https://export.invalid", "port": 80, "status": "PASS"}]},
                          {"artifacts": []}, {"artifacts": [{"status": "FAIL"}]}, {"kind": "unknown"}):
            with self.subTest(overrides=overrides):
                self.export()
                data = health.snapshot(self.base, ["caddy"])
                data["extensions"][0].update(overrides)
                with (patch.object(checker, "fetch", side_effect=self.responses(data)) as fetch,
                      patch.object(checker, "probe") as probe):
                    result = checker.check("https://citadel.invalid", ["caddy"])
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(fetch.call_count, 2)
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
