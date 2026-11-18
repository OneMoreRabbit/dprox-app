"""Where dprox-query gets its address, its certificate and its key.

Every one of those four values is declared in the configuration file. None has
a default and none is derived from another (constitution §11): a tool that
guessed an address would search the wrong estate and say nothing, and a tool
that guessed a key path would fall back to an identity nobody chose.

The configuration file itself is a path, and a wrong path fails loudly with
the name of the file it looked for, so that one has a fallback chain.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ENV_CONFIG_PATH = "DPROX_QUERY_CONFIG"

DEFAULT_CONFIG_PATH = Path("~/.config/dprox-query/config.yml").expanduser()
"""The estate writes the configuration, the certificate and the key into the
seat's persistent home — never into an image (agent-skeleton, hub #13765)."""

REQUIRED = ("endpoint", "ca_cert_path", "client_cert_path", "client_key_path")


class ConfigError(Exception):
    """Configuration missing, unreadable or incomplete. Exit code 2."""


@dataclass(frozen=True)
class QueryConfig:
    endpoint: str
    ca_cert_path: Path
    client_cert_path: Path
    client_key_path: Path
    timeout_seconds: int = 30
    """A tunable, not a declared value: a timeout that is wrong announces
    itself by timing out. The four above are not, so they have no default."""

    source: Path | None = None


def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env)
    return DEFAULT_CONFIG_PATH


def load_config(path: str | os.PathLike[str] | None = None) -> QueryConfig:
    resolved = resolve_config_path(path)

    if not resolved.exists():
        raise ConfigError(
            f"config file not found: {resolved}\n"
            f"The estate writes it. Set {ENV_CONFIG_PATH} or pass --config to "
            f"point at another."
        )
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read config {resolved}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error in {resolved}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {resolved}")

    missing = [key for key in REQUIRED if not str(raw.get(key) or "").strip()]
    if missing:
        raise ConfigError(
            f"config {resolved} is missing: {', '.join(missing)}. "
            f"Every one is required and none has a default."
        )

    unknown = sorted(set(raw) - set(REQUIRED) - {"timeout_seconds"})
    if unknown:
        # A key nobody reads is a setting someone believes is in force.
        raise ConfigError(
            f"config {resolved} has key(s) dprox-query does not read: "
            f"{', '.join(unknown)}"
        )

    endpoint = str(raw["endpoint"]).strip()
    if not endpoint.startswith("https://"):
        raise ConfigError(
            f"endpoint must be https://, got {endpoint!r}: dprox is reached "
            f"with a client certificate and there is no plain-HTTP mode"
        )

    timeout = raw.get("timeout_seconds", 30)
    if not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ConfigError("timeout_seconds must be a whole number of seconds, 1..300")

    return QueryConfig(
        endpoint=endpoint.rstrip("/"),
        ca_cert_path=Path(str(raw["ca_cert_path"])).expanduser(),
        client_cert_path=Path(str(raw["client_cert_path"])).expanduser(),
        client_key_path=Path(str(raw["client_key_path"])).expanduser(),
        timeout_seconds=timeout,
        source=resolved,
    )


def missing_files(config: QueryConfig) -> list[str]:
    """The configured files that are not there, named as the config named them.

    Checked before the first connection: a missing key file otherwise surfaces
    as a TLS handshake error, which reads like the server's fault.
    """
    return [
        f"{name}: {path}"
        for name, path in (
            ("ca_cert_path", config.ca_cert_path),
            ("client_cert_path", config.client_cert_path),
            ("client_key_path", config.client_key_path),
        )
        if not path.is_file()
    ]
