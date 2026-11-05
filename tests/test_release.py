import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
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


if __name__ == '__main__':
    unittest.main()
