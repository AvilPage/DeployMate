# DeployMate

Single command to deploy your Docker Compose app to any server with SSH access.
No registry, no agent/daemon on the host, no orchestrator.

DeployMate streams your build context over SSH, builds the image **on the
remote host** (native arch, BuildKit cache), runs a freshly versioned
container, health-checks it, then atomically points a built-in Caddy reverse
proxy (auto-HTTPS) at the new version. The previous version keeps serving until
the new one is healthy, so a failed deploy never causes downtime.

## Installation

```bash
pip install deploymate    # or: uvx deploymate
```

`deploymate` is aliased to `dm`.

## Usage

```bash
# one-time: save host config + bootstrap (docker, perms, network, Caddy)
deploymate init --machine ubuntu@avilpage.com --email you@avilpage.com

# afterwards, no --machine needed
deploymate deploy
deploymate rollback web
deploymate status
deploymate logs web -F
```

## Config

Like `kubectl`/`uncloud`, connection config is global; the app spec (compose)
is per-project.

- **Global** `~/.config/deploymate/config.yml` — `machine`, `email`. Written by
  `init`. Override path with `$DEPLOYMATE_CONFIG`. Deploy from any directory.
- **Per-project** `./deploymate.yml` (optional) — override the host/email for a
  specific app. Write it with `deploymate init --local`.
- **Precedence:** CLI flag > `./deploymate.yml` > global config > default.

## Compose config

Add an `x-deploymate` block to any service you want exposed via the proxy:

```yaml
services:
  web:
    build: .
    environment:
      DATABASE_URL: postgres://db/app
    x-deploymate:
      domain: app.avilpage.com   # Caddy routes this host to the container
      port: 8080                 # container port the app listens on
      health: /healthz           # optional HTTP health check, gates the swap

  db:
    image: postgres:16           # services without x-deploymate run as-is
    volumes:
      - pgdata:/var/lib/postgresql/data
```

## How it works

- **No registry** — build context is tar-streamed over SSH and built on the host.
- **No daemon** — all state lives in Docker labels (`dm.service`, `dm.version`);
  nothing extra runs on the host besides your containers and Caddy.
- **Zero-downtime** — new container is health-checked before Caddy swaps to it;
  old containers are removed only after the swap.
- **Rollback** — prior image tags (`<service>:dm-<version>`) are kept on the
  host; `dm rollback` re-runs the previous one.

## License

MIT
