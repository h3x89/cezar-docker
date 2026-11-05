"""Exercise the user-facing script with a fake npm, without downloads/accounts."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[1] / "scripts/codex-tool"


class CodexToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cezar tools ")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home with spaces"
        self.bin = Path(self.tmp.name) / "test-bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / "npm-calls.jsonl"
        self.env = {**os.environ, "HOME": str(self.home),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "NPM_TEST_LOG": str(self.log)}
        self.executable("id", "#!/bin/sh\nprintf '1000\\n'\n")
        self.executable("npm", '''#!/usr/bin/env python3
import json, os, pathlib, sys
with open(os.environ["NPM_TEST_LOG"], "a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")
if os.environ.get("NPM_TEST_FAIL"): sys.exit(42)
prefix=pathlib.Path(sys.argv[sys.argv.index("--prefix")+1])
version=sys.argv[-1].split("@")[-1]
package=prefix/"lib/node_modules/@openai/codex"
(package/"bin").mkdir(parents=True, exist_ok=True)
(package/"package.json").write_text(json.dumps({"version":version}))
target=package/"bin/codex"
target.write_text("#!/bin/sh\\nprintf '%s\\\\n' 'codex-cli "+version+"'\\n")
target.chmod(0o755)
(prefix/"bin").mkdir(exist_ok=True)
link=prefix/"bin/codex"
if not link.is_symlink(): link.symlink_to(target)
''')

    def executable(self, name, content):
        p = self.bin / name
        p.write_text(content)
        p.chmod(0o755)

    def run_tool(self, *args, **env):
        return subprocess.run(["bash", str(TOOL), *args], env={**self.env, **env},
                              text=True, capture_output=True)

    def test_explicit_install_update_and_read_only_check(self):
        for operation, version in [("install", "0.163.0"), ("update", "0.166.0")]:
            r = self.run_tool(operation, version)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "codex-cli " + version)
        calls_before = self.log.read_text()
        r = self.run_tool("check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log.read_text(), calls_before)
        calls = [json.loads(x) for x in calls_before.splitlines()]
        self.assertEqual(calls[0][-1], "@openai/codex@0.163.0")
        self.assertIn(str(self.home / ".local"), calls[0])

    def test_aliases_urls_and_shell_text_do_not_reach_npm(self):
        for version in ["latest", "^0.166.0", "https://example.invalid/a", "0.1.0;touch oops", ""]:
            with self.subTest(version=version):
                self.assertNotEqual(self.run_tool("install", version).returncode, 0)
        self.assertFalse(self.log.exists())

    def test_invalid_operations_and_arguments_fail(self):
        for args in [(), ("remove",), ("install",), ("check", "extra")]:
            self.assertNotEqual(self.run_tool(*args).returncode, 0)
        self.assertFalse(self.log.exists())

    def test_missing_or_broken_installation_fails(self):
        self.assertNotEqual(self.run_tool("check").returncode, 0)
        self.assertEqual(self.run_tool("install", "0.166.0").returncode, 0)
        (self.home / ".local/lib/node_modules/@openai/codex/bin/codex").unlink()
        self.assertNotEqual(self.run_tool("check").returncode, 0)

    def test_npm_failure_is_propagated_without_retry_or_fallback(self):
        r = self.run_tool("install", "0.166.0", NPM_TEST_FAIL="1")
        self.assertEqual(r.returncode, 42)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_root_is_rejected(self):
        self.executable("id", "#!/bin/sh\nprintf '0\\n'\n")
        r = self.run_tool("install", "0.166.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_launcher_outside_home_is_not_claimed_persistent(self):
        self.assertEqual(self.run_tool("install", "0.166.0").returncode, 0)
        link = self.home / ".local/bin/codex"
        link.unlink()
        self.executable("external-codex", "#!/bin/sh\nprintf 'codex-cli 0.166.0\\n'\n")
        link.symlink_to(self.bin / "external-codex")
        self.assertNotEqual(self.run_tool("check").returncode, 0)

    def test_executable_version_mismatch_is_rejected(self):
        self.assertEqual(self.run_tool("install", "0.166.0").returncode, 0)
        p = self.home / ".local/lib/node_modules/@openai/codex/package.json"
        p.write_text('{"version":"0.1.0"}')
        self.assertNotEqual(self.run_tool("check").returncode, 0)

    def test_prefix_symlink_outside_home_fails_before_npm(self):
        outside = Path(self.tmp.name) / "outside-home"
        outside.mkdir()
        (self.home / ".local").symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.run_tool("install", "0.166.0").returncode, 0)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
