"""Strict, secret-safe Nagisa configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """The supplied container configuration is incomplete or unsafe."""


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _origin(environment: Mapping[str, str], name: str) -> str:
    value = _required(environment, name).rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ConfigurationError(f"{name} must be a plain HTTP(S) origin URL")
    return value


def _secret(
    environment: Mapping[str, str],
    direct_name: str,
    file_name: str,
    config_dir: Path,
) -> str:
    direct = environment.get(direct_name, "").strip()
    configured_file = environment.get(file_name, "").strip()
    if direct and configured_file:
        raise ConfigurationError(f"set only one of {direct_name} or {file_name}")
    if direct:
        value = direct
    else:
        if not configured_file:
            raise ConfigurationError(f"{direct_name} or {file_name} is required")
        candidate = Path(configured_file)
        try:
            resolved_config = config_dir.resolve(strict=True)
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(resolved_config)
        except (OSError, ValueError):
            raise ConfigurationError(
                f"{file_name} must be a readable file inside CONFIG_DIR"
            ) from None
        if candidate.is_symlink() or not resolved.is_file() or resolved.stat().st_size > 4096:
            raise ConfigurationError(
                f"{file_name} must be a small regular non-symlink file"
            )
        try:
            value = resolved.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise ConfigurationError(f"{file_name} could not be read") from None
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ConfigurationError(f"{direct_name} must contain one non-empty value")
    return value


def _mode(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "report").strip().lower()
    if value not in {"report", "enabled"}:
        raise ConfigurationError(f"{name} must be report or enabled")
    return value


def _bounded_integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environment.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer") from None
    if value < minimum or value > maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _versions(environment: Mapping[str, str], name: str) -> frozenset[str]:
    values = frozenset(
        value.strip()
        for value in _required(environment, name).split(",")
        if value.strip()
    )
    if not values:
        raise ConfigurationError(f"{name} must not be empty")
    return values


def _positive_ids(environment: Mapping[str, str], name: str) -> tuple[int, ...]:
    rows = [row.strip() for row in _required(environment, name).split(",")]
    try:
        values = tuple(int(row) for row in rows)
    except ValueError:
        raise ConfigurationError(f"{name} must contain positive integers") from None
    if not values or any(value <= 0 for value in values) or len(set(values)) != len(values):
        raise ConfigurationError(f"{name} must contain unique positive integers")
    return values


def _absolute_root(environment: Mapping[str, str], name: str, default: str) -> Path:
    raw = environment.get(name, default).strip() or default
    container_path = PurePosixPath(raw)
    if not container_path.is_absolute() or ".." in container_path.parts:
        raise ConfigurationError(f"{name} must be an absolute contained root")
    return Path(raw)


def _posix_root(environment: Mapping[str, str], name: str, default: str) -> str:
    raw = environment.get(name, default).strip() or default
    root = PurePosixPath(raw)
    if (
        not root.is_absolute() or ".." in root.parts or "\x00" in raw
        or str(root) != raw.rstrip("/") or str(root) == "/"
    ):
        raise ConfigurationError(f"{name} must be one absolute POSIX root")
    return str(root)


def _arrival_config_file(environment,config_dir):
    raw=environment.get('ARRIVAL_CONFIG_FILE','').strip()
    if not raw:return None
    try:
        path=Path(raw);base=config_dir.resolve(strict=True)
        if not path.is_absolute() or '..' in path.parts or path.is_symlink() or path.resolve(strict=True)!=path or not path.is_relative_to(base) or not path.is_file() or path.stat().st_size>16384:
            raise ConfigurationError('ARRIVAL_CONFIG_FILE must be a small canonical file inside CONFIG_DIR')
        return path
    except OSError:
        raise ConfigurationError('ARRIVAL_CONFIG_FILE must be readable inside CONFIG_DIR') from None


@dataclass(frozen=True, slots=True)
class Config:
    shoko_url: str
    shoko_token: str = field(repr=False)
    shoko_allowed_versions: frozenset[str]
    plex_url: str
    plex_token: str = field(repr=False)
    plex_section_ids: tuple[int, ...]
    sonarr_url: str
    sonarr_token: str = field(repr=False)
    sonarr_allowed_versions: frozenset[str]
    normal_sonarr_url: str
    normal_sonarr_token: str = field(repr=False)
    normal_sonarr_allowed_versions: frozenset[str]
    qbit_url: str
    qbit_username: str = field(repr=False)
    qbit_password: str = field(repr=False)
    sonarr_import_root: str = "/Shoko"
    ordering_mode: str = "report"
    reconciler_mode: str = "report"
    future_guard_mode: str = "report"
    ordering_poll_seconds: int = 15
    reconciler_poll_seconds: int = 900
    future_guard_poll_seconds: int = 15
    future_guard_hours: int = 24
    import_root: Path = Path("/Import")
    anime_root: Path = Path("/Anime")
    bind_host: str = "0.0.0.0"
    port: int = 8787
    config_dir: Path = Path("/config")
    request_timeout_seconds: int = 10
    arrival_config_file: Path | None = None
    delivery_config_file: Path | None = None
    seerr_guard_config_file: Path | None = None
    folder_inventory_file: Path | None = None
    delivery_settings_token: str | None = field(default=None, repr=False)
    radarr_url: str | None = None
    radarr_token: str | None = field(default=None, repr=False)

    @classmethod
    def load(cls, environment: Mapping[str, str]) -> "Config":
        config_dir = Path(environment.get("CONFIG_DIR", "/config").strip() or "/config")
        if not config_dir.is_absolute():
            raise ConfigurationError("CONFIG_DIR must be absolute")
        return cls(
            radarr_url=_origin(environment,'RADARR_URL') if environment.get('RADARR_URL') else None,
            radarr_token=_secret(environment,'RADARR_API_TOKEN','RADARR_API_TOKEN_FILE',config_dir) if environment.get('RADARR_URL') else None,
            arrival_config_file=_arrival_config_file(environment,config_dir),
            delivery_config_file=_arrival_config_file({'ARRIVAL_CONFIG_FILE': environment.get('DELIVERY_CONFIG_FILE','')},config_dir),
            seerr_guard_config_file=_arrival_config_file({'ARRIVAL_CONFIG_FILE': environment.get('SEERR_GUARD_CONFIG_FILE','')},config_dir),
            folder_inventory_file=_arrival_config_file({'ARRIVAL_CONFIG_FILE': environment.get('FOLDER_INVENTORY_FILE','')},config_dir),
            delivery_settings_token=_secret(environment, 'DELIVERY_SETTINGS_TOKEN', 'DELIVERY_SETTINGS_TOKEN_FILE', config_dir)
                if environment.get('DELIVERY_SETTINGS_TOKEN') or environment.get('DELIVERY_SETTINGS_TOKEN_FILE') else None,
            shoko_url=_origin(environment, "SHOKO_URL"),
            shoko_token=_secret(
                environment, "SHOKO_API_TOKEN", "SHOKO_API_TOKEN_FILE", config_dir
            ),
            shoko_allowed_versions=_versions(environment, "SHOKO_ALLOWED_VERSIONS"),
            plex_url=_origin(environment, "PLEX_URL"),
            plex_token=_secret(
                environment, "PLEX_TOKEN", "PLEX_TOKEN_FILE", config_dir
            ),
            plex_section_ids=_positive_ids(environment, "PLEX_SECTION_IDS"),
            sonarr_url=_origin(environment, "SONARR_URL"),
            sonarr_token=_secret(
                environment, "SONARR_API_TOKEN", "SONARR_API_TOKEN_FILE", config_dir
            ),
            sonarr_allowed_versions=_versions(environment, "SONARR_ALLOWED_VERSIONS"),
            normal_sonarr_url=_origin(environment, "NORMAL_SONARR_URL"),
            normal_sonarr_token=_secret(
                environment, "NORMAL_SONARR_API_TOKEN", "NORMAL_SONARR_API_TOKEN_FILE", config_dir
            ),
            normal_sonarr_allowed_versions=_versions(
                environment, "NORMAL_SONARR_ALLOWED_VERSIONS"
            ),
            qbit_url=_origin(environment, "QBIT_URL"),
            qbit_username=_secret(
                environment, "QBIT_USERNAME", "QBIT_USERNAME_FILE", config_dir
            ),
            qbit_password=_secret(
                environment, "QBIT_PASSWORD", "QBIT_PASSWORD_FILE", config_dir
            ),
            sonarr_import_root=_posix_root(
                environment, "SONARR_IMPORT_ROOT", "/Shoko"
            ),
            ordering_mode=_mode(environment, "ORDERING_MODE"),
            reconciler_mode=_mode(environment, "RECONCILER_MODE"),
            future_guard_mode=_mode(environment, "FUTURE_GUARD_MODE"),
            ordering_poll_seconds=_bounded_integer(
                environment, "ORDERING_POLL_SECONDS", 15, 15, 3600
            ),
            reconciler_poll_seconds=_bounded_integer(
                environment, "RECONCILER_POLL_SECONDS", 900, 60, 86400
            ),
            future_guard_poll_seconds=_bounded_integer(
                environment, "FUTURE_GUARD_POLL_SECONDS", 15, 15, 3600
            ),
            future_guard_hours=_bounded_integer(
                environment, "FUTURE_GUARD_HOURS", 24, 6, 168
            ),
            import_root=_absolute_root(environment, "IMPORT_ROOT", "/Import"),
            anime_root=_absolute_root(environment, "ANIME_ROOT", "/Anime"),
            bind_host=environment.get("BIND_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_bounded_integer(environment, "PORT", 8787, 1, 65535),
            config_dir=config_dir,
            request_timeout_seconds=_bounded_integer(
                environment, "REQUEST_TIMEOUT_SECONDS", 10, 2, 60
            ),
        )
