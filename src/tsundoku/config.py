#!/usr/bin/env python3
"""Configuration management for public tsundoku releases."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import sys
from typing import Optional


APP_NAME = "tsundoku"
CONFIG_FILENAME = "config.json"
CONFIG_VERSION = 1


def default_config_dir() -> Path:
    """Return an OS-appropriate configuration directory."""
    override = os.environ.get("TSUNDOKU_CONFIG_HOME")
    if override:
        return Path(override).expanduser() / APP_NAME
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_NAME


def default_data_dir() -> Path:
    """Return an OS-appropriate data directory."""
    override = os.environ.get("TSUNDOKU_DATA_HOME")
    if override:
        return Path(override).expanduser() / APP_NAME
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or (Path.home() / "AppData" / "Local"))
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / APP_NAME


def default_config_path() -> Path:
    """Return the default config file path."""
    return default_config_dir() / CONFIG_FILENAME


@dataclass
class AuthConfig:
    """Authentication strategy for an HTTP backend."""

    type: str = "none"
    env_var: str = ""
    header_name: str = "Authorization"
    header_prefix: str = "Bearer "
    body_field: str = "password"
    query_field: str = "token"


@dataclass
class BackendProfile:
    """A named backend and system profile."""

    name: str = "default"
    system_name: str = "My System"
    system_description: str = ""
    integration_goal: str = ""
    base_url: str = ""
    health_path: str = "/health"
    message_path: str = "/api/agents/{agent}/message/send"
    create_task_path: str = "/api/tasks"
    message_field: str = "message"
    timeout_field: str = "timeout"
    agent_field: str = "agent"
    task_text_field: str = "text"
    task_priority_field: str = "priority"
    task_notes_field: str = "notes"
    response_text_paths: list[str] = field(default_factory=lambda: ["response", "message", "data.response"])
    response_model_paths: list[str] = field(default_factory=lambda: ["model", "data.model"])
    task_id_paths: list[str] = field(default_factory=lambda: ["id", "task.id", "data.id", "task_id"])
    agents: dict[str, str] = field(
        default_factory=lambda: {
            "analysis": "assistant",
            "meta": "assistant",
            "task": "assistant",
        }
    )
    auth: AuthConfig = field(default_factory=AuthConfig)


@dataclass
class AppConfig:
    """Top-level tsundoku configuration."""

    version: int = CONFIG_VERSION
    data_dir: str = ""
    active_profile: str = "default"
    profiles: dict[str, BackendProfile] = field(
        default_factory=lambda: {"default": BackendProfile()}
    )
    fetch_mode: str = "auto"


_cached_config: Optional[AppConfig] = None


def _profile_from_dict(name: str, data: dict) -> BackendProfile:
    auth_data = data.get("auth", {}) if isinstance(data, dict) else {}
    merged = dict(data or {})
    merged["name"] = name
    merged["auth"] = AuthConfig(**{k: v for k, v in auth_data.items() if k in AuthConfig.__dataclass_fields__})
    return BackendProfile(**{k: v for k, v in merged.items() if k in BackendProfile.__dataclass_fields__})


def create_default_config() -> AppConfig:
    """Build a default neutral configuration."""
    return AppConfig()


def config_to_dict(config: AppConfig) -> dict:
    """Serialize config to plain dict form."""
    payload = asdict(config)
    payload["profiles"] = {
        name: asdict(profile)
        for name, profile in config.profiles.items()
    }
    return payload


def load_config(path: Optional[Path] = None) -> AppConfig:
    """Load config from disk or return defaults when missing."""
    config_path = path or default_config_path()
    if not config_path.exists():
        return create_default_config()

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    profiles_raw = raw.get("profiles", {})
    profiles = {
        name: _profile_from_dict(name, profile_data)
        for name, profile_data in profiles_raw.items()
    } or {"default": BackendProfile()}

    return AppConfig(
        version=int(raw.get("version", CONFIG_VERSION)),
        data_dir=str(raw.get("data_dir", "")),
        active_profile=str(raw.get("active_profile", "default")),
        profiles=profiles,
        fetch_mode=str(raw.get("fetch_mode", "auto") or "auto"),
    )


def save_config(config: AppConfig, path: Optional[Path] = None) -> Path:
    """Persist config to disk."""
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config_to_dict(config), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    global _cached_config
    _cached_config = config
    return config_path


def get_config(path: Optional[Path] = None, refresh: bool = False) -> AppConfig:
    """Return cached config unless refresh is requested."""
    global _cached_config
    if refresh or _cached_config is None:
        _cached_config = load_config(path)
    return _cached_config


def reset_cache() -> None:
    """Clear cached configuration."""
    global _cached_config
    _cached_config = None


def get_active_profile(config: Optional[AppConfig] = None) -> BackendProfile:
    """Return the active backend profile."""
    cfg = config or get_config()
    profile = cfg.profiles.get(cfg.active_profile)
    if profile is not None:
        return profile
    first_name, first_profile = next(iter(cfg.profiles.items()))
    cfg.active_profile = first_name
    return first_profile


def get_data_dir(config: Optional[AppConfig] = None) -> Path:
    """Resolve the configured data directory."""
    cfg = config or get_config()
    if cfg.data_dir:
        return Path(cfg.data_dir).expanduser()
    return default_data_dir()


__all__ = [
    "APP_NAME",
    "CONFIG_FILENAME",
    "CONFIG_VERSION",
    "AuthConfig",
    "BackendProfile",
    "AppConfig",
    "default_config_dir",
    "default_config_path",
    "default_data_dir",
    "create_default_config",
    "config_to_dict",
    "load_config",
    "save_config",
    "get_config",
    "reset_cache",
    "get_active_profile",
    "get_data_dir",
]
