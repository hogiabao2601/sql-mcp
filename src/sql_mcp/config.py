"""Connection profiles: one named connection per project / environment.

Profiles come from a JSON file (SQL_MCP_CONFIG, or MYSQL_CONFIG for compatibility) or, for a single
MySQL connection, from MYSQL_HOST/PORT/USER/PASSWORD/DATABASE. Loaded lazily, so a config mistake
becomes a readable tool error instead of a server that dies at startup.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_MASK = ("password", "passwd", "secret", "token", "api_key", "apikey", "ssn")
ENGINES = {"mysql": 3306, "mariadb": 3306, "postgres": 5432, "sqlite": None, "starrocks": 9030}
KNOWN_KEYS = {"engine", "host", "port", "user", "password", "password_env", "password_cmd", "database", "schema",
              "path", "databases", "catalog", "timeout_ms",
              "pool_size", "mask_columns", "tls", "tls_ca", "ssl_ca", "ssl_check_hostname"}
# disable: plaintext. prefer: TLS if offered, unverified, may fall back to plaintext. require: TLS or fail,
# unverified. verify-ca: TLS + certificate chain checked against tls_ca. verify-full: verify-ca + host name.
TLS_MODES = ("disable", "prefer", "require", "verify-ca", "verify-full")
TIMEOUT_MS_RANGE = (100, 600_000)       # 0 would mean "no limit" on MySQL (REVIEW #9), so it is refused
POOL_SIZE_RANGE = (1, 20)
PASSWORD_CMD_TIMEOUT_S = 10


@dataclass(frozen=True)
class Profile:
    name: str
    engine: str
    host: str
    port: int | None
    user: str
    password: str
    database: str | None          # default database (MySQL family) / connection database (PostgreSQL)
    schema: str | None            # PostgreSQL: default schema (tools' `database` argument means schema there)
    catalog: str | None           # StarRocks: the external/internal catalog this profile reads
    path: str | None              # SQLite: a single file (exposed as database 'main')
    databases: tuple[tuple[str, str], ...]   # SQLite: (name, file) pairs; each name is a database in the tools
    timeout_ms: int               # enforced by the engine for every statement it can cover
    pool_size: int
    mask: tuple[str, ...]         # column-name fragments whose values are hidden
    tls: str                      # one of TLS_MODES
    tls_ca: str | None            # CA file for verify-ca / verify-full


def _int_in(name: str, key: str, value, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"Profile '{name}': '{key}' must be an integer, got {value!r}.") from None
    if not lo <= v <= hi:
        raise ToolError(f"Profile '{name}': '{key}' must be between {lo} and {hi}, got {v}.")
    return v


def _tls(name: str, raw: dict) -> tuple[str, str | None]:
    if "tls" in raw or "tls_ca" in raw:
        if "ssl_ca" in raw or "ssl_check_hostname" in raw:
            raise ToolError(f"Profile '{name}': use either tls/tls_ca or the older ssl_ca/ssl_check_hostname, "
                            "not both.")
        mode, ca = raw.get("tls", "verify-full" if raw.get("tls_ca") else "prefer"), raw.get("tls_ca") or None
    elif raw.get("ssl_ca"):          # mysql_server.py v2 keys
        mode, ca = ("verify-full" if raw.get("ssl_check_hostname", True) else "verify-ca"), raw["ssl_ca"]
    else:
        mode, ca = "prefer", None
    if mode not in TLS_MODES:
        raise ToolError(f"Profile '{name}': tls must be one of {', '.join(TLS_MODES)}, got '{mode}'.")
    if mode.startswith("verify") and not ca:
        raise ToolError(f"Profile '{name}': tls '{mode}' needs 'tls_ca' (the CA file to verify against).")
    return mode, ca


def _password(name: str, raw: dict) -> str:
    """password_cmd (argv list, no shell; e.g. a keychain lookup), else password_env, else a literal password."""
    sources = [k for k in ("password", "password_env", "password_cmd") if k in raw]
    if len(sources) > 1:
        raise ToolError(f"Profile '{name}': give only one of password, password_env, password_cmd.")
    cmd = raw.get("password_cmd")
    if cmd is not None:
        if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
            raise ToolError(f"Profile '{name}': password_cmd must be a list of strings (argv, run without a shell), "
                            'e.g. ["security", "find-generic-password", "-s", "my-db", "-w"].')
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=PASSWORD_CMD_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise ToolError(f"Profile '{name}': password_cmd did not finish within {PASSWORD_CMD_TIMEOUT_S} s."
                            ) from None
        except OSError as e:
            raise ToolError(f"Profile '{name}': password_cmd could not start: {e.strerror}.") from None
        if r.returncode != 0:               # report stderr (diagnostics), never stdout (it may hold the secret)
            err = (r.stderr.strip().splitlines() or [""])[0][:200]
            raise ToolError(f"Profile '{name}': password_cmd failed with exit code {r.returncode}: {err}")
        return r.stdout[:-1] if r.stdout.endswith("\n") else r.stdout
    env_name = raw.get("password_env")
    if env_name:
        if env_name not in os.environ:
            raise ToolError(f"Profile '{name}': environment variable {env_name} is not set.")
        return os.environ[env_name]
    return raw.get("password", "")


def _profile(name: str, raw: dict) -> Profile:
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        raise ToolError(f"Profile '{name}': unknown key(s) {', '.join(sorted(unknown))}.")
    engine = raw.get("engine", "mysql")
    if engine not in ENGINES:
        raise ToolError(f"Profile '{name}': engine must be one of {', '.join(ENGINES)}, got '{engine}'.")
    password = _password(name, raw)
    mask = raw.get("mask_columns", DEFAULT_MASK)
    if isinstance(mask, str):
        mask = mask.split(",")
    default_port = ENGINES[engine]
    tls, tls_ca = _tls(name, raw)
    if raw.get("schema") and engine != "postgres":
        raise ToolError(f"Profile '{name}': 'schema' is for PostgreSQL; for {engine} use 'database'.")
    if raw.get("catalog") and engine != "starrocks":
        raise ToolError(f"Profile '{name}': 'catalog' is for StarRocks only.")
    databases: tuple[tuple[str, str], ...] = ()
    if engine == "sqlite":
        if bool(raw.get("path")) == bool(raw.get("databases")):
            raise ToolError(f"Profile '{name}': give either 'path' (one SQLite file) or 'databases' ({{name: file}}).")
        files = {"main": raw["path"]} if raw.get("path") else raw["databases"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 9 or not all(
                isinstance(k, str) and k and isinstance(v, str) and v for k, v in files.items()):
            raise ToolError(f"Profile '{name}': 'databases' must map 1 to 9 names to SQLite file paths.")
        if raw.get("databases") and ({"main", "temp"} & {k.lower() for k in files}):
            raise ToolError(f"Profile '{name}': 'main' and 'temp' are reserved SQLite names; use 'path' for a "
                            "single file, or pick other names.")
        databases = tuple(files.items())
        if raw.get("database") and raw["database"] not in files:
            raise ToolError(f"Profile '{name}': database '{raw['database']}' is not one of {', '.join(files)}.")
    elif not raw.get("user"):
        raise ToolError(f"Profile '{name}': 'user' is required.")
    return Profile(
        name=name,
        engine=engine,
        host=raw.get("host", "127.0.0.1"),
        port=None if default_port is None else int(raw.get("port", default_port)),
        user=raw.get("user", ""),
        password=password,
        database=raw.get("database") or (databases[0][0] if databases else None),
        schema=raw.get("schema") or None,
        catalog=raw.get("catalog") or None,
        path=raw.get("path") or None,
        databases=databases,
        timeout_ms=_int_in(name, "timeout_ms", raw.get("timeout_ms", 5000), *TIMEOUT_MS_RANGE),
        pool_size=_int_in(name, "pool_size", raw.get("pool_size", 3), *POOL_SIZE_RANGE),
        mask=tuple(m.strip().lower() for m in mask if m.strip()),
        tls=tls,
        tls_ca=tls_ca,
    )


_reload_hooks: list = []


def on_reload(fn) -> None:
    """Run fn() after the profile file changed and was reloaded (the executor closes its pools)."""
    _reload_hooks.append(fn)


class _Config:
    """config(): the profiles, reloaded when the profile file changes (REVIEW #14: no restart needed).
    config.cache_clear() forces a reload, as with the lru_cache it replaces."""
    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._value = None

    def cache_clear(self) -> None:
        with self._lock:
            self._key = None

    def __call__(self) -> tuple[dict[str, Profile], str]:
        path = os.environ.get("SQL_MCP_CONFIG") or os.environ.get("MYSQL_CONFIG")
        try:
            stamp = os.stat(path).st_mtime_ns if path else None
        except OSError:
            stamp = None
        key = (path, stamp)
        with self._lock:
            if self._key == key and self._value is not None:
                return self._value
            changed = self._key is not None and self._key[0] == path
        value = _load()                      # raises ToolError on a bad file; the old value stays cached
        with self._lock:
            self._key, self._value = key, value
        if changed:
            for fn in _reload_hooks:
                fn()
        return value


def _load() -> tuple[dict[str, Profile], str]:
    path = os.environ.get("SQL_MCP_CONFIG") or os.environ.get("MYSQL_CONFIG")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ToolError(f"Cannot read config file '{path}': {exc}") from exc
        profiles = {n: _profile(n, r) for n, r in raw.get("profiles", {}).items()}
        if not profiles:
            raise ToolError(f"Config file '{path}' has no profiles.")
        default = raw.get("default") or next(iter(profiles))
        if default not in profiles:
            raise ToolError(f"Config file default profile '{default}' does not exist.")
        return profiles, default
    # Single-profile mode from plain env vars (same as mysql_server.py v1/v2)
    if not os.environ.get("MYSQL_USER"):
        raise ToolError("Set SQL_MCP_CONFIG (profiles file) or MYSQL_USER/MYSQL_PASSWORD/MYSQL_HOST/...")
    single = {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port": os.environ.get("MYSQL_PORT", 3306),
        "user": os.environ["MYSQL_USER"],
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DATABASE"),
        "mask_columns": os.environ.get("MYSQL_MASK_COLUMNS", ",".join(DEFAULT_MASK)),
    }
    return {"default": _profile("default", single)}, "default"


config = _Config()


def get_profile(name: str | None) -> Profile:
    profiles, default = config()
    key = name or default
    if key not in profiles:
        raise ToolError(f"Unknown profile '{key}'. Available: {', '.join(profiles)}")
    return profiles[key]
