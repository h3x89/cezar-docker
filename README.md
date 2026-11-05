# Cezar Docker

Container packaging for [upstream Cezar](https://github.com/open-mercato/cezar).
Images are not published to a registry yet. Build locally with Docker Compose
or use GitHub Actions to build and test the image.

The image contains the unchanged Cezar npm package and basic development tools.
Codex is installed separately into a persistent home volume. Linux AMD64 only.

## Start

Docker Engine with Compose v2 or later is required. Choose an exact Cezar
version, build the image, and start the cockpit:

```sh
export CEZAR_VERSION=0.18.0
docker compose build
docker compose up -d --wait
```

Open <http://127.0.0.1:4321>. Cezar has no built-in authentication. Keep the
default loopback binding, or use an authenticating reverse proxy for remote access.
The default project volume is empty; add a project through Cezar or mount your
own repository at `/workspace/project` using a Compose override.

## Install and sign in to Codex

Choose an exact stable version. Run these as the default container user,
without sudo:

```sh
docker compose exec cezar codex-tool install 0.166.0
docker compose exec cezar codex login --device-auth
docker compose exec cezar codex login status
docker compose exec cezar codex-tool check
```

Follow the device-login link in your browser; your account may need device
authentication enabled. Select Codex as your runner in Cezar. Installation does
not log you in, change Cezar's upstream runner defaults, or call a paid model.
After installation, refresh the cockpit's environment checks.

To deliberately change the installed version, finish active tasks first:

```sh
docker compose exec cezar codex-tool update 0.166.0
```

`install` and `update` both delegate the requested exact version to npm.
There are no fallback versions or retries. npm failures are returned to the
caller; a failed update can leave an incomplete installation that needs repair.
`check` verifies the launcher, package location, executable and version, not login.
Nothing installs or updates agents at container startup. Claude Code and other
agents are outside this MVP.

## Persistence and image updates

Compose keeps the complete `/home/cezar` and `/workspace/project` in named
volumes. The home contains Codex's full npm prefix (`.local/bin` **and**
`.local/lib/node_modules`), `.codex` login/configuration, and Cezar settings.
Credentials are local user data, never part of the published image.

Keep the same Compose project name, volumes, UID/GID `1000:1000` and HOME when
replacing containers. Stop active tasks and back up both volumes before updates.
Rebuild with another exact `CEZAR_VERSION`, then run `docker compose up -d --wait`.
This changes Cezar independently of the Codex installed in the home volume.
The base OS, Node and installed CLI must remain compatible; test old volumes
when changing the base. Login can expire or be revoked independently.

`docker compose down` stops the service and preserves named volumes.
Adding `--volumes` deletes their data. Do not use it for a normal update.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
CEZAR_VERSION=0.18.0 CODEX_VERSION=0.166.0 CEZAR_IMAGE=cezar:local bash scripts/smoke.sh
```

The container smoke test uses an isolated, disposable Compose project. It
installs real Codex, checks the cockpit and Cezar dry-run, replaces the container
with a different image configuration, and checks persistence without reinstalling.
Only its own test volumes are removed. Its auth fixture is not a valid login;
a user must separately verify a real login survives replacement. No model call.

GitHub Actions runs on the standard `ubuntu-24.04` runner. In the Actions tab,
choose **Build and test image**, select **Run workflow**, and provide exact
Cezar and Codex versions. A manual run can rebuild the same Cezar version after
a base-image or packaging change. Pull requests also run the build and tests.
The workflow has no registry login or image publication step. Hourly stable/nightly
version monitoring is planned separately.

The Cezar version and Node base digest are pinned. Transitive npm and system
packages resolve during the build; a build is not a byte-for-byte reproducibility
guarantee. Use the resulting image digest to identify a particular artifact.

## License

The container packaging, scripts and documentation are available under the
[MIT License](LICENSE). Cezar and separately installed tools retain their own
licenses. This project packages the upstream Cezar release without modifying it.
