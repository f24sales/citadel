from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "functions"))
sys.path.insert(0, str(ROOT / "functions" / "providers"))

from cloudflare_policy import (  # noqa: E402
    cloudflare_rules,
    default_subdomains,
    normalize_rule,
    resolve_hostname,
    write_cloudflare_rules,
)
from cloudflare import (  # noqa: E402
    access_policy_payload,
    adopt_matching_ingress,
    ensure_one_time_pin,
    one_time_pin_enabled,
    reconcile_access,
    remove_managed_ingress,
)
from cloudflare_api import CloudflareAPI, CloudflareAPIError  # noqa: E402
import core  # noqa: E402
import cloudflare_defaults  # noqa: E402
import cloudflare as cloudflare_provider  # noqa: E402
import dispatch  # noqa: E402
import health  # noqa: E402


class CloudflarePolicyTests(unittest.TestCase):
    def test_resolves_label_default_and_full_hostname(self) -> None:
        self.assertEqual(
            resolve_hostname(399, "", "services.example.net", "example.net"),
            "399.services.example.net",
        )
        self.assertEqual(
            resolve_hostname(399, "citadel", "services.example.net", "example.net"),
            "citadel.services.example.net",
        )
        self.assertEqual(
            resolve_hostname(399, "citadel.example.net", "services.example.net", "example.net"),
            "citadel.example.net",
        )

    def test_rejects_hostname_outside_zone(self) -> None:
        with self.assertRaises(ValueError):
            resolve_hostname(399, "citadel.example.org", "services.example.net", "example.net")

    def test_whitelist_requires_email(self) -> None:
        with self.assertRaises(ValueError):
            normalize_rule({"whitelist": True, "emails": []})

    def test_normalizes_csv_aliases_and_legacy_subdomain(self) -> None:
        self.assertEqual(
            normalize_rule({"subdomains": "399, Citadel"}, port=399)["subdomains"],
            ["399", "citadel"],
        )
        self.assertEqual(
            normalize_rule({"subdomain": "Legacy"}, port=399)["subdomains"],
            ["legacy"],
        )
        self.assertEqual(normalize_rule({}, port=399)["subdomains"], ["399"])
        with self.assertRaises(ValueError):
            normalize_rule({"subdomains": "399,399"}, port=399)

    def test_https_hostname_switches_are_independent_and_generic(self) -> None:
        settings = {
            "CITADEL_CLOUDFLARE_WWW443": "1",
            "CITADEL_CLOUDFLARE_DOMAIN443": "1",
        }
        with patch.dict(os.environ, settings):
            self.assertEqual(default_subdomains(443), ["www", "domain"])
            self.assertEqual(normalize_rule({"subdomains": ["443"]}, port=443)["subdomains"], ["www", "domain"])
            self.assertEqual(
                resolve_hostname(443, "domain", "f24-sales.com", "f24-sales.com"),
                "f24-sales.com",
            )
        with patch.dict(os.environ, {"CITADEL_CLOUDFLARE_WWW443": "0", "CITADEL_CLOUDFLARE_DOMAIN443": "1"}):
            self.assertEqual(default_subdomains(443), ["domain"])
        with patch.dict(os.environ, {"CITADEL_CLOUDFLARE_WWW443": "0", "CITADEL_CLOUDFLARE_DOMAIN443": "0"}):
            self.assertEqual(default_subdomains(443), ["443"])

    def test_strict_policy_rejects_invalid_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ports.filter.json"
            path.write_text(
                json.dumps({"cloudflare": {"399": {"whitelist": True, "emails": []}}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                cloudflare_rules(path, strict=True)

    def test_policy_round_trip_preserves_global_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ports.filter.json"
            path.write_text(
                json.dumps({"whitelist": [399], "blacklist": [400]}),
                encoding="utf-8",
            )
            write_cloudflare_rules(
                path,
                {
                    "399": {
                        "subdomains": ["399", "citadel"],
                        "whitelist": True,
                        "emails": ["USER@example.net", "user@example.net"],
                    }
                },
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["whitelist"], [399])
            self.assertEqual(payload["blacklist"], [400])
            self.assertEqual(cloudflare_rules(path)["399"]["subdomains"], ["399", "citadel"])
            self.assertEqual(
                cloudflare_rules(path)["399"]["emails"],
                ["user@example.net"],
            )


class DashboardCoreTests(unittest.TestCase):
    def test_citadel_service_is_featured_and_sorted_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            enabled = base / "extensions" / "enabled"
            localhost = enabled / "localhost"
            localhost.mkdir(parents=True)
            (base / "services.json").write_text(
                json.dumps({
                    "http_services": [
                        {"port": 11000, "name": "CODEANALYST"},
                        {"port": 10999, "name": "CITADEL"},
                    ],
                    "other_ports": [],
                }),
                encoding="utf-8",
            )
            (localhost / "extension.json").write_text(
                json.dumps({"label": "Localhost"}),
                encoding="utf-8",
            )
            (localhost / "routes.json").write_text(
                json.dumps({
                    "considered": True,
                    "available": True,
                    "services": {
                        "10999": {
                            "mode": "direct",
                            "url": "http://127.0.0.1:10999",
                            "target": None,
                            "owns_listener": False,
                        },
                        "11000": {
                            "mode": "direct",
                            "url": "http://127.0.0.1:11000",
                            "target": None,
                            "owns_listener": False,
                        },
                    },
                }),
                encoding="utf-8",
            )
            (base / "extensions" / "providers_state.json").write_text(
                json.dumps({
                    "considered_providers": ["localhost"],
                    "available_providers": ["localhost"],
                    "errors": [],
                }),
                encoding="utf-8",
            )
            (base / "ports.filter.json").write_text(
                json.dumps({"cloudflare": {}}),
                encoding="utf-8",
            )

            original = {
                "SERVICES_FILE": core.SERVICES_FILE,
                "LAST_SCAN_FILE": core.LAST_SCAN_FILE,
                "ENABLED_EXT_DIR": core.ENABLED_EXT_DIR,
                "PROVIDERS_STATE_FILE": core.PROVIDERS_STATE_FILE,
                "UI_CONFIG_FILE": core.UI_CONFIG_FILE,
                "PORT_FILTER_FILE": core.PORT_FILTER_FILE,
            }
            old_port = os.environ.get("CITADEL_WEBUI_PORT")
            try:
                core.SERVICES_FILE = base / "services.json"
                core.LAST_SCAN_FILE = base / "last_scan.txt"
                core.ENABLED_EXT_DIR = enabled
                core.PROVIDERS_STATE_FILE = base / "extensions" / "providers_state.json"
                core.UI_CONFIG_FILE = base / "extensions" / "ui.json"
                core.PORT_FILTER_FILE = base / "ports.filter.json"
                os.environ["CITADEL_WEBUI_PORT"] = "10999"
                dashboard = core.build_dashboard()
            finally:
                for name, value in original.items():
                    setattr(core, name, value)
                if old_port is None:
                    os.environ.pop("CITADEL_WEBUI_PORT", None)
                else:
                    os.environ["CITADEL_WEBUI_PORT"] = old_port

            self.assertEqual([item["port"] for item in dashboard["http_tiles"]], [10999, 11000])
            self.assertTrue(dashboard["http_tiles"][0]["featured"])
            self.assertEqual(dashboard["http_tiles"][0]["display_name"], "⭐ CITADEL ⭐")

    def test_stale_host_discovery_is_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "services.json").write_text(
                json.dumps({"http_services": [{"port": 11000}], "other_ports": []}),
                encoding="utf-8",
            )
            (base / "host_services.json").write_text(
                json.dumps({
                    "host_http_services": [{
                        "port": 8080,
                        "origin": "host",
                        "origin_port": 8080,
                        "route_port": None,
                        "scheme": "http",
                    }],
                    "host_other_ports": [{"port": 5432, "service": "postgres"}],
                    "deduplicated_ports": [],
                }),
                encoding="utf-8",
            )
            original_services = core.SERVICES_FILE
            original_enabled = core.ENABLED_EXT_DIR
            try:
                core.SERVICES_FILE = base / "services.json"
                core.ENABLED_EXT_DIR = base / "extensions" / "enabled"
                dashboard = core.build_dashboard()
            finally:
                core.SERVICES_FILE = original_services
                core.ENABLED_EXT_DIR = original_enabled

            self.assertEqual([item["port"] for item in dashboard["http_tiles"]], [11000])
            self.assertNotIn("host_listeners", dashboard)
            self.assertNotIn("deduplicated_ports", dashboard)


class CloudflareDefaultsTests(unittest.TestCase):
    class TTYInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    def run_defaults(self, base: Path, stdin: str = "", email: str = "Admin@Example.com, ops@example.com") -> int:
        old_cloudflare_ready = cloudflare_defaults.cloudflare_ready
        old_project_get = cloudflare_defaults.project_get
        old_argv = sys.argv
        old_stdin = sys.stdin
        try:
            cloudflare_defaults.cloudflare_ready = lambda _root: (True, "test")
            cloudflare_defaults.project_get = lambda _root, key, default="": email if key == "CLOUDFLARE_EMAIL" else default
            sys.argv = [
                "cloudflare_defaults.py",
                "--root",
                str(base),
                "--services-file",
                str(base / "services.json"),
                "--policy-file",
                str(base / "ports.filter.json"),
            ]
            sys.stdin = self.TTYInput(stdin)
            return cloudflare_defaults.main()
        finally:
            cloudflare_defaults.cloudflare_ready = old_cloudflare_ready
            cloudflare_defaults.project_get = old_project_get
            sys.argv = old_argv
            sys.stdin = old_stdin

    def test_env_email_defaults_protect_new_ports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "services.json").write_text(
                json.dumps({"http_services": [{"port": 12001}, {"port": 12002}]}),
                encoding="utf-8",
            )
            (base / "ports.filter.json").write_text(
                json.dumps({
                    "whitelist": [],
                    "blacklist": [],
                    "cloudflare": {
                        "12001": {
                            "subdomains": ["custom"],
                            "whitelist": False,
                            "emails": [],
                        }
                    },
                }),
                encoding="utf-8",
            )
            self.assertEqual(self.run_defaults(base), 0)
            policy = json.loads((base / "ports.filter.json").read_text(encoding="utf-8"))
            self.assertEqual(policy["cloudflare_defaults"]["emails"], ["admin@example.com", "ops@example.com"])
            self.assertEqual(policy["cloudflare"]["12001"]["subdomains"], ["custom"])
            self.assertFalse(policy["cloudflare"]["12001"]["whitelist"])
            self.assertEqual(policy["cloudflare"]["12002"]["subdomains"], ["12002"])
            self.assertTrue(policy["cloudflare"]["12002"]["whitelist"])
            self.assertEqual(policy["cloudflare"]["12002"]["emails"], ["admin@example.com", "ops@example.com"])

    def test_missing_env_email_skips_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "services.json").write_text(
                json.dumps({"http_services": [{"port": 399}]}),
                encoding="utf-8",
            )
            (base / "ports.filter.json").write_text(
                json.dumps({"whitelist": [], "blacklist": [], "cloudflare": {}}),
                encoding="utf-8",
            )
            self.assertEqual(self.run_defaults(base, email=""), 0)
            policy = json.loads((base / "ports.filter.json").read_text(encoding="utf-8"))
            self.assertNotIn("cloudflare_defaults", policy)
            self.assertEqual(policy["cloudflare"], {})


class CloudflareActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        self.addCleanup(sys.path.__setitem__, slice(None), list(sys.path))
        self.base = Path(temporary.name)
        self.provider_dir = self.base / "extensions/enabled/cloudflare"
        self.manifest()
        self.write("services.json", {"http_services": [{"port": 8000, "scheme": "http"}]})
        self.write("cache/8000.json", {"kind": "html"})
        self.write("extensions/providers_state.json", {"providers": {"cloudflare": {"status": "ok"}}})
        (self.base / "last_scan.txt").write_text("2026-10-02T12:00:00Z")
        self.settings = {
            "CLOUDFLARE_API_TOKEN": "unit-test-token",
            "CITADEL_CLOUDFLARE_DOMAIN": "services.example.net",
            "CITADEL_CLOUDFLARE_ACCOUNT_ID": "account",
            "CITADEL_CLOUDFLARE_ZONE_ID": "zone",
            "CITADEL_CLOUDFLARE_TUNNEL_ID": "tunnel",
        }
        self.getter = Mock(side_effect=lambda key, default="": self.settings.get(key, default))
        self.api = create_autospec(CloudflareAPI, instance=True)
        self.api.verify_token.return_value = None
        self.api.tunnel_connections.return_value = [{"id": "connection"}]
        self.api.zone.return_value = {"name": "example.net"}
        self.api.tunnel_configuration.return_value = {"ingress": [{"service": "http_status:404"}]}
        self.api.access_apps.return_value = []
        self.api.access_policies.return_value = []
        self.api.ensure_tunnel_dns.return_value = "dns-record"
        factory = patch.object(cloudflare_provider, "CloudflareAPI", return_value=self.api)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        for target in ("subprocess.run", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("Live processes/network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def write(self, name, payload):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def manifest(self, enabled=True):
        self.write("extensions/enabled/cloudflare/extension.json", {
            "provider": "cloudflare", "label": "Cloudflare", "enabled": enabled,
        })

    def run_provider(self):
        output = self.provider_dir / "routes.json"
        with (patch.object(cloudflare_provider, "load_project_getter", return_value=self.getter),
              patch.object(sys, "argv", ["cloudflare.py", "--provider-dir", str(self.provider_dir),
                                         "--routes-out", str(output), "--services-file", str(self.base / "services.json")])):
            code = cloudflare_provider.main()
        return code, json.loads(output.read_text())

    def ready(self):
        with patch.dict(sys.modules, {
            "python_header": SimpleNamespace(get=self.getter),
            "cloudflare_api": SimpleNamespace(CloudflareAPI=self.factory),
        }):
            return cloudflare_defaults.cloudflare_ready(self.base)

    def assert_no_remote_mutations(self):
        mutations = [call for call in self.api.method_calls
                     if call[0].startswith(("create_", "update_", "delete_", "ensure_"))]
        self.assertEqual(mutations, [])

    def test_valid_token_activates_without_legacy_toggle_or_global_email(self):
        code, payload = self.run_provider()
        self.assertEqual(code, 0)
        self.assertTrue(payload["considered"])
        self.assertTrue(payload["available"])
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["services"]["8000"]["url"], "https://8000.services.example.net")
        self.api.verify_token.assert_called_once_with()
        self.api.tunnel_connections.assert_called_once_with("account", "tunnel")
        self.api.zone.assert_called_once_with("zone")

    def test_legacy_toggle_values_are_never_read_or_honored(self):
        for value in ("0", "false", "1", "true", "invalid", ""):
            with self.subTest(value=value):
                self.settings["CITADEL_CLOUDFLARE"] = value
                code, payload = self.run_provider()
                self.assertEqual(code, 0)
                self.assertTrue(payload["considered"])
                self.assertTrue(self.ready()[0])
        self.assertNotIn("CITADEL_CLOUDFLARE", [call.args[0] for call in self.getter.call_args_list])

    def test_production_sources_no_longer_reference_activation_toggle(self):
        for path in (Path(cloudflare_provider.__file__), Path(cloudflare_defaults.__file__)):
            with self.subTest(path=path):
                self.assertNotIn('"CITADEL_CLOUDFLARE"', path.read_text())
                self.assertNotIn("'CITADEL_CLOUDFLARE'", path.read_text())

    def test_missing_or_blank_token_skips_provider_and_defaults(self):
        self.settings["CITADEL_CLOUDFLARE"] = "1"
        for token in (None, "", " \t\n"):
            with self.subTest(token=token):
                if token is None:
                    self.settings.pop("CLOUDFLARE_API_TOKEN", None)
                else:
                    self.settings["CLOUDFLARE_API_TOKEN"] = token
                code, payload = self.run_provider()
                self.assertEqual(code, 0)
                self.assertFalse(payload["considered"])
                self.assertFalse(payload["available"])
                self.assertEqual(payload["errors"], [])
                self.assertEqual(payload["services"], {})
                self.assertEqual(health.snapshot(self.base, ["cloudflare"])["extensions"][0]["status"], "SKIP")
                self.assertEqual(self.ready(), (False, "CLOUDFLARE_API_TOKEN is missing"))
        self.factory.assert_not_called()

    def test_invalid_token_fails_without_resource_changes(self):
        self.api.verify_token.side_effect = CloudflareAPIError("invalid token")
        code, payload = self.run_provider()
        self.assertEqual(code, 1)
        self.assertFalse(payload["considered"])
        self.assertFalse(payload["authenticated"])
        self.assertEqual(payload["errors"], ["invalid token"])
        self.api.tunnel_connections.assert_not_called()
        self.api.zone.assert_not_called()
        self.assertEqual(self.ready(), (False, "invalid token"))
        self.assert_no_remote_mutations()

    def test_manifest_disabled_skips_even_with_token_and_old_toggle(self):
        self.manifest(enabled=False)
        self.settings["CITADEL_CLOUDFLARE"] = "1"
        code, payload = self.run_provider()
        self.assertEqual(code, 0)
        self.assertFalse(payload["considered"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(self.ready(), (False, "provider is disabled"))
        self.assertEqual(health.snapshot(self.base, ["cloudflare"])["extensions"][0]["status"], "SKIP")
        self.factory.assert_not_called()

    def test_disabled_folder_skips_even_with_valid_token(self):
        disabled = self.base / "extensions/disabled/cloudflare"
        disabled.parent.mkdir()
        self.provider_dir.rename(disabled)
        self.provider_dir = disabled
        code, payload = self.run_provider()
        self.assertEqual(code, 0)
        self.assertFalse(payload["considered"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(self.ready(), (False, "provider is disabled"))
        self.assertEqual(health.snapshot(self.base, ["cloudflare"])["extensions"][0]["status"], "SKIP")
        self.factory.assert_not_called()

    def test_dispatch_does_not_execute_disabled_manifest(self):
        self.manifest(enabled=False)
        with (patch.object(sys, "argv", [
                "dispatch.py", "--enabled-dir", str(self.base / "extensions/enabled"),
                "--services-file", str(self.base / "services.json"), "--cache-dir", str(self.base / "cache"),
                "--config-ini", str(self.base / "config.ini"),
                "--state-file", str(self.base / "extensions/providers_state.json"),
                "--tailscale-file", str(self.base / "tailscale.json"), "--provider", "cloudflare", "--strict",
              ]), patch.object(dispatch.subprocess, "run") as run):
            self.assertEqual(dispatch.main(), 0)
        run.assert_not_called()
        self.factory.assert_not_called()

    def test_valid_token_is_considered_but_missing_required_settings_fail(self):
        for key in ("CITADEL_CLOUDFLARE_DOMAIN", "CITADEL_CLOUDFLARE_ACCOUNT_ID",
                    "CITADEL_CLOUDFLARE_ZONE_ID", "CITADEL_CLOUDFLARE_TUNNEL_ID"):
            with self.subTest(key=key):
                saved = self.settings.pop(key)
                code, payload = self.run_provider()
                self.settings[key] = saved
                self.assertEqual(code, 1)
                self.assertTrue(payload["considered"])
                self.assertFalse(payload["available"])
                self.assertIn(key, payload["errors"][0])
        self.api.tunnel_connections.assert_not_called()
        self.assert_no_remote_mutations()

    def test_account_or_tunnel_failure_remains_error_after_token_verification(self):
        self.api.tunnel_connections.side_effect = CloudflareAPIError("account/tunnel unavailable")
        code, payload = self.run_provider()
        self.assertEqual(code, 1)
        self.assertTrue(payload["considered"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["errors"], ["account/tunnel unavailable"])
        self.api.zone.assert_not_called()
        self.assert_no_remote_mutations()

    def test_zone_domain_validation_precedes_any_resource_mutation(self):
        for zone in ({}, {"name": "other.example.org"}):
            with self.subTest(zone=zone):
                self.api.zone.return_value = zone
                code, payload = self.run_provider()
                self.assertEqual(code, 1)
                self.assertTrue(payload["considered"])
                self.assertFalse(payload["available"])
                self.assertTrue(payload["errors"])
        self.api.tunnel_configuration.assert_not_called()
        self.assert_no_remote_mutations()

    def test_invalid_origin_or_whitelist_still_prevents_resource_mutations(self):
        self.settings["CITADEL_SUBNET_IP"] = "invalid/origin"
        code, payload = self.run_provider()
        self.assertEqual(code, 1)
        self.assertTrue(payload["considered"])
        self.assertIn("CITADEL_SUBNET_IP", payload["errors"][0])
        self.settings.pop("CITADEL_SUBNET_IP")
        self.write("ports.filter.json", {"cloudflare": {"8000": {"whitelist": True, "emails": []}}})
        code, payload = self.run_provider()
        self.assertEqual(code, 1)
        self.assertTrue(payload["considered"])
        self.assertTrue(payload["errors"])
        self.assert_no_remote_mutations()

    def test_readiness_only_verifies_trimmed_token(self):
        self.settings["CLOUDFLARE_API_TOKEN"] = "  unit-test-token  "
        self.assertEqual(self.ready(), (True, "API token verified"))
        self.factory.assert_called_once_with("unit-test-token")
        self.api.verify_token.assert_called_once_with()
        self.assertEqual([call[0] for call in self.api.method_calls], ["verify_token"])


class CloudflareProviderTests(unittest.TestCase):
    def test_preserves_foreign_ingress_and_keeps_fallback(self) -> None:
        config = {
            "ingress": [
                {"hostname": "foreign.example.net", "service": "http://127.0.0.1:1"},
                {"hostname": "399.example.net", "service": "http://127.0.0.1:399"},
                {"service": "http_status:404"},
            ]
        }
        preserved, fallback = remove_managed_ingress(config, {"399.example.net"})
        self.assertEqual([item.get("hostname") for item in preserved], ["foreign.example.net"])
        self.assertEqual(fallback, {"service": "http_status:404"})

    def test_replaces_unmanaged_ingress_for_desired_hostname(self) -> None:
        config = {
            "ingress": [
                {"hostname": "399.example.net", "service": "http://other:399"},
                {"hostname": "foreign.example.net", "service": "http://other:400"},
                {"service": "http_status:404"},
            ]
        }
        preserved, fallback = remove_managed_ingress(config, {"399.example.net"})
        self.assertEqual(
            preserved,
            [{"hostname": "foreign.example.net", "service": "http://other:400"}],
        )
        self.assertEqual(fallback, {"service": "http_status:404"})

    def test_adopts_only_ingress_with_exact_origin(self) -> None:
        config = {
            "ingress": [
                {"hostname": "399.example.net", "service": "http://127.0.0.1:399"},
                {"hostname": "400.example.net", "service": "http://other:400"},
                {"service": "http_status:404"},
            ]
        }
        desired = {
            "399.example.net": {"scheme": "http", "port": 399},
            "400.example.net": {"scheme": "http", "port": 400},
        }
        self.assertEqual(
            adopt_matching_ingress(config, desired, set(), "127.0.0.1"),
            {"399.example.net"},
        )

    def test_access_policy_uses_exact_email_rules(self) -> None:
        payload = access_policy_payload(
            "399.example.net",
            ["one@example.net", "two@example.net"],
        )
        self.assertEqual(payload["decision"], "allow")
        self.assertEqual(
            payload["include"],
            [
                {"email": {"email": "one@example.net"}},
                {"email": {"email": "two@example.net"}},
            ],
        )

    def test_one_time_pin_detection_is_exact(self) -> None:
        self.assertTrue(one_time_pin_enabled([{"type": "onetimepin"}]))
        self.assertFalse(one_time_pin_enabled([{"type": "google"}]))

    def test_access_initialization_creates_organization_and_otp(self) -> None:
        class FakeAPI:
            def access_organization(self, _account_id):
                raise CloudflareAPIError("not initialized")

            def create_access_organization(self, account_id, auth_domain, name):
                self.organization = (account_id, auth_domain, name)
                return {"auth_domain": auth_domain, "name": name}

            def access_identity_providers(self, _account_id):
                return []

            def create_access_identity_provider(self, account_id, payload):
                self.provider = (account_id, payload)
                return {"id": "otp", **payload}

        api = FakeAPI()
        providers = ensure_one_time_pin(api, "a" * 32, "example.net")
        self.assertEqual(api.organization[1], "citadel-aaaaaaaaaaaa.cloudflareaccess.com")
        self.assertEqual(api.organization[2], "example.net")
        self.assertEqual(api.provider[1]["type"], "onetimepin")
        self.assertTrue(one_time_pin_enabled(providers))

    def test_access_initialization_reuses_existing_otp(self) -> None:
        class FakeAPI:
            def access_organization(self, _account_id):
                return {"auth_domain": "existing.cloudflareaccess.com"}

            def access_identity_providers(self, _account_id):
                return [{"id": "otp", "type": "onetimepin"}]

        self.assertTrue(one_time_pin_enabled(ensure_one_time_pin(FakeAPI(), "account", "example.net")))

    def test_access_refuses_unmanaged_application(self) -> None:
        class FakeAPI:
            def access_apps(self, _account_id):
                return [{"id": "foreign", "domain": "399.example.net", "name": "Foreign"}]

            def access_policies(self, _account_id):
                return []

        with self.assertRaises(CloudflareAPIError):
            reconcile_access(
                FakeAPI(),
                "account",
                {
                    "399.example.net": {
                        "whitelist": True,
                        "emails": ["user@example.net"],
                    }
                },
                {},
                {},
                {},
                {},
            )

    def test_access_adopts_exact_citadel_names(self) -> None:
        class FakeAPI:
            def access_apps(self, _account_id):
                return [{"id": "app", "domain": "399.example.net", "name": "CITADEL 399.example.net"}]

            def access_policies(self, _account_id):
                return [{"id": "policy", "name": "CITADEL email whitelist 399.example.net"}]

            def update_access_policy(self, _account_id, policy_id, _payload):
                self.policy_id = policy_id

            def update_access_app(self, _account_id, app_id, _payload):
                self.app_id = app_id

        api = FakeAPI()
        apps, policies = reconcile_access(
            api,
            "account",
            {"399.example.net": {"whitelist": True, "emails": ["user@example.net"]}},
            {},
            {},
            {},
            {},
        )
        self.assertEqual(apps, {"399.example.net": "app"})
        self.assertEqual(policies, {"399.example.net": "policy"})
        self.assertEqual(api.app_id, "app")
        self.assertEqual(api.policy_id, "policy")

    def test_dns_refuses_unmanaged_record(self) -> None:
        api = CloudflareAPI("token")

        def request(method, _path, **_kwargs):
            if method == "GET":
                return [{"id": "foreign", "type": "CNAME"}]
            return {}

        api.request = request
        with self.assertRaises(CloudflareAPIError):
            api.ensure_tunnel_dns("zone", "399.example.net", "tunnel")

    def test_dns_adopts_cname_for_same_tunnel(self) -> None:
        api = CloudflareAPI("token")
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                return [{
                    "id": "record",
                    "type": "CNAME",
                    "content": "tunnel.cfargotunnel.com",
                    "proxied": True,
                }]
            return {}

        api.request = request
        self.assertEqual(
            api.ensure_tunnel_dns("zone", "399.example.net", "tunnel"),
            "record",
        )
        self.assertEqual(calls[-1][0], "PUT")

    def test_dns_ignores_mail_records_when_creating_apex_cname(self) -> None:
        api = CloudflareAPI("token")
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                return [
                    {"id": "mx", "type": "MX", "content": "mail.example.net"},
                    {"id": "txt", "type": "TXT", "content": "v=spf1"},
                ]
            return {"id": "created"}

        api.request = request
        self.assertEqual(api.ensure_tunnel_dns("zone", "example.net", "tunnel"), "created")
        self.assertEqual(calls[-1][0], "POST")

    def test_delete_is_idempotent_for_missing_resource(self) -> None:
        api = CloudflareAPI("token")

        def missing(*_args, **_kwargs):
            raise CloudflareAPIError("missing", status_code=404)

        api.request = missing
        api.delete_dns_record("zone", "record")
        api.delete_access_app("account", "app")
        api.delete_access_policy("account", "policy")


class CloudflareCoreTests(unittest.TestCase):
    def test_batch_save_is_atomic_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            services = base / "services.json"
            policy = base / "ports.filter.json"
            services.write_text(
                json.dumps(
                    {
                        "http_services": [
                            {"port": 12001},
                            {"port": 12002},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            old_services = core.SERVICES_FILE
            old_policy = core.PORT_FILTER_FILE
            core.SERVICES_FILE = services
            core.PORT_FILTER_FILE = policy
            try:
                saved = core.save_all_cloudflare_rules(
                    {
                        "12001": {
                            "subdomains": ["12001", "citadel"],
                            "whitelist": True,
                            "emails": ["user@example.net"],
                        },
                        "12002": {
                            "subdomains": ["12002"],
                            "whitelist": False,
                            "emails": [],
                        },
                    }
                )
                self.assertEqual(list(saved), ["12001", "12002"])
                self.assertEqual(saved["12001"]["subdomains"], ["12001", "citadel"])
                self.assertFalse(saved["12002"]["whitelist"])
                self.assertEqual(cloudflare_rules(policy), saved)
                with self.assertRaises(ValueError):
                    core.save_all_cloudflare_rules(
                        {
                            "12001": {"subdomains": ["same"], "whitelist": False},
                            "12002": {"subdomains": ["same"], "whitelist": False},
                        }
                    )
                with self.assertRaises(ValueError):
                    core.save_all_cloudflare_rules(
                        {
                            "12001": {"subdomains": ["12001"], "whitelist": False},
                            "12002": {"subdomains": ["12001"], "whitelist": False},
                        }
                    )
                self.assertEqual(cloudflare_rules(policy), saved)
            finally:
                core.SERVICES_FILE = old_services
                core.PORT_FILTER_FILE = old_policy


if __name__ == "__main__":
    unittest.main()
