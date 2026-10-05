"""Run plugin handlers against local subprocess fixtures, without a gateway."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PluginCommandsTests(unittest.TestCase):
    def test_commands_buttons_paths_and_auth(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = (ROOT / "index.js").read_text().replace(
                'import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";',
                'const definePluginEntry = value => value;')
            (root / "index.mjs").write_text(source)
            (root / "functions").mkdir()
            (root / "functions/plugin_bridge.py").write_text(
                'import json,sys\nfrom pathlib import Path\n'
                'with Path("bridge.log").open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                'ports=json.loads(Path("ports.json").read_text()) if Path("ports.json").exists() else [5800,11000]\n'
                'if "delete-serve" in sys.argv:\n'
                ' ports.remove(int(sys.argv[-1])); Path("ports.json").write_text(json.dumps(ports))\n'
                'print(json.dumps({"port":5800,"ports":ports,"provider_options":{"tailscale":"Tailscale","cloudflare":"Cloudflare"},"http_tiles":[{"port":443,"urls":{"tailscale":"https://node:443"}}],"other_ports":[]}))\n')
            (root / "scan.sh").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> scan.log\n')
            runner = '''
import plugin from './index.mjs';
let command;
const api={registerCommand:c=>command=c}; plugin.register(api);
const replies={};
for(const args of ['tailscale','tailscale edit','tailscale del 5800','tailscale reset','scan tailscale','add tailscale'])
 replies[args]=await command.handler({args,channel:'telegram'});
for(const args of ['tailscale del','tailscale del 0','tailscale del 65536','tailscale del 443 5800','tailscale del abc']) {
 try { await command.handler({args,channel:'telegram'}); throw new Error('Invalid deletion accepted'); }
 catch(error) { if(!error.message.includes('Usage:')) throw error; }
}
console.log(JSON.stringify({auth:command.requireAuth,replies}));
'''
            run = subprocess.run(["node", "--input-type=module", "-e", runner], cwd=root,
                                 capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertTrue(result["auth"])
            serialized = json.dumps(result["replies"])
            self.assertIn("Reset Serve Routes", serialized)
            self.assertIn("/citadel tailscale reset", serialized)
            edit = json.dumps(result["replies"]["tailscale edit"])
            self.assertIn("/citadel tailscale del 5800", edit)
            self.assertIn("/citadel tailscale del 11000", edit)
            self.assertNotIn("/citadel tailscale del 443", edit)
            deleted = json.dumps(result["replies"]["tailscale del 5800"])
            self.assertNotIn("/citadel tailscale del 5800", deleted)
            self.assertIn("/citadel tailscale del 11000", deleted)
            self.assertIn("/citadel add tailscale", serialized)
            self.assertIn('"label": "Add"', serialized)
            self.assertEqual((root / "scan.log").read_text().splitlines(), ["", "--add"])
            calls = [json.loads(line) for line in (root / "bridge.log").read_text().splitlines()]
            self.assertIn("reset-serve", [call[-1] for call in calls])
            self.assertEqual([call[-2:] for call in calls if "delete-serve" in call], [["delete-serve", "5800"]])
            for call in calls:
                self.assertEqual(call[call.index("--policy-path") + 1], str(root / "CITADEL_DATA/ports.filter.json"))

    def test_manifest_matches_runtime_config_schema(self):
        properties = json.loads((ROOT / "openclaw.plugin.json").read_text())["configSchema"]["properties"]
        self.assertEqual(set(properties), {"servicesPath", "scanScript", "policyPath", "pythonPath"})
