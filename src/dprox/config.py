"""Config schema and loader for dprox.

Mirrors `dprox-design-spec-v0.1.md` §6.2. Loaded once at startup; downstream
components consume the validated `Config` object. No per-org defaults are
baked in here — every per-org value comes from config.yml or env vars
templated by the platform's Ansible.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_CONFIG_PATH = "/etc/dprox/config.yml"
ENV_CONFIG_PATH = "DPROX_CONFIG"


class ConfigError(Exception):
    """Raised when config loading or validation fails. Maps to exit code 3."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ServerConfig(_Strict):
    bind: str = Field(min_length=3)
    # Required, no default (constitution principle 11): a size limit on the
    # trust boundary is a value whose wrong answer is invisible, so dprox
    # refuses to start rather than invent one. The estate declares it.
    max_body_bytes: int = Field(gt=0, le=10_000_000)

    @field_validator("bind")
    @classmethod
    def _validate_bind(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("bind must be 'host:port'")
        host, _, port_s = v.rpartition(":")
        if not host or not port_s:
            raise ValueError("bind must be 'host:port'")
        try:
            port = int(port_s)
        except ValueError as exc:
            raise ValueError("bind port must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("bind port must be in 1..65535")
        return v

    @property
    def host(self) -> str:
        return self.bind.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.bind.rsplit(":", 1)[1])


class MTLSConfig(_Strict):
    ca_cert_path: Path
    server_cert_path: Path
    server_key_path: Path
    client_cert_mode: Literal["optional", "required", "off"] = "optional"
    tls_min_version: Literal["TLSv1.2", "TLSv1.3"] = "TLSv1.3"
    tls_pin_enabled: bool = True


class PlanConfig(_Strict):
    compiled_plan_path: Path
    cache_enabled: bool = True
    reload_on_mtime_change: bool = True
    reload_on_unknown_cn: bool = True
    reload_min_interval_seconds: int = Field(default=5, ge=0, le=3600)


class EmbeddingConfig(_Strict):
    endpoint: str = Field(min_length=1)
    model: str = Field(min_length=1)
    vector_dim: int = Field(gt=0, le=8192)
    timeout_seconds: int = Field(gt=0, le=300)


class QdrantConfig(_Strict):
    url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    # One dprox serves every org, so the collection is no longer one name:
    # it is a collection for each org, keyed by the org name the plan's
    # groups[] section states. There is no default and no derivation from
    # the org name — the estate declares both sides (constitution §10, §11).
    collections: dict[str, str] = Field(min_length=1)
    default_limit: int = Field(gt=0)
    max_limit: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0, le=300)

    @field_validator("collections")
    @classmethod
    def _collections_are_distinct(cls, v: dict[str, str]) -> dict[str, str]:
        """Refuse blanks, and refuse two orgs pointing at one collection.

        Two orgs sharing a collection would put their passages in one place
        and make the org check a no-op: every caller would reach both orgs'
        passages, held back only by the group filter. That is the one failure
        the two checks exist to survive, so it is refused at startup.
        """
        for org, collection in v.items():
            if not org.strip():
                raise ValueError("an org name in collections is empty")
            if not collection.strip():
                raise ValueError(f"collection for org {org!r} is empty")
        seen: dict[str, str] = {}
        for org, collection in v.items():
            if collection in seen:
                raise ValueError(
                    f"orgs {seen[collection]!r} and {org!r} both name collection "
                    f"{collection!r}; one collection holds one org"
                )
            seen[collection] = org
        return v

    @field_validator("max_limit")
    @classmethod
    def _max_ge_default(cls, v: int, info) -> int:
        default = info.data.get("default_limit")
        if default is not None and v < default:
            raise ValueError("max_limit must be >= default_limit")
        return v


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"
    log_query_text: bool = False


class Config(_Strict):
    # No top-level `org:`. One dprox answers every org, so there is no single
    # org it belongs to; the orgs it serves are the keys of
    # `qdrant.collections`, and which of them a caller reaches is decided per
    # request from the plan. `extra="forbid"` means a config still carrying
    # `org:` fails at startup rather than being quietly ignored.
    server: ServerConfig
    mtls: MTLSConfig
    plan: PlanConfig
    embedding: EmbeddingConfig
    qdrant: QdrantConfig
    logging: LoggingConfig = LoggingConfig()


def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env)
    return Path(DEFAULT_CONFIG_PATH)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    resolved = resolve_config_path(path)

    if not resolved.exists():
        raise ConfigError(f"config file not found: {resolved}")
    if not resolved.is_file():
        raise ConfigError(f"config path is not a file: {resolved}")

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read config {resolved}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error in {resolved}: {exc}") from exc

    if raw is None:
        raise ConfigError(f"config file is empty: {resolved}")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config root must be a mapping, got {type(raw).__name__}: {resolved}"
        )

    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"config validation failed for {resolved}:\n{exc}") from exc
