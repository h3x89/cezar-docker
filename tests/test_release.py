import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('release', Path(__file__).resolve().parents[1] / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
DIGEST = 'sha256:' + 'a' * 64
IMAGE = 'ghcr.io/h3x89/cezar-docker:1.2.3'


class Store:
    def __init__(self):
        self.snapshots = []
        self.fail = False

    def save(self, state):
        if self.fail:
            raise RuntimeError('state write failed')
        self.snapshots.append(json.loads(json.dumps(state)))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.state = {'versions': {}, 'aliases': {}}
        self.env = mock.patch.dict(os.environ, {'IMAGE_REPOSITORY': 'ghcr.io/h3x89/cezar-docker'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def successful_build(self, command, **kwargs):
        if command[:2] == ['bash', 'scripts/build-publish.sh']:
            Path(kwargs['env']['RELEASE_RESULT']).write_text(json.dumps({'image': IMAGE, 'digest': DIGEST}))
        return subprocess.CompletedProcess(command, 0)

    @mock.patch.object(release.subprocess, 'run')
    def test_success_is_persisted_and_skipped_on_next_runner(self, run):
        run.side_effect = self.successful_build
        release.run_release(self.store, self.state, '1.2.3', ['latest'], now=10000)
        self.assertEqual(self.store.snapshots[0]['versions']['1.2.3']['status'], 'reserved')
        restored = json.loads(json.dumps(self.store.snapshots[-1]))
        self.assertEqual(restored['versions']['1.2.3']['digest'], DIGEST)
        release.run_release(self.store, restored, '1.2.3', ['latest'], now=20000)
        self.assertEqual(run.call_count, 1)

    @mock.patch.object(release.subprocess, 'run')
    def test_three_failures_consume_budget_with_hour_boundary(self, run):
        run.side_effect = subprocess.CalledProcessError(1, 'build')
        for now in (10000, 13600, 17200):
            with self.assertRaises(RuntimeError):
                release.run_release(self.store, self.state, '1.2.3', ['version'], now=now)
        release.run_release(self.store, self.state, '1.2.3', ['version'], now=20800)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(self.state['versions']['1.2.3']['attempts'], 3)
        self.assertEqual(self.state['versions']['1.2.3']['status'], 'failed')

    @mock.patch.object(release.subprocess, 'run')
    def test_cancelled_reservation_survives_and_cooldown_is_exact(self, run):
        run.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            release.run_release(self.store, self.state, '1.2.3', ['version'], now=10000)
        recovered = copy.deepcopy(self.store.snapshots[-1])
        run.side_effect = self.successful_build
        release.run_release(self.store, recovered, '1.2.3', ['version'], now=13599)
        self.assertEqual(run.call_count, 1)
        release.run_release(self.store, recovered, '1.2.3', ['version'], now=13600)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(recovered['versions']['1.2.3']['attempts'], 2)

    @mock.patch.object(release.subprocess, 'run')
    def test_state_write_failure_prevents_build(self, run):
        self.store.fail = True
        with self.assertRaises(RuntimeError):
            release.run_release(self.store, self.state, '1.2.3', ['version'], now=10000)
        run.assert_not_called()

    @mock.patch.object(release, 'promote')
    @mock.patch.object(release.subprocess, 'run')
    def test_same_version_builds_once_and_promotes_other_alias(self, run, promote):
        run.side_effect = self.successful_build
        release.run_release(self.store, self.state, '1.2.3', ['latest', 'nightly'], now=10000)
        self.assertEqual(run.call_count, 1)
        promote.assert_called_once_with('ghcr.io/h3x89/cezar-docker', '1.2.3', DIGEST, 'nightly')
        self.assertEqual(self.state['aliases']['nightly']['digest'], DIGEST)

    @mock.patch.object(release.subprocess, 'run')
    def test_force_resets_only_requested_version(self, run):
        self.state['versions'] = {'1.2.3': {'attempts': 3, 'status': 'failed'}, '1.0.0': {'attempts': 2}}
        run.side_effect = self.successful_build
        release.run_release(self.store, self.state, '1.2.3', ['version'], force=True, now=10000)
        self.assertEqual(self.state['versions']['1.2.3']['attempts'], 1)
        self.assertEqual(self.state['versions']['1.0.0']['attempts'], 2)

    @mock.patch.object(release.subprocess, 'run')
    def test_alias_promotion_copies_the_complete_index(self, run):
        release.promote('ghcr.io/h3x89/cezar-docker', '1.2.3', DIGEST, 'nightly')
        run.assert_called_once_with(
            ['regctl', 'image', 'copy', 'ghcr.io/h3x89/cezar-docker@' + DIGEST,
             'ghcr.io/h3x89/cezar-docker:nightly'], check=True, timeout=600)


class BuildPublishTests(unittest.TestCase):
    """Check real shell orchestration with fake Docker/smoke, without a registry."""

    def run_script(self, fail_arm=False, bad_index=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = f'#!{sys.executable}\n' + r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
tool = Path(sys.argv[0]).name
with open(os.environ['COMMAND_LOG'], 'a') as f:
    f.write(json.dumps([tool, os.environ.get('CEZAR_PLATFORM'), args]) + '\n')
if tool == 'bash':
    sys.exit(1 if os.environ.get('FAIL_ARM') == 'true' and os.environ['CEZAR_PLATFORM'] == 'linux/arm64' else 0)
if args[0] == 'push':
    print('digest: sha256:' + ('a' if ':amd64-' in args[1] else 'b') * 64)
if tool == 'regctl' and args[:2] == ['index', 'create']:
    print('sha256:' + 'c' * 64)
if tool == 'regctl' and args[:2] == ['manifest', 'get']:
    m = {'mediaType': 'application/vnd.oci.image.index.v1+json',
         'manifests': [{'platform': {'os': 'linux', 'architecture': a}}
                       for a in ('amd64', 'arm64')],
         'annotations': {'org.opencontainers.image.description':
             'Cezar for Linux AMD64 (x86_64) and ARM64 (aarch64); install Codex separately.'}}
    if os.environ['BAD_INDEX'] == 'true':
        m['annotations'] = {}
    print(json.dumps(m))
'''
            for tool in ('docker', 'bash', 'regctl'):
                path = root / tool
                path.write_text(stub)
                path.chmod(0o755)
            log, result = root / 'commands.jsonl', root / 'result.json'
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                   'CEZAR_VERSION': '1.2.3', 'CODEX_VERSION': '0.166.0',
                   'PUBLISH': 'true', 'RELEASE_CHANNEL': 'latest',
                   'IMAGE_REPOSITORY': 'ghcr.io/h3x89/cezar-docker',
                   'RELEASE_RESULT': str(result), 'COMMAND_LOG': str(log),
                   'FAIL_ARM': str(fail_arm).lower(), 'BAD_INDEX': str(bad_index).lower()}
            completed = subprocess.run(
                ['/bin/bash', str(Path(__file__).resolve().parents[1] / 'scripts/build-publish.sh')],
                env=env, capture_output=True, text=True)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            return completed, commands, json.loads(result.read_text()) if result.exists() else None

    def test_both_tests_precede_any_push_and_alias_keeps_index(self):
        completed, commands, result = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        smoke = [i for i, c in enumerate(commands) if c[0] == 'bash']
        pushes = [i for i, c in enumerate(commands) if c[2][0] == 'push']
        self.assertEqual([commands[i][1] for i in smoke], ['linux/amd64', 'linux/arm64'])
        self.assertGreater(min(pushes), max(smoke))
        self.assertEqual([commands[i][2][1] for i in pushes],
                         ['ghcr.io/h3x89/cezar-docker:' + a + '-1.2.3'
                          for a in ('amd64', 'arm64')])
        create = next(c[2] for c in commands if c[0] == 'regctl' and c[2][:2] == ['index', 'create'])
        self.assertEqual([create[i + 1] for i, arg in enumerate(create) if arg == '--ref'],
                         ['ghcr.io/h3x89/cezar-docker@sha256:' + d * 64 for d in ('a', 'b')])
        # Platform tags start with a letter; exact accepted versions start with a digit.
        for i in pushes:
            with self.assertRaises(ValueError):
                release.validate_version(commands[i][2][1].split(':')[1])
        self.assertEqual(result, {'image': IMAGE, 'digest': 'sha256:' + 'c' * 64})
        self.assertEqual(commands[-1][2],
                         ['image', 'copy', 'ghcr.io/h3x89/cezar-docker@' + result['digest'],
                          'ghcr.io/h3x89/cezar-docker:latest'])

    def test_arm_smoke_failure_prevents_all_registry_writes(self):
        completed, commands, result = self.run_script(fail_arm=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNone(result)
        self.assertFalse(any(c[2][0] == 'push' or c[0] == 'regctl' for c in commands))

    def test_invalid_index_prevents_alias_and_success_result(self):
        completed, commands, result = self.run_script(bad_index=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNone(result)
        self.assertFalse(any(c[0] == 'regctl' and c[2][:2] == ['image', 'copy']
                             for c in commands))


if __name__ == '__main__':
    unittest.main()
