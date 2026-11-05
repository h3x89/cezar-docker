#!/usr/bin/env python3
"""Publish npm releases with durable, compare-and-swap state.

A reservation is persisted before each build. If final state persistence fails,
subsequent runs can rebuild within the remaining three-attempt budget.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

VERSION = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$', re.ASCII)
DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
MAX_ATTEMPTS = 3
INTERVAL = 3600
BRANCH = 'update-state'


def validate_version(version):
    match = VERSION.fullmatch(version)
    if not match or len(version) > 128:
        raise ValueError(f'Invalid exact Docker-compatible version: {version!r}')
    if match.group(4) and any(p.isdigit() and len(p) > 1 and p[0] == '0' for p in match.group(4).split('.')):
        raise ValueError(f'Invalid prerelease version: {version!r}')
    return bool(match.group(4))


def gh(*args, data=None, missing_ok=False):
    command = ['gh', 'api', *args]
    if data is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=True, timeout=60)
    if result.returncode:
        if missing_ok and 'HTTP 404' in result.stderr:
            return None
        raise RuntimeError(f'GitHub API request failed: {result.stderr.strip()}')
    return json.loads(result.stdout)


class StateStore:
    def __init__(self, repo):
        self.root = f'repos/{repo}'
        self.sha = None

    def load(self):
        ref = gh(f'{self.root}/git/ref/heads/{BRANCH}', missing_ok=True)
        if ref is None:
            main = gh(f'{self.root}/git/ref/heads/main')
            gh(f'{self.root}/git/refs', data={'ref': f'refs/heads/{BRANCH}', 'sha': main['object']['sha']})
        obj = gh(f'{self.root}/contents/state.json?ref={BRANCH}', missing_ok=True)
        if obj is None:
            return {'versions': {}, 'aliases': {}}
        self.sha = obj['sha']
        state = json.loads(base64.b64decode(obj['content'], validate=False))
        if not isinstance(state, dict) or not isinstance(state.get('versions'), dict) or not isinstance(state.get('aliases'), dict):
            raise ValueError('Invalid release state')
        return state

    def save(self, state):
        content = base64.b64encode((json.dumps(state, indent=2, sort_keys=True) + '\n').encode()).decode()
        body = {'message': 'Update release state', 'branch': BRANCH, 'content': content}
        if self.sha:
            body['sha'] = self.sha
        result = gh(f'{self.root}/contents/state.json', '-X', 'PUT', data=body)
        self.sha = result['content']['sha']


def resolve_tags():
    url = 'https://registry.npmjs.org/-/package/%40open-mercato%2Fcezar/dist-tags'
    with urllib.request.urlopen(url, timeout=20) as response:
        tags = json.load(response)
    result = {}
    for channel in ('latest', 'nightly'):
        version = tags[channel]
        prerelease = validate_version(version)
        if channel == 'latest' and prerelease:
            raise ValueError('The latest dist-tag must refer to a stable version')
        result[channel] = version
    return result


def eligible(record, now):
    return (record.get('status') != 'published' and record.get('attempts', 0) < MAX_ATTEMPTS
            and (record.get('last_attempt') is None or now - record['last_attempt'] >= INTERVAL))


def promote(repo, version, digest, channel):
    source = f'{repo}@{digest}'
    subprocess.run(['docker', 'pull', source], check=True, timeout=600)
    subprocess.run(['docker', 'tag', source, f'{repo}:{channel}'], check=True, timeout=60)
    subprocess.run(['docker', 'push', f'{repo}:{channel}'], check=True, timeout=600)


def run_release(store, state, version, channels, force=False, now=None):
    validate_version(version)
    now = int(time.time()) if now is None else now
    versions = state['versions']
    record = versions.setdefault(version, {'attempts': 0})
    if force:
        record = versions[version] = {'attempts': 0}
    image_repo = os.environ.get('IMAGE_REPOSITORY', 'ghcr.io/h3x89/cezar-docker')
    channel = next((c for c in ('latest', 'nightly') if c in channels), 'version')
    if eligible(record, now):
        record.update(attempts=record.get('attempts', 0) + 1, last_attempt=now, status='reserved')
        store.save(state)  # A failure here must prevent the build.
        env = os.environ.copy()
        env.update(CEZAR_VERSION=version, RELEASE_CHANNEL=channel, PUBLISH='true', IMAGE_REPOSITORY=image_repo)
        try:
            with tempfile.TemporaryDirectory(prefix='cezar-release-') as directory:
                result_path = os.path.join(directory, 'result.json')
                env['RELEASE_RESULT'] = result_path
                subprocess.run(['bash', 'scripts/build-publish.sh'], env=env, check=True)
                with open(result_path, encoding='utf-8') as result_file:
                    result = json.load(result_file)
            digest = result['digest']
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError('Build result has no valid sha256 digest')
            if result.get('image') != f'{image_repo}:{version}':
                raise ValueError('Build result image does not match the requested version')
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
            record['status'] = 'failed'
            store.save(state)
            raise RuntimeError(f'Release failed for {version}: {exc}') from exc
        record.update(status='published', digest=digest, image=result['image'])
        if channel != 'version':
            state['aliases'][channel] = {'version': version, 'digest': digest}
        store.save(state)
    elif record.get('status') != 'published':
        print(f'Skipping {version}: attempts exhausted or cooldown active')
        return
    else:
        print(f'Skipping already published {version}')
    # A tag may move to an already published version, or both npm tags may
    # resolve to one version. Promote the immutable digest without rebuilding.
    for alias in channels:
        if alias == 'version':
            continue
        target = {'version': version, 'digest': record['digest']}
        if state['aliases'].get(alias) != target:
            promote(image_repo, version, record['digest'], alias)
            state['aliases'][alias] = target
            store.save(state)


def main():
    manual = os.environ.get('INPUT_CEZAR_VERSION', '').strip()
    channel = os.environ.get('INPUT_CHANNEL', 'version') or 'version'
    force = os.environ.get('INPUT_FORCE', 'false').lower() == 'true'
    if channel not in ('version', 'latest', 'nightly'):
        raise ValueError('Invalid channel')
    if force and not manual:
        raise ValueError('Force requires an explicit version')
    if manual:
        prerelease = validate_version(manual)
        if channel == 'latest' and prerelease:
            raise ValueError('The latest channel requires a stable version')
        requests = {manual: [channel]}
    else:
        requests = {}
        for tag, version in resolve_tags().items():
            requests.setdefault(version, []).append(tag)
    store = StateStore(os.environ['GITHUB_REPOSITORY'])
    state = store.load()
    failed = False
    for version, channels in requests.items():
        try:
            run_release(store, state, version, channels, force=force)
        except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            failed = True
            # Stop on any failure, including uncertain state writes.
            break
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
