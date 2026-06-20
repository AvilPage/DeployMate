import os
import sys

import click
import yaml
from fabric import Connection
from loguru import logger

from deploymate import core

LOCAL_CONFIG = "deploymate.yml"  # cwd: optional per-project override


def global_config_path():
    """~/.config/deploymate/config.yml (XDG-aware), overridable via env."""
    if os.environ.get("DEPLOYMATE_CONFIG"):
        return os.environ["DEPLOYMATE_CONFIG"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "deploymate", "config.yml")


def project_key():
    """Identity for the current project in the global config.

    Defaults to the absolute cwd so deploys from different project trees never
    clobber each other. Override with DEPLOYMATE_PROJECT to share one block
    across directories (e.g. a checkout and a worktree of the same app).
    """
    return os.environ.get("DEPLOYMATE_PROJECT") or os.getcwd()


def _read(path):
    if path and os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def load_config():
    """Resolve config for the current project.

    Global config is keyed by project (see `project_key`); we read this
    project's block, fall back to legacy top-level keys for pre-multi-project
    configs, then let a cwd `deploymate.yml` override everything.
    """
    g = _read(global_config_path())
    legacy = {k: v for k, v in g.items() if k != "projects"}
    cfg = {**legacy, **g.get("projects", {}).get(project_key(), {})}
    cfg.update(_read(LOCAL_CONFIG))
    return cfg


def save_config(data, local=False):
    if local:
        path = LOCAL_CONFIG
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        merged = {**_read(path), **data}
        with open(path, "w") as f:
            yaml.safe_dump(merged, f, sort_keys=False)
        return path

    path = global_config_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    g = _read(path)
    projects = g.setdefault("projects", {})
    key = project_key()
    projects[key] = {**projects.get(key, {}), **data}
    with open(path, "w") as f:
        yaml.safe_dump(g, f, sort_keys=False)
    return path


def _setup(ctx, machine, compose_file, log_level, email):
    """Build the shared deploy context. CLI flags win over deploymate.yml."""
    cfg = load_config()
    machine = machine or cfg.get("machine")
    email = email or cfg.get("email")
    compose_file = compose_file or cfg.get("compose_file") or "docker-compose.yml"

    if not machine:
        raise click.UsageError(
            "No machine specified. Pass --machine or run 'deploymate init' first."
        )

    logger.remove()
    logger.add(sys.stderr, level=log_level.upper(),
               format="<level>{level: <8}</level> {message}")

    ctx.connection = Connection(machine)
    try:
        ctx.connection.open()
        logger.info("ssh connection to {} succeeded", machine)
    except Exception as exc:
        logger.error("ssh connection to {} failed: {}", machine, exc)
        raise SystemExit(1)

    ctx.compose_file = compose_file
    ctx.compose_dir = os.path.dirname(os.path.abspath(compose_file))
    ctx.compose_yaml = core.get_compose(compose_file)
    ctx.email = email
    ctx.hide = log_level.upper() != "DEBUG"


machine_opt = click.option(
    "--machine", "-m", default=None,
    help="SSH target, e.g. ubuntu@avilpage.com (defaults to deploymate.yml).",
)
compose_opt = click.option(
    "--compose-file", "-f", default=None,
    help="Path to compose file (default: docker-compose.yml).",
)
loglevel_opt = click.option("--log-level", default="INFO", help="Logging level.")
email_opt = click.option(
    "--email", default=None,
    help="Email for Let's Encrypt (ACME) certificate registration.",
)


@click.group()
def deploymate():
    """DeployMate - single-command, registry-less Docker deploys over SSH."""


@deploymate.command()
@click.option("--machine", "-m", required=True,
              help="SSH target, e.g. ubuntu@avilpage.com")
@compose_opt
@loglevel_opt
@email_opt
@click.option("--local", is_flag=True,
              help="Save to ./deploymate.yml instead of global ~/.config.")
@click.pass_context
def init(ctx, machine, compose_file, log_level, email, local):
    """Save host config and bootstrap the host (docker + Caddy).

    Connection config is saved globally to ~/.config/deploymate/config.yml
    under a block keyed by this project's directory, so multiple projects
    coexist without overwriting each other. Use --local to pin it in
    ./deploymate.yml instead.
    """
    data = {"machine": machine}
    if email:
        data["email"] = email
    if compose_file:
        data["compose_file"] = compose_file
    path = save_config(data, local=local)

    _setup(ctx, machine, compose_file, log_level, email)
    core.init(ctx)
    logger.success("host ready; config saved to {}", path)


@deploymate.command()
@machine_opt
@compose_opt
@loglevel_opt
@email_opt
@click.pass_context
def deploy(ctx, machine, compose_file, log_level, email):
    """Build on the remote host, health-check, then swap Caddy to the new version."""
    _setup(ctx, machine, compose_file, log_level, email)
    core.init(ctx)
    core.deploy_compose(ctx)


@deploymate.command()
@click.argument("service")
@machine_opt
@compose_opt
@loglevel_opt
@email_opt
@click.pass_context
def rollback(ctx, service, machine, compose_file, log_level, email):
    """Roll a SERVICE back to its previous image version."""
    _setup(ctx, machine, compose_file, log_level, email)
    core.rollback(ctx, service)


@deploymate.command()
@machine_opt
@compose_opt
@loglevel_opt
@click.pass_context
def status(ctx, machine, compose_file, log_level):
    """Show deploymate-managed containers on the host."""
    _setup(ctx, machine, compose_file, log_level, None)
    core.status(ctx)


@deploymate.command()
@click.argument("service", required=False)
@machine_opt
@compose_opt
@loglevel_opt
@click.option("--follow", "-F", is_flag=True, help="Follow log output.")
@click.option("--tail", default=100, help="Lines to show from the end.")
@click.pass_context
def logs(ctx, service, machine, compose_file, log_level, follow, tail):
    """Stream logs from a SERVICE, or all deploymate containers if omitted."""
    _setup(ctx, machine, compose_file, log_level, None)
    core.logs(ctx, service, follow=follow, tail=tail)


if __name__ == "__main__":
    deploymate()
