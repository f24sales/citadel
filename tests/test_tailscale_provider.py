from __future__ import annotations

import copy
import importlib.util
import json
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
    return {"BackendState": state, "CertDomains": [DOMAIN], "Self": {"DNSName": DOMAIN + "."}}


def live_route(port=5800, scheme="https", target=None):
    return {
        "TCP": {str(port): {scheme.upper(): True}},
        "Web": {f"{DOMAIN}:{port}": {"Handlers": {"/": {"Proxy": target or f"http://127.0.0.1:{port}"}}}},
    }


def service(port, scheme="http", addr="127.0.0.1"):
    return {"port": port, "scheme": scheme, "addr": addr, "addrs": [addr],
            "urls": {"localhost": f"{scheme}://127.0.0.1:{port}"}}


class TailscaleProviderTests(unittest.TestCase):
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

    def run_provider(self, *, persistent=None, live=None, statuses=None, startup_error=None,
                     reset_error=False, reset_ignored=False, apply_fail=(), unconfirmed=(),
                     post_fail=False, serve_status=None, cli=True, expect_rc=0):
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
            if args == ["tailscale", "status", "--json"]:
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
            self.assertEqual(key, "CITADEL_PERSISTENT")
            return default if persistent is None else str(persistent)

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
                if cmd not in (["tailscale", "status", "--json"], ["tailscale", "serve", "status", "--json"])]

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
        self.assertFalse(payload["persistent"])
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

    def test_persistent_matching_https_is_unchanged(self):
        payload = self.run_provider(persistent=1, live=live_route(11000))
        self.assertEqual(self.mutations(), [])
        self.assertEqual(payload["services"]["11000"]["url"], f"https://{DOMAIN}:11000")

    def test_persistent_retains_unobserved_routes_without_advertising_them(self):
        live = live_route(5800)
        payload = self.run_provider(persistent=1, live=live)
        self.assertEqual(tailscale.node_ports(self.live), {"5800", "11000"})
        self.assertEqual(set(payload["services"]), {"11000"})
        self.assertFalse(any(cmd[-1] == "reset" for cmd in self.commands))

    def test_persistent_empty_discovery_retains_live_configuration(self):
        self.discover([])
        initial = live_route()
        payload = self.run_provider(persistent=1, live=initial)
        self.assertEqual(self.live, initial)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(payload["services"], {})

    def test_persistent_replaces_conflicting_port_without_ownership(self):
        for kind in ("http", "target", "paths", "funnel", "tcp", "foreground"):
            with self.subTest(kind=kind):
                live = live_route(11000, "http" if kind == "http" else "https")
                if kind == "target":
                    live["Web"][f"{DOMAIN}:11000"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
                elif kind == "paths":
                    live["Web"][f"{DOMAIN}:11000"]["Handlers"]["/extra"] = {"Text": "old"}
                elif kind == "funnel":
                    live["AllowFunnel"] = {f"{DOMAIN}:11000": True}
                elif kind == "tcp":
                    live = {"TCP": {"11000": {"TCPForward": "127.0.0.1:22"}}}
                elif kind == "foreground":
                    live = {"Foreground": {"session": live}}
                payload = self.run_provider(persistent=1, live=live)
                self.assertEqual(len(self.mutations()), 2)
                self.assertEqual(self.mutations()[0][1:5], ["debug", "localapi", "POST", "serve-config"])
                self.assertEqual(payload["services"]["11000"]["url"], f"https://{DOMAIN}:11000")

    def test_persistent_removal_failure_does_not_apply_or_publish(self):
        payload = self.run_provider(persistent=1, live=live_route(11000, "http"), post_fail=True, expect_rc=1)
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(payload["services"], {})

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

    def test_invalid_persistence_prevents_all_commands(self):
        self.run_provider(persistent="maybe", expect_rc=1)
        self.assertEqual(self.commands, [])

    def test_blank_persistence_defaults_to_reset_mode(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                payload = self.run_provider(persistent=value)
                self.assertFalse(payload["persistent"])
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
