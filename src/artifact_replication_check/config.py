"""Load and validate the YAML config."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """The config file is missing, malformed or references unset env vars."""


@dataclass
class Site:
    name: str
    url: str
    kind: str = "registry"  # "registry" (Docker Registry HTTP API v2) or "generic"
    timeout: float = 10.0
    username: str | None = None
    password: str | None = None
    token: str | None = None


@dataclass
class DnsSpec:
    name: str
    expect_addresses: list[str] = field(default_factory=list)
    expect_cname: str | None = None


@dataclass
class Config:
    source: Site
    target: Site
    repositories: list[str]
    compare_digests: bool = False
    dns: DnsSpec | None = None


def _env(var: str, where: str) -> str:
    value = os.environ.get(var)
    if not value:
        raise ConfigError(f"{where}: environment variable {var} is not set")
    return value


def _site(raw: dict, role: str, timeout: float, base_dir: Path) -> Site:
    if not isinstance(raw, dict) or not raw.get("url"):
        raise ConfigError(f"'{role}' must be a mapping with at least a 'url'")
    kind = raw.get("type", "registry")
    if kind not in ("registry", "generic"):
        raise ConfigError(f"{role}.type must be 'registry' or 'generic', got {kind!r}")
    url = str(raw["url"]).rstrip("/")
    if kind == "generic" and not url.startswith(("http://", "https://")):
        url = (base_dir / url).as_posix()  # local JSON file, relative to the config
    site = Site(name=raw.get("name", role), url=url, kind=kind, timeout=timeout)
    auth = raw.get("auth") or {}
    if "token_env" in auth:
        site.token = _env(auth["token_env"], f"{role}.auth")
    elif "username_env" in auth:
        if "password_env" not in auth:
            raise ConfigError(f"{role}.auth: username_env needs password_env")
        site.username = _env(auth["username_env"], f"{role}.auth")
        site.password = _env(auth["password_env"], f"{role}.auth")
    return site


def load_config(path: str | Path, timeout: float | None = None) -> Config:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    timeout = float(timeout if timeout is not None else raw.get("timeout", 10))
    repos = raw.get("repositories")
    if not repos or not isinstance(repos, list):
        raise ConfigError("'repositories' must be a non-empty list")

    dns = None
    if raw.get("dns"):
        d = raw["dns"]
        if not d.get("name") or not (d.get("expect_addresses") or d.get("expect_cname")):
            raise ConfigError("dns needs 'name' and 'expect_addresses' and/or 'expect_cname'")
        dns = DnsSpec(d["name"], [str(a) for a in d.get("expect_addresses") or []],
                      d.get("expect_cname"))

    return Config(
        source=_site(raw.get("source"), "source", timeout, path.parent),
        target=_site(raw.get("target"), "target", timeout, path.parent),
        repositories=[str(r) for r in repos],
        compare_digests=bool(raw.get("compare_digests", False)),
        dns=dns,
    )
