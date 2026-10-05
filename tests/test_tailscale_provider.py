from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "functions/providers"))
spec = importlib.util.spec_from_file_location("citadel_tailscale_provider", ROOT / "functions/providers/tailscale.py")
tailscale = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tailscale)
DOMAIN = "node.example.ts.net"


def status(state="Running"):
    return {"BackendState": state, "CertDomains": [DOMAIN],
            "Self": {"DNSName": DOMAIN + ".", "TailscaleIPs": ["100.100.100.1"]}}


def live_route(port=5800, scheme="https", target=None):
    return {
        "TCP": {str(port): {scheme.upper(): True}},
        "Web": {f"{DOMAIN}:{port}": {"Handlers": {"/": {"Proxy": target or f"http://127.0.0.1:{port}"}}}},
    }


def service(port, scheme="http", addr="127.0.0.1"):
    return {"port": port, "scheme": scheme, "addr": addr, "addrs": [addr],
            "urls": {"localhost": f"{scheme}://127.0.0.1:{port}"}}


class TailscaleProviderTests(unittest.TestCase):
    def test_add_preserves_old_routes_and_never_resets(self):
        old = {"url": f"https://{DOMAIN}:5800", "owns_listener": True}
        self.routes.write_text(json.dumps({"services": {"5800": old}}))
        self.services.write_text(json.dumps({"http_services": [service(5800), service(11000)], "added_ports": [11000]}))
        (self.cache / "5800.json").write_text('{"tailscale_url":"retained"}')
        with patch.dict(os.environ, {"CITADEL_SCAN_ADD": "1"}):
            result = self.run_provider(live=live_route(5800))
        self.assertNotIn(["tailscale", "serve", "reset"], self.commands)
        self.assertEqual(result["services"]["5800"], old)
        self.assertEqual(set(result["services"]), {"5800", "11000"})
        self.assertEqual(json.loads((self.cache / "5800.json").read_text())["tailscale_url"], "retained")
        self.assertIn("5800", self.live["TCP"])

    def test_add_rejects_existing_conflicting_serve_without_mutation(self):
        self.services.write_text(json.dumps({"http_services": [service(11000)], "added_ports": [11000]}))
        with patch.dict(os.environ, {"CITADEL_SCAN_ADD": "1"}):
            result = self.run_provider(live=live_route(11000, target="http://127.0.0.1:42"), expect_rc=1)
        self.assertEqual(self.mutations(), [])
        self.assertTrue(result["errors"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.provider = self.root / "extensions/enabled/tailscale"
        self.provider.mkdir(parents=True)
        self.manifest = self.provider / "extension.json"
        self.manifest.write_text('{"label":"Tailscale","enabled":true,"provider":"tailscale"}')
        self.services = self.root / "services.json"
        self.services.write_text(json.dumps({"http_services": [service(11000)]}))
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.state = self.root / "tailscale.json"
        self.routes = self.provider / "routes.json"

    def discover(self, rows, https_only=False):
        self.services.write_text(json.dumps({"http_services": rows, "https_only": https_only}))

    def run_provider(self, *, serve=None, live=None, statuses=None, startup_error=None,
                     reset_error=False, reset_ignored=False, apply_fail=(), unconfirmed=(),
                     post_fail=False, serve_status=None, cli=True, expect_rc=0, direct_fail=False):
        self.commands = []
        self.live = copy.deepcopy(live or {})
        pending = list(statuses or [status()])
        serve_pending = list(serve_status or [])

        def fake_run(args, **kwargs):
            self.commands.append(args)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertLessEqual(kwargs["timeout"], 15)
            output = ""
            error = ""
            if args[0] == "curl":
                self.assertNotIn("-k", args)
                self.assertNotIn("--insecure", args)
                self.assertIn("--resolve", args)
                self.assertIn("--noproxy", args)
                output = "401"
                error = "TLS certificate verification failed" if direct_fail else ""
            elif args == ["tailscale", "status", "--json"]:
                response = pending.pop(0) if len(pending) > 1 else pending[0]
                if response is None:
                    error = "daemon unavailable"
                else:
                    output = json.dumps(response)
            elif args in (["sudo", "-n", "systemctl", "start", "tailscaled"], ["systemctl", "start", "tailscaled"]) or args[1:5] == ["debug", "localapi", "PATCH", "prefs"]:
                if isinstance(startup_error, Exception):
                    raise startup_error
                error = startup_error or ""
            elif args == ["tailscale", "serve", "status", "--json"]:
                response = serve_pending.pop(0) if serve_pending else self.live
                output = response if isinstance(response, str) else json.dumps(response)
            elif args == ["tailscale", "serve", "reset"]:
                if reset_error:
                    error = "reset denied"
                elif not reset_ignored:
                    self.live = {}
            elif args[1:5] == ["debug", "localapi", "POST", "serve-config"]:
                if post_fail:
                    error = "remove denied"
                else:
                    self.live = json.loads(args[5])
            elif args[:4] == ["tailscale", "serve", "--bg", "--yes"]:
                self.assertTrue(args[4].startswith("--https="))
                port = int(args[4].split("=")[1])
                if port in apply_fail:
                    error = "apply denied"
                elif port not in unconfirmed:
                    update = live_route(port, "https", args[-1])
                    for field in ("TCP", "Web"):
                        self.live.setdefault(field, {}).update(update[field])
            else:
                self.fail(f"unexpected command {args}")
            return subprocess.CompletedProcess(args, bool(error), output, error)

        def value(_directory, key, default=""):
            self.assertEqual(key, "CITADEL_TAILSCALE_SERVE")
            return default if serve is None else str(serve)

        argv = ["tailscale.py", "--provider-dir", str(self.provider),
                "--services-file", str(self.services), "--cache-dir", str(self.cache),
                "--config-ini", str(self.root / "config.ini"),
                "--routes-out", str(self.routes), "--tailscale-file", str(self.state)]
        with (patch.object(tailscale.subprocess, "run", side_effect=fake_run),
              patch.object(tailscale, "citadel_value", side_effect=value),
              patch.object(tailscale.shutil, "which", return_value="tailscale" if cli else None),
              patch.object(tailscale.time, "sleep"),
              patch.object(sys, "argv", argv)):
            self.assertEqual(tailscale.main(), expect_rc)
        self.assertFalse(any(arg.startswith("--http=") for cmd in self.commands for arg in cmd))
        self.assertFalse(any(cmd[:2] in (["tailscale", "up"], ["tailscale", "login"]) for cmd in self.commands))
        return json.loads(self.routes.read_text()) if self.routes.exists() else None

    def mutations(self):
        return [cmd for cmd in self.commands
                if cmd[0] != "curl" and cmd not in (["tailscale", "status", "--json"], ["tailscale", "serve", "status", "--json"])]

    def test_default_resets_all_routes_then_builds_same_port_https(self):
        self.discover([service(11000), service(2000, "https", "*")])
        payload = self.run_provider(live=live_route(5800))
        self.assertEqual(self.mutations(), [
            ["tailscale", "serve", "reset"],
            ["tailscale", "serve", "--bg", "--yes", "--https=2000", "https+insecure://127.0.0.1:2000"],
            ["tailscale", "serve", "--bg", "--yes", "--https=11000", "http://127.0.0.1:11000"],
        ])
        self.assertNotIn("5800", tailscale.node_ports(self.live))
        self.assertEqual(set(payload["services"]), {"2000", "11000"})
        self.assertTrue(all(route["url"].startswith("https://") and route["mode"] == "proxy"
                            for route in payload["services"].values()))
        self.assertTrue(payload["serve_enabled"])
        self.assertNotIn("persistent", payload)
        self.assertNotIn("managed_routes", payload)
        self.assertNotIn("serve_routes", payload)

    def test_reset_mode_rebuilds_even_matching_existing_route(self):
        self.run_provider(live=live_route(11000))
        self.assertEqual(len(self.mutations()), 2)

    def test_reset_clears_paths_tcp_foreground_and_funnel(self):
        live = live_route()
        live["TCP"]["22"] = {"TCPForward": "127.0.0.1:22"}
        live["Foreground"] = {"session": live_route(9090)}
        live["AllowFunnel"] = {f"{DOMAIN}:5800": True}
        self.run_provider(live=live)
        self.assertEqual(tailscale.node_ports(self.live), {"11000"})

    def test_reset_failure_clears_stale_urls_and_does_not_apply(self):
        row = service(11000)
        row["urls"]["tailscale"] = f"http://{DOMAIN}:11000"
        self.discover([row])
        cached = self.cache / "11000.json"
        cached.write_text(json.dumps({"title": "app", "tailscale_url": row["urls"]["tailscale"]}))
        payload = self.run_provider(reset_error=True, expect_rc=1)
        self.assertEqual(self.mutations(), [["tailscale", "serve", "reset"]])
        self.assertEqual(payload["services"], {})
        self.assertFalse(payload["available"])
        self.assertNotIn("tailscale", json.loads(self.services.read_text())["http_services"][0]["urls"])
        self.assertEqual(json.loads(cached.read_text()), {"title": "app"})

    def test_success_exit_without_empty_reset_is_rejected(self):
        payload = self.run_provider(live=live_route(), reset_ignored=True, expect_rc=1)
        self.assertEqual(self.mutations(), [["tailscale", "serve", "reset"]])
        self.assertEqual(payload["services"], {})

    def test_individual_apply_failure_does_not_publish_failed_port(self):
        self.discover([service(2008, addr="*"), service(9090, "https", "*")])
        payload = self.run_provider(apply_fail={2008}, expect_rc=1)
        self.assertEqual(set(payload["services"]), {"9090"})
        self.assertEqual(payload["services"]["9090"]["target"], "https+insecure://127.0.0.1:9090")

    def test_unconfirmed_apply_or_unreadable_status_publishes_no_url(self):
        payload = self.run_provider(unconfirmed={11000}, expect_rc=1)
        self.assertEqual(payload["services"], {})
        payload = self.run_provider(serve_status=[{}, "{broken"], expect_rc=1)
        self.assertEqual(payload["services"], {})

    def test_empty_discovery_still_resets_in_default_mode(self):
        self.discover([])
        payload = self.run_provider(live=live_route())
        self.assertEqual(self.mutations(), [["tailscale", "serve", "reset"]])
        self.assertEqual(payload["services"], {})

    def test_serve_off_only_publishes_verified_direct_https_without_touching_serve(self):
        self.discover([service(3005, "https", "*"), service(5800)])
        initial = live_route(11000)
        payload = self.run_provider(serve=0, live=initial)
        self.assertFalse(payload["serve_enabled"])
        self.assertEqual(payload["services"], {"3005": {
            "mode": "direct", "url": f"https://{DOMAIN}:3005", "target": None, "owns_listener": False}})
        self.assertEqual(self.live, initial)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(any("serve" in cmd or "serve-config" in cmd for cmd in self.commands))
        curl = next(cmd for cmd in self.commands if cmd[0] == "curl")
        self.assertIn(f"{DOMAIN}:3005:100.100.100.1", curl)

    def test_serve_off_does_not_advertise_failed_https_or_stale_http_links(self):
        row = service(3005, "https")
        row["urls"]["tailscale"] = "https://old.example:3005"
        self.discover([row])
        payload = self.run_provider(serve=0, direct_fail=True)
        self.assertEqual(payload["services"], {})
        self.assertIn("TLS certificate", payload["skipped"]["3005"])
        self.assertEqual(payload["errors"], [])
        self.assertNotIn("tailscale", json.loads(self.services.read_text())["http_services"][0]["urls"])
        self.assertEqual(self.mutations(), [])

    def test_serve_off_with_empty_scan_never_reads_or_changes_serve(self):
        self.discover([])
        initial = live_route()
        self.run_provider(serve=0, live=initial)
        self.assertEqual(self.commands, [["tailscale", "status", "--json"]])
        self.assertEqual(self.live, initial)

    def test_direct_https_does_not_require_serve_certificate_configuration(self):
        self.discover([service(3005, "https")])
        info = status()
        info["CertDomains"] = []
        self.assertTrue(self.run_provider(serve=0, statuses=[info])["available"])

    def test_direct_https_ipv6_is_pinned_to_the_node_address(self):
        self.discover([service(3005, "https")])
        info = status()
        info["Self"]["TailscaleIPs"] = ["fd7a:115c:a1e0::1"]
        self.run_provider(serve=0, statuses=[info])
        curl = next(cmd for cmd in self.commands if cmd[0] == "curl")
        self.assertIn(f"{DOMAIN}:3005:[fd7a:115c:a1e0::1]", curl)

    def test_direct_https_without_node_address_does_not_invent_links(self):
        self.discover([service(3005, "https")])
        info = status()
        info["Self"]["TailscaleIPs"] = []
        payload = self.run_provider(serve=0, statuses=[info])
        self.assertEqual(payload["services"], {})
        self.assertIn("no Tailscale IP", payload["skipped"]["3005"])
        self.assertEqual(self.commands, [["tailscale", "status", "--json"]])

    def test_old_state_is_not_input_or_ownership_ledger(self):
        self.state.write_text("{broken")
        payload = self.run_provider()
        self.assertEqual(set(payload["services"]), {"11000"})

    def test_https_only_filters_backends_but_hidden_webui_backend_is_eligible(self):
        hidden = service(11000)
        hidden["hide_from_dashboard"] = True
        self.discover([hidden, service(2000, "https")], https_only=True)
        self.assertEqual(set(self.run_provider()["services"]), {"2000"})
        self.discover([hidden, service(2000, "https")])
        self.assertEqual(set(self.run_provider()["services"]), {"11000", "2000"})

    def test_disabled_manifest_does_not_start_or_change_tailscale(self):
        self.manifest.write_text('{"enabled":false}')
        payload = self.run_provider(statuses=[None])
        self.assertFalse(payload["considered"])
        self.assertEqual(self.commands, [])

    def test_invalid_input_prevents_all_commands(self):
        for payload in ({}, {"http_services": [None]}, {"http_services": [service(0)]},
                        {"http_services": [service(11000), service(11000)]},
                        {"http_services": [service(11000, "tcp")]}):
            with self.subTest(payload=payload):
                self.services.write_text(json.dumps(payload))
                self.run_provider(expect_rc=1)
                self.assertEqual(self.commands, [])

    def test_invalid_serve_flag_prevents_all_commands(self):
        self.run_provider(serve="maybe", expect_rc=1)
        self.assertEqual(self.commands, [])

    def test_blank_serve_flag_defaults_to_reset_mode(self):
        for value in ("", "   ", "blank"):
            with self.subTest(value=value):
                payload = self.run_provider(serve=value)
                self.assertTrue(payload["serve_enabled"])
                self.assertEqual(self.mutations()[0], ["tailscale", "serve", "reset"])

    def test_missing_cli_has_no_urls(self):
        payload = self.run_provider(cli=False, expect_rc=1)
        self.assertEqual(self.commands, [])
        self.assertEqual(payload["services"], {})

    def test_running_daemon_needs_no_startup(self):
        self.run_provider()
        self.assertFalse(any(cmd[0] == "sudo" or "PATCH" in cmd for cmd in self.commands))

    def test_missing_daemon_gets_one_noninteractive_start_and_recheck(self):
        with patch.object(tailscale.os, "geteuid", return_value=1000):
            payload = self.run_provider(statuses=[None, status()])
        self.assertTrue(payload["running"])
        self.assertEqual(self.mutations()[0], ["sudo", "-n", "systemctl", "start", "tailscaled"])

    def test_root_container_starts_without_requiring_sudo(self):
        with patch.object(tailscale.os, "geteuid", return_value=0):
            payload = self.run_provider(statuses=[None, status()])
        self.assertTrue(payload["running"])
        self.assertEqual(self.mutations()[0], ["systemctl", "start", "tailscaled"])

    def test_stopped_daemon_gets_one_preference_resume_without_authentication(self):
        payload = self.run_provider(statuses=[status("Stopped"), status("Starting"), status()])
        startup = self.mutations()[0]
        self.assertEqual(startup[:5], ["tailscale", "debug", "localapi", "PATCH", "prefs"])
        self.assertEqual(json.loads(startup[5]), {"WantRunning": True, "WantRunningSet": True})
        self.assertTrue(payload["running"])

    def test_failed_startup_or_needs_login_never_resets_or_publishes(self):
        for initial, failure in ((None, "sudo denied"), (status("NeedsLogin"), None),
                                 (status("Stopped"), subprocess.TimeoutExpired("resume", 10))):
            with self.subTest(initial=initial, failure=failure):
                payload = self.run_provider(statuses=[initial], startup_error=failure, expect_rc=1)
                self.assertEqual(len(self.mutations()), 1)
                self.assertEqual(payload["services"], {})
                self.assertIn("one startup attempt", payload["errors"][0])

    def test_missing_certificate_domain_never_resets_or_starts_authentication(self):
        info = status()
        info["CertDomains"] = []
        payload = self.run_provider(statuses=[info], expect_rc=1)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(payload["services"], {})


if __name__ == "__main__":
    unittest.main()
