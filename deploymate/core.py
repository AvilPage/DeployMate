"""DeployMate core: registry-less, daemonless single-host Docker deploys over SSH.

Model: build the image *on the remote host* (no registry), run a freshly
versioned container, health-check it, then atomically point Caddy at the new
version. Old containers are removed only after the swap succeeds, so a failed
deploy never takes the running app down. All deploy state lives in Docker
labels on the host -- there is no agent/daemon and no local state file.
"""

import io
import json
import os
import tarfile
import time

import yaml
from invoke.exceptions import UnexpectedExit
from loguru import logger

NETWORK = "deploymate"
ADMIN = "http://localhost:2019"
LABEL = "tool=deploymate"


def remote_base(context):
    """Per-user state dir on the host (`~/.deploymate`), resolved to an
    absolute path so it can be bind-mounted into containers. Cached per run."""
    base = getattr(context, "remote_base_dir", None)
    if base is None:
        home = context.connection.run("echo $HOME", hide=True).stdout.strip()
        base = f"{home}/.deploymate"
        context.connection.run(f"mkdir -p {base}/build", hide=True)
        context.remote_base_dir = base
    return base


# --------------------------------------------------------------------------- #
# compose parsing
# --------------------------------------------------------------------------- #

def get_compose(compose_path):
    with open(compose_path, "r") as f:
        return yaml.safe_load(f)


def _container_port(spec):
    """Extract the container-side port from a compose `ports` entry.

    Handles "8000", "8000:8000", "127.0.0.1:8000:8000", "8000:8000/tcp" and
    the long form {target: 8000, published: ...}. The container port is the
    last numeric field.
    """
    if isinstance(spec, dict):
        port = spec.get("target")
    else:
        port = str(spec).split("/")[0].split(":")[-1]
    try:
        return int(port)
    except (TypeError, ValueError):
        return None


def service_config(service, context=None):
    """Pull the deploymate routing block out of a compose service.

    Supports both `x-deploymate` (compose-spec extension field) and a bare
    `deploymate` key. Returns {} when the service is not web-facing (e.g. a db).

    Routing is inferred so plain composes work untouched:

    * A service that publishes `ports:` is treated as web-facing -- the
      container-side port becomes the Caddy upstream and the service is proxied
      at the machine hostname instead of binding a host port (this is what lets
      blue-green redeploys avoid host-port collisions).
    * An explicit `x-deploymate` block overrides the inference; set
      `proxy: false` in it to keep raw host-port publishing (e.g. for a db).
    * A web-facing service with no explicit `domain` defaults to the deploy
      machine's hostname.
    """
    cfg = dict(service.get("x-deploymate") or service.get("deploymate") or {})

    # Explicit opt-out: keep raw host-port publishing, no Caddy route.
    if cfg.get("proxy") is False:
        return {}

    # Infer a web service from published ports when no explicit block exists.
    if not cfg and service.get("ports"):
        for spec in service["ports"]:
            port = _container_port(spec)
            if port:
                cfg = {"port": port}
                break

    if cfg and not cfg.get("domain") and context is not None:
        host = getattr(getattr(context, "connection", None), "host", None)
        if host:
            cfg["domain"] = host
    return cfg


# --------------------------------------------------------------------------- #
# host bootstrap
# --------------------------------------------------------------------------- #

def install_docker(context):
    res = context.connection.run("docker --version", hide=True, warn=True)
    if res.ok:
        logger.info("docker present: {}", res.stdout.strip())
        return
    logger.info("docker not found, installing via get.docker.com")
    context.connection.run(
        "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && "
        "sh /tmp/get-docker.sh && rm -f /tmp/get-docker.sh",
        hide=context.hide,
    )


def ensure_docker_access(context):
    """Make sure the SSH user can talk to the Docker socket without sudo.

    On a fresh host the login user is usually not in the `docker` group, so
    socket calls fail with "permission denied". We add the user to the group
    (passwordless sudo) and then reopen the SSH connection -- group membership
    only takes effect on a new login session, so reconnecting is what makes it
    active for the rest of this deploy.
    """
    if context.connection.run("docker ps", hide=True, warn=True).ok:
        return

    user = context.connection.user
    logger.info("docker socket denied; adding {} to docker group", user)
    added = context.connection.run(
        f"sudo -n usermod -aG docker {user}", hide=True, warn=True
    )
    if not added.ok:
        raise SystemExit(
            "Docker permission denied and could not auto-fix (needs passwordless "
            f"sudo). Run on the host once:\n  sudo usermod -aG docker {user}\n"
            "then reconnect and retry."
        )

    # New group membership requires a fresh login session.
    context.connection.close()
    if not context.connection.run("docker ps", hide=True, warn=True).ok:
        raise SystemExit(
            f"Added {user} to docker group but socket still denied. "
            "Reconnect (new SSH session) and retry."
        )
    logger.info("docker access granted to {}", user)


def ensure_network(context):
    context.connection.run(
        f"docker network inspect {NETWORK} >/dev/null 2>&1 || "
        f"docker network create {NETWORK}",
        hide=True,
    )


def setup_caddy(context):
    """Run Caddy as the shared reverse proxy with its admin API reachable.

    Admin binds 0.0.0.0:2019 *inside* the container and is published only to
    the host loopback, so config pushes go over SSH (`curl localhost:2019`)
    and the admin API is never exposed publicly. Cert/data volumes persist
    Let's Encrypt certificates across deploys.
    """
    caddyfile = f"{remote_base(context)}/Caddyfile"
    # Minimal bootstrap config; real routes arrive later via the admin API.
    bootstrap = "{\n\tadmin 0.0.0.0:2019\n}\n"
    context.connection.put(io.StringIO(bootstrap), caddyfile)

    running = context.connection.run(
        "docker ps -q --filter name=^caddy$", hide=True, warn=True
    ).stdout.strip()
    if running:
        logger.info("caddy already running")
        return

    logger.info("starting caddy reverse proxy")
    context.connection.run(
        "docker run -d --name caddy --restart unless-stopped "
        f"--network {NETWORK} --label {LABEL} "
        "-p 80:80 -p 443:443 -p 127.0.0.1:2019:2019 "
        "-v caddy_data:/data -v caddy_config:/config "
        f"-v {caddyfile}:/etc/caddy/Caddyfile "
        "caddy",
        hide=context.hide,
    )
    # Give the admin API a moment to come up.
    _wait_for_admin(context)


def _wait_for_admin(context, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = context.connection.run(
            f"curl -fsS -m 2 {ADMIN}/config/ >/dev/null", hide=True, warn=True
        )
        if res.ok:
            return
        time.sleep(1)
    logger.warning("caddy admin API did not respond within {}s", timeout)


def init(context):
    install_docker(context)
    ensure_docker_access(context)
    ensure_network(context)
    setup_caddy(context)


# --------------------------------------------------------------------------- #
# remote build (no registry)
# --------------------------------------------------------------------------- #

def _parse_dockerignore(ctx_dir):
    """Return a list of (negated, pattern) tuples from .dockerignore, if present."""
    path = os.path.join(ctx_dir, ".dockerignore")
    rules = []
    if not os.path.isfile(path):
        return rules
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            pattern = line[1:] if negated else line
            rules.append((negated, pattern.rstrip("/")))
    return rules



def _tar_context(ctx_dir):
    """Pack a build context directory into an in-memory .tar.gz, respecting .dockerignore.

    Uses a manual walk so excluded directories are never traversed — avoids
    scanning Flutter build/ or node_modules/ even when they are excluded.
    """
    rules = _parse_dockerignore(ctx_dir)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(ctx_dir):
            rel_root = os.path.relpath(root, ctx_dir)
            if rel_root == ".":
                rel_root = ""

            # Prune dirs in-place to avoid descending into excluded directories
            dirs[:] = [
                d for d in dirs
                if _is_included(os.path.join(rel_root, d) if rel_root else d, rules)
            ]

            for fname in files:
                rel_path = os.path.join(rel_root, fname) if rel_root else fname
                if _is_included(rel_path, rules):
                    tar.add(
                        os.path.join(root, fname),
                        arcname=os.path.join(".", rel_path),
                    )
    buf.seek(0)
    return buf


def _is_included(rel, rules):
    """Return True if rel path should be included per .dockerignore rules (last-match wins).
    With no rules, everything is included.
    """
    if not rules:
        return True
    import fnmatch
    excluded = False
    for negated, pattern in rules:
        if (
            fnmatch.fnmatch(rel, pattern)
            or fnmatch.fnmatch(os.path.basename(rel), pattern)
            or rel.startswith(pattern + "/")
            or rel == pattern
        ):
            excluded = not negated
    return not excluded


def remote_build(context, service_name, service, version):
    """Stream the build context to the host and `docker build` it there.

    Builds run on the remote host so the image is native to the server's
    architecture (no cross-arch emulation) and BuildKit's layer cache makes
    repeat deploys fast. Returns the built image tag.
    """
    build = service["build"]
    if isinstance(build, str):
        rel_ctx, dockerfile, build_args = build, "Dockerfile", {}
    else:
        rel_ctx = build.get("context", ".")
        dockerfile = build.get("dockerfile", "Dockerfile")
        build_args = build.get("args", {}) or {}

    ctx_dir = os.path.normpath(os.path.join(context.compose_dir, rel_ctx))
    if not os.path.isdir(ctx_dir):
        raise FileNotFoundError(f"build context not found: {ctx_dir}")

    image = f"{service_name}:dm-{version}"
    remote_dir = f"{remote_base(context)}/build/{service_name}-{version}"
    tarball = f"/tmp/dm-{service_name}-{version}.tar.gz"

    logger.info("[{}] packing build context {}", service_name, ctx_dir)
    packed = _tar_context(ctx_dir)
    size_kb = packed.seek(0, 2) / 1024
    packed.seek(0)
    logger.info("[{}] uploading {:.1f} KB to remote", service_name, size_kb)
    context.connection.put(packed, tarball)

    logger.info("[{}] extracting + building on remote {}", service_name, remote_dir)
    context.connection.run(f"mkdir -p {remote_dir}", hide=True)
    context.connection.run(f"tar xzf {tarball} -C {remote_dir}", hide=True)
    context.connection.run(f"rm -f {tarball}", hide=True)

    # Cache the first extracted context dir so non-build services can resolve
    # relative volume paths against it (e.g. ./templates from the same repo).
    if not getattr(context, "compose_remote_dir", None):
        context.compose_remote_dir = remote_dir

    arg_flags = " ".join(
        f"--build-arg {k}={v}" for k, v in build_args.items()
    )
    context.connection.run(
        f"DOCKER_BUILDKIT=1 docker build {arg_flags} "
        f"-t {image} -f {remote_dir}/{dockerfile} {remote_dir}",
        hide=context.hide,
    )
    return image


# --------------------------------------------------------------------------- #
# container lifecycle
# --------------------------------------------------------------------------- #

def _parse_env_file(path):
    """Parse a .env file into a list of (key, value) pairs, skipping comments."""
    pairs = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                pairs.append((k.strip(), v.strip()))
    except FileNotFoundError:
        logger.warning("env_file not found: {}", path)
    return pairs


def _env_flags(service, compose_dir=None):
    merged = {}

    # env_file entries first (lower priority)
    for ef in service.get("env_file", []):
        ef_path = ef if os.path.isabs(ef) else os.path.join(compose_dir or ".", ef)
        for k, v in _parse_env_file(ef_path):
            merged[k] = v

    # environment: overrides env_file values
    env = service.get("environment", {})
    pairs = env.items() if isinstance(env, dict) else (
        (e.split("=", 1) + [""])[:2] for e in env
    )
    for k, v in pairs:
        merged[k] = v

    return " ".join(f'-e {k}={_shell_quote(v)}' for k, v in merged.items())


def _volume_flags(service, remote_build_dir=None):
    flags = []
    for v in service.get("volumes", []):
        if remote_build_dir and isinstance(v, str) and v.startswith("./"):
            # Resolve host-side relative path against the remote build dir
            rel = v[2:].split(":")[0]
            rest = v[len("./" + rel):]
            v = f"{remote_build_dir}/{rel}{rest}"
        flags.append(f"-v {v}")
    return " ".join(flags)


def _shell_quote(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def run_container(context, service_name, service, image, version):
    """Start a new versioned container on the deploymate network.

    Web app ports are NOT published to the host -- Caddy reaches the container
    by name over the shared network. Non-web services (no deploymate block)
    keep their compose port mappings so they behave as before.

    A --network-alias for the bare service name is added so inter-service
    references in compose (e.g. http://viewer:80/) resolve correctly.
    """
    cfg = service_config(service, context)
    container = f"{service_name}-{version}"
    labels = (
        f"--label {LABEL} "
        f"--label dm.service={service_name} "
        f"--label dm.version={version}"
    )
    if cfg.get("domain"):
        labels += f" --label dm.domain={cfg['domain']}"

    # Only non-proxied services publish ports to the host.
    port_flags = ""
    if not cfg.get("domain"):
        port_flags = " ".join(f"-p {p}" for p in service.get("ports", []))

    remote_build_dir = f"{remote_base(context)}/build/{service_name}-{version}"
    # For non-build services, resolve relative volumes from the compose context dir.
    vol_base = remote_build_dir if "build" in service else (
        getattr(context, "compose_remote_dir", None) or remote_build_dir
    )
    cmd = (
        f"docker run -d --name {container} --restart unless-stopped "
        f"--network {NETWORK} --network-alias {service_name} {labels} "
        f"{_env_flags(service, getattr(context, 'compose_dir', None))} "
        f"{_volume_flags(service, vol_base)} {port_flags} {image}"
    )
    logger.info("[{}] starting {}", service_name, container)
    context.connection.run(cmd, hide=context.hide)
    return container


def wait_healthy(context, container, cfg, timeout=60):
    """Block until the new container is serving, or time out.

    With a `health` path we probe over HTTP from the Caddy container (alpine
    busybox has wget), which proves the app actually answers. Without one we
    fall back to confirming the container is up and not crash-looping.
    """
    health = cfg.get("health")
    port = cfg.get("port")
    deadline = time.time() + timeout

    if health and port:
        url = f"http://{container}:{port}{health}"
        logger.info("[{}] health probe {}", container, url)
        while time.time() < deadline:
            res = context.connection.run(
                f"docker exec caddy wget -q -T 3 -O /dev/null {url}",
                hide=True, warn=True,
            )
            if res.ok:
                logger.info("[{}] healthy", container)
                return True
            time.sleep(2)
        logger.error("[{}] failed health check", container)
        return False

    # No HTTP health: ensure it stays running for a short grace period.
    time.sleep(3)
    state = context.connection.run(
        f"docker inspect -f '{{{{.State.Running}}}}' {container}",
        hide=True, warn=True,
    ).stdout.strip()
    ok = state == "true"
    logger.info("[{}] running={}", container, ok)
    return ok


def remove_old_containers(context, service_name, keep_version):
    """Remove prior containers for a service, keeping the just-deployed one."""
    ids = context.connection.run(
        f"docker ps -aq --filter label=dm.service={service_name}",
        hide=True, warn=True,
    ).stdout.split()
    for cid in ids:
        ver = context.connection.run(
            f"docker inspect -f '{{{{index .Config.Labels \"dm.version\"}}}}' {cid}",
            hide=True, warn=True,
        ).stdout.strip()
        if ver != keep_version:
            logger.info("[{}] removing old container {}", service_name, cid)
            context.connection.run(f"docker rm -f {cid}", hide=True, warn=True)


# --------------------------------------------------------------------------- #
# caddy routing (admin API, full-config load)
# --------------------------------------------------------------------------- #

def build_caddy_config(routes, email=None):
    """Build a complete Caddy JSON config from the live route set.

    We POST the whole config to /load (idempotent) rather than patching
    individual routes -- simpler to reason about and the swap is atomic.
    """
    http_routes = [
        {
            "match": [{"host": [r["domain"]]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": f"{r['container']}:{r['port']}"}],
                }
            ],
        }
        for r in routes
    ]
    config = {
        "admin": {"listen": "0.0.0.0:2019"},
        "apps": {
            "http": {
                "servers": {
                    "srv0": {"listen": [":80", ":443"], "routes": http_routes}
                }
            }
        },
    }
    if email:
        config["apps"]["tls"] = {
            "automation": {
                "policies": [{"issuers": [{"module": "acme", "email": email}]}]
            }
        }
    return config


def apply_caddy_routes(context, routes, email=None):
    """Push the route set to Caddy's admin API, switching live upstreams."""
    config = build_caddy_config(routes, email)
    remote_json = "/tmp/dm-caddy.json"
    context.connection.put(io.StringIO(json.dumps(config)), remote_json)
    logger.info("updating caddy routes: {}", [r["domain"] for r in routes])
    context.connection.run(
        f"curl -fsS -X POST -H 'Content-Type: application/json' "
        f"--data @{remote_json} {ADMIN}/load",
        hide=context.hide,
    )
    context.connection.run(f"rm -f {remote_json}", hide=True)


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

def deploy_compose(context):
    """Deploy every service, then atomically swap Caddy to the new versions.

    Build + run + health-check happens per service first. Caddy is only
    re-pointed once *all* web services are healthy, so a failure anywhere
    leaves the previous deploy serving untouched.
    """
    version = str(int(time.time()))
    services = context.compose_yaml.get("services", {})
    routes = []
    new_containers = {}

    for name, service in services.items():
        cfg = service_config(service, context)

        if "build" in service:
            image = remote_build(context, name, service, version)
        else:
            image = service["image"]
            logger.info("[{}] pulling base image {}", name, image)
            context.connection.run(f"docker pull {image}", hide=context.hide)

        container = run_container(context, name, service, image, version)
        new_containers[name] = container

        if not wait_healthy(context, container, cfg):
            logger.error("[{}] unhealthy -- aborting, previous deploy intact", name)
            context.connection.run(f"docker rm -f {container}", hide=True, warn=True)
            raise SystemExit(1)

        if cfg.get("domain") and cfg.get("port"):
            routes.append(
                {"domain": cfg["domain"], "container": container, "port": cfg["port"]}
            )

    if routes:
        apply_caddy_routes(context, routes, email=context.email)

    for name in services:
        remove_old_containers(context, name, version)

    logger.success("deployed version {}", version)


def rollback(context, service_name):
    """Re-deploy the previous image for a service and swap Caddy back.

    Old containers are pruned after each deploy, but their images are kept
    (tagged `<service>:dm-<version>`), so rollback runs the prior image tag.
    """
    tags = context.connection.run(
        f"docker images {service_name} --filter reference='{service_name}:dm-*' "
        "--format '{{.Tag}}'",
        hide=True, warn=True,
    ).stdout.split()
    # Tags look like dm-<unix-ts>; newest first, second entry is the previous.
    versions = sorted(
        (t.replace("dm-", "") for t in tags if t.startswith("dm-")),
        reverse=True,
    )
    if len(versions) < 2:
        logger.error("[{}] no previous version to roll back to", service_name)
        raise SystemExit(1)

    prev = versions[1]
    image = f"{service_name}:dm-{prev}"
    service = context.compose_yaml.get("services", {}).get(service_name, {})
    cfg = service_config(service, context)

    logger.info("[{}] rolling back to {}", service_name, image)
    container = run_container(context, service_name, service, image, prev)
    if not wait_healthy(context, container, cfg):
        context.connection.run(f"docker rm -f {container}", hide=True, warn=True)
        logger.error("[{}] rollback target unhealthy", service_name)
        raise SystemExit(1)

    if cfg.get("domain") and cfg.get("port"):
        apply_caddy_routes(
            context,
            [{"domain": cfg["domain"], "container": container, "port": cfg["port"]}],
            email=context.email,
        )
    remove_old_containers(context, service_name, prev)
    logger.success("[{}] rolled back to {}", service_name, prev)


def status(context):
    """Print deploymate-managed containers on the host."""
    out = context.connection.run(
        "docker ps -a --filter label=tool=deploymate "
        "--format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}'",
        hide=True, warn=True,
    ).stdout
    print(out)


def _stream(context, cmd, follow):
    """Run a (possibly follow) command, treating Ctrl-C as a clean exit.

    Following logs is ended by the user with Ctrl-C, which kills the remote
    process and makes invoke raise UnexpectedExit -- that's normal, not a
    failure, so swallow it when following.
    """
    try:
        context.connection.run(cmd, pty=follow)
    except (UnexpectedExit, KeyboardInterrupt):
        if not follow:
            raise


def logs(context, service_name=None, follow=False, tail=100):
    """Stream logs from one service, or all deploymate containers if none given."""
    if service_name:
        cid = context.connection.run(
            f"docker ps -q --filter label=dm.service={service_name}",
            hide=True, warn=True,
        ).stdout.strip()
        if not cid:
            logger.error("[{}] no running container", service_name)
            raise SystemExit(1)
        flags = f"--tail {tail}" + (" -f" if follow else "")
        _stream(context, f"docker logs {flags} {cid}", follow)
        return

    # No service: show a tail for every deploymate-managed container.
    names = context.connection.run(
        "docker ps --filter label=tool=deploymate --format '{{.Names}}'",
        hide=True, warn=True,
    ).stdout.split()
    if not names:
        logger.error("no deploymate containers running")
        raise SystemExit(1)

    if follow:
        # docker logs -f is single-container: fan out in a shell loop, prefix
        # each line with its container name, and wait so Ctrl-C tears all down.
        joined = " ".join(names)
        cmd = (
            f"for c in {joined}; do "
            f'(docker logs -f --tail {tail} "$c" 2>&1 | sed "s/^/[$c] /") & '
            "done; wait"
        )
        _stream(context, cmd, follow=True)
        return

    for name in names:
        print(f"\n===== {name} =====")
        context.connection.run(f"docker logs --tail {tail} {name}")
