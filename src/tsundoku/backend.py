#!/usr/bin/env python3
"""Generic HTTP JSON backend integration for tsundoku."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import config as app_config


MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 20.0
REQUEST_TIMEOUT = 180


def get_logger(name: str = "tsundoku.backend") -> logging.Logger:
    """Get or create a logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
    return logger


logger = get_logger()


class ConfigurationError(RuntimeError):
    """Raised when the active backend profile is incomplete."""


def _extract_path(data: Any, path: str) -> Any:
    """Extract a dotted path from a nested dict/list structure."""
    current = data
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, list):
            try:
                current = current[int(segment)]
                continue
            except (ValueError, IndexError):
                return None
        return None
    return current


def _first_path(data: Any, paths: list[str]) -> Any:
    """Return the first non-empty value found across candidate paths."""
    for path in paths:
        value = _extract_path(data, path)
        if value not in (None, ""):
            return value
    return None


def _format_path(path_template: str, *, agent: str = "") -> str:
    """Format a backend path template safely."""
    try:
        return path_template.format(agent=agent)
    except KeyError:
        return path_template


@dataclass
class ResponseEnvelope:
    """Normalized backend response."""

    raw: dict[str, Any]
    text: str = ""
    model: str = ""
    task_id: str = ""


class BackendClient:
    """HTTP client for configurable JSON backends."""

    def __init__(
        self,
        profile: app_config.BackendProfile,
        timeout: int = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ):
        self.profile = profile
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def base_url(self) -> str:
        """Return the normalized base URL for the profile."""
        return str(self.profile.base_url or "").rstrip("/")

    def configured(self) -> bool:
        """True when the profile points to an HTTP backend."""
        return bool(self.base_url)

    def _resolve_secret(self) -> str:
        env_var = self.profile.auth.env_var.strip()
        if not env_var:
            return ""
        return str(os.environ.get(env_var, ""))

    def _prepare_auth(
        self,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        path: str,
    ) -> tuple[dict[str, str], dict[str, Any], str]:
        auth = self.profile.auth
        secret = self._resolve_secret()
        if auth.type == "none" or not secret:
            return headers, body, path
        if auth.type == "bearer":
            headers[auth.header_name or "Authorization"] = f"{auth.header_prefix}{secret}"
            return headers, body, path
        if auth.type == "header":
            headers[auth.header_name or "X-API-Key"] = f"{auth.header_prefix}{secret}"
            return headers, body, path
        if auth.type == "body":
            body[auth.body_field or "password"] = secret
            return headers, body, path
        if auth.type == "query":
            joiner = "&" if "?" in path else "?"
            path = f"{path}{joiner}{urlencode({auth.query_field or 'token': secret})}"
            return headers, body, path
        return headers, body, path

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict[str, Any]] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_on_timeout: bool = True,
    ) -> dict[str, Any]:
        if not self.configured():
            raise ConfigurationError("The active backend profile does not define a base URL.")

        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        headers = {"Content-Type": "application/json"}
        payload = dict(body or {})
        headers, payload, path = self._prepare_auth(headers=headers, body=payload, path=path)
        data = json.dumps(payload).encode("utf-8") if payload else None
        url = f"{self.base_url}/{path.lstrip('/')}"

        last_error = ""
        backoff = INITIAL_BACKOFF
        attempts_made = 0
        for attempt in range(retries + 1):
            attempts_made = attempt + 1
            try:
                request = Request(url, data=data, headers=headers, method=method.upper())
                with urlopen(request, timeout=timeout or self.timeout) as response:
                    response_text = response.read().decode("utf-8")
                if not response_text:
                    return {"ok": True}
                try:
                    return json.loads(response_text)
                except json.JSONDecodeError:
                    return {"response": response_text}
            except HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = f"HTTP {exc.code}: {error_body}"
                if 400 <= exc.code < 500:
                    break
            except URLError as exc:
                last_error = f"URL error: {exc.reason}"
                if not retry_on_timeout and "timed out" in str(exc.reason).lower():
                    break
            except TimeoutError:
                last_error = "Request timeout"
                if not retry_on_timeout:
                    break
            except Exception as exc:  # pragma: no cover - defensive
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < retries:
                logger.warning("Backend request failed: %s", last_error)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

        label = "attempt" if attempts_made == 1 else "attempts"
        return {"error": f"Request failed after {attempts_made} {label}: {last_error or 'unknown error'}"}

    def check_health(self) -> bool:
        """Check whether the configured backend appears reachable."""
        if not self.configured():
            return False
        path = self.profile.health_path.strip()
        if not path:
            return True
        result = self._request("GET", path, timeout=5, max_retries=0)
        return "error" not in result

    def send_message(
        self,
        agent: str,
        message: str,
        *,
        timeout: int = REQUEST_TIMEOUT,
        max_retries: Optional[int] = None,
        retry_on_timeout: bool = True,
    ) -> dict[str, Any]:
        """Send an analysis or meta prompt to the backend."""
        body: dict[str, Any] = {
            self.profile.message_field: message,
            self.profile.timeout_field: timeout,
        }
        path = _format_path(self.profile.message_path, agent=agent)
        if "{agent}" not in self.profile.message_path and self.profile.agent_field:
            body[self.profile.agent_field] = agent
        return self._request(
            "POST",
            path,
            body=body,
            timeout=timeout,
            max_retries=max_retries,
            retry_on_timeout=retry_on_timeout,
        )

    def create_task(
        self,
        text: str,
        *,
        agent: str,
        priority: str = "med",
        notes: str = "",
    ) -> dict[str, Any]:
        """Create a task or equivalent backlog item on the backend."""
        path = self.profile.create_task_path.strip()
        if not path:
            return {"error": "The active backend profile does not define a task creation path."}
        body = {
            self.profile.task_text_field: text,
            self.profile.task_priority_field: priority,
            self.profile.task_notes_field: notes,
        }
        if self.profile.agent_field:
            body[self.profile.agent_field] = agent
        return self._request("POST", path, body=body)

    def normalize_message_response(self, raw: dict[str, Any]) -> ResponseEnvelope:
        """Return a normalized envelope for message responses."""
        text = _first_path(raw, self.profile.response_text_paths) or ""
        model = _first_path(raw, self.profile.response_model_paths) or ""
        task_id = _first_path(raw, self.profile.task_id_paths) or ""
        return ResponseEnvelope(raw=raw, text=str(text), model=str(model), task_id=str(task_id))


_default_client: Optional[BackendClient] = None


def get_client(refresh: bool = False) -> BackendClient:
    """Return the active backend client."""
    global _default_client
    if refresh or _default_client is None:
        _default_client = BackendClient(app_config.get_active_profile())
    return _default_client


def reset_cache() -> None:
    """Clear the cached backend client."""
    global _default_client
    _default_client = None


__all__ = [
    "ConfigurationError",
    "ResponseEnvelope",
    "BackendClient",
    "get_client",
    "reset_cache",
]
