"""Configuration plumbing for JsonEE services.

JsonEE owns the *shape* of how a service is configured — the file
discovery layout, the option names every JsonEE app shares (storage,
HTTP bind, public base URL), and the rule that one option has three
sources (CLI flag, env var, YAML key) that resolve by precedence.

Apps pass their *app name* and get back a ConfigArgParse parser that
already knows where to look for config files and has the
JsonEE-common options registered. The app adds its own options the
same way (``parser.add(...)``) and calls :func:`parse` to get a
plain dict (PureJson principle — no settings class).

File discovery for ``app_name="nyrkiov3"`` (low → high priority,
later wins; CLI flags and env vars override all of these):

  ``/etc/nyrkiov3/config.yml``
  ``/etc/nyrkiov3/secrets.yml``
  ``~/.nyrkiov3/config.yml``
  ``~/.nyrkiov3/secrets.yml``
  ``./nyrkiov3.yml``
  ``./nyrkiov3-secrets.yml``

The two-file split (config + secrets) is a convention only — both
files share one schema; apps put values in whichever file makes
sense for distribution (config.yml in git, secrets.yml mode 0600).
"""
from __future__ import annotations

import os
from typing import Any

import configargparse


def default_config_files(app_name: str) -> list[str]:
    """Return the standard JsonEE discovery list for ``app_name``.

    Exposed so callers can introspect or patch the list (tests do
    this to isolate from the developer's real ``~/.{app}/`` files)."""
    return [
        f"/etc/{app_name}/config.yml",
        f"/etc/{app_name}/secrets.yml",
        os.path.expanduser(f"~/.{app_name}/config.yml"),
        os.path.expanduser(f"~/.{app_name}/secrets.yml"),
        f"./{app_name}.yml",
        f"./{app_name}-secrets.yml",
    ]


def create_parser(
    app_name: str,
    *,
    prog: str | None = None,
    description: str = "",
    env_prefix: str | None = None,
    with_store: bool = True,
    with_http: bool = True,
    default_storage_path: str | None = None,
    default_mongo_db: str | None = None,
    default_bind: str = "127.0.0.1:8123",
    default_base_url: str = "",
    config_files: list[str] | None = None,
) -> configargparse.ArgParser:
    """Build a JsonEE-flavoured ConfigArgParse parser.

    Pre-registers ``-c/--config`` and (when requested) the JsonEE
    service options. The caller is expected to add app-specific
    options via the returned parser and then call :func:`parse`.

    Parameters
    ----------
    app_name
        Used to compute the default config-file discovery paths and
        the default env-var prefix. Conventionally the same as the
        Python distribution name.
    env_prefix
        Prefix for environment variables. Defaults to
        ``app_name.upper() + "_"`` (e.g. ``NYRKIOV3_``). Pass a custom
        value to keep an existing prefix when an app is renamed.
    with_store, with_http
        Toggle the two groups of pre-registered options. Set both to
        ``False`` for a parser that only owns config-file plumbing
        (useful for CLI tools that don't run an HTTP server).
    default_storage_path, default_mongo_db, default_bind,
    default_base_url
        Per-app defaults for the pre-registered options.
    config_files
        Override the discovery list entirely. Defaults to
        :func:`default_config_files` for ``app_name``.
    """
    prefix = env_prefix if env_prefix is not None else app_name.upper() + "_"
    files = config_files if config_files is not None else default_config_files(app_name)
    p = configargparse.ArgParser(
        prog=prog or app_name,
        description=description,
        default_config_files=files,
        config_file_parser_class=configargparse.YAMLConfigFileParser,
        args_for_setting_config_path=["-c", "--config"],
        config_arg_help_message=(
            "extra YAML config file merged on top of the auto-discovered "
            "ones"
        ),
        add_config_file_help=True,
        add_env_var_help=True,
    )
    # Tag the parser so parse() knows which prefix was used and which
    # path-typed options to expand. Stored as attributes rather than
    # via a wrapper class to keep the parser drop-in replaceable.
    p._jsonee_env_prefix = prefix
    p._jsonee_path_options: list[str] = []

    if with_store:
        p.add("--storage-path", env_var=prefix + "STORAGE_PATH",
              default=default_storage_path,
              help="directory for embedded secantusdb data; ignored "
                   "when --mongo-uri is set")
        p._jsonee_path_options.append("storage_path")
        p.add("--mongo-uri", env_var=prefix + "MONGO_URI", default=None,
              help="external MongoDB or FerretDB connection string")
        p.add("--mongo-db", env_var=prefix + "MONGO_DB",
              default=default_mongo_db,
              help=f"database name (default {default_mongo_db!r})")

    if with_http:
        p.add("--bind", env_var=prefix + "BIND", default=default_bind,
              help=f"host:port to listen on (default {default_bind!r})")
        p.add("--base-url", env_var=prefix + "BASE_URL",
              default=default_base_url,
              help="public origin+path the service is served from; "
                   "used for redirect URIs and post-redirect Location "
                   "headers")

    return p


def parse(parser: configargparse.ArgParser,
          argv: list[str] | None = None) -> dict[str, Any]:
    """Parse argv and return a normalized config dict.

    - Drops the ``config_file`` housekeeping key (it's an input to the
      loader, not a runtime setting).
    - Expands ``~`` in any option tagged as path-typed by
      :func:`create_parser` (currently just ``storage_path``).

    App-specific normalisation (fallback chains, derived defaults)
    happens *after* this — apps own that logic since it depends on
    their option set."""
    ns = parser.parse_args(argv)
    cfg = vars(ns).copy()
    cfg.pop("config_file", None)
    for key in getattr(parser, "_jsonee_path_options", ()):
        if cfg.get(key):
            cfg[key] = os.path.expanduser(cfg[key])
    return cfg
