#!/usr/bin/env python3
"""Persistent local storage for tsundoku."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Optional

from . import config as app_config
from .models import LinkStatus


DEFAULT_LINKS_FILE = "links.json"
DEFAULT_ANALYSES_DIR = "analyses"
DEFAULT_INTEGBUF_FILE = "integrations_buffer.json"
DEFAULT_INTLOG_FILE = "integration_log.json"
DEFAULT_PREFS_FILE = "prefs.json"
DEFAULT_PREFS = {
    "sort_mode": "date-desc",
}


def get_logger(name: str = "tsundoku.storage") -> logging.Logger:
    """Get or create a module logger."""
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


class StorageManager:
    """Encapsulate all file IO and validation for the local store."""

    def __init__(
        self,
        store_dir: Optional[Path] = None,
        *,
        links_file: str = DEFAULT_LINKS_FILE,
        analyses_dir: str = DEFAULT_ANALYSES_DIR,
        integ_buf_file: str = DEFAULT_INTEGBUF_FILE,
        int_log_file: str = DEFAULT_INTLOG_FILE,
        prefs_file: str = DEFAULT_PREFS_FILE,
    ):
        self.store_dir = (store_dir or app_config.get_data_dir()).expanduser()
        self.links_file = self.store_dir / links_file
        self.analyses_dir = self.store_dir / analyses_dir
        self.integ_buf_file = self.store_dir / integ_buf_file
        self.integration_log_file = self.store_dir / int_log_file
        self.prefs_file = self.store_dir / prefs_file
        self.logger = get_logger()
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.analyses_dir.mkdir(parents=True, exist_ok=True)

    def _create_backup(self, file_path: Path) -> Optional[Path]:
        if not file_path.exists():
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = file_path.parent / f"{file_path.stem}.{timestamp}.bak"
        try:
            shutil.copy2(file_path, backup)
            backups = sorted(
                file_path.parent.glob(f"{file_path.stem}.*.bak"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for old in backups[5:]:
                old.unlink(missing_ok=True)
            return backup
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning("Failed to create backup for %s: %s", file_path, exc)
            return None

    def _atomic_write(self, file_path: Path, text: str) -> bool:
        self._create_backup(file_path)
        fd, temp_path = tempfile.mkstemp(dir=file_path.parent, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temp_path, file_path)
            return True
        except Exception as exc:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            self.logger.error("Failed to write %s: %s", file_path, exc)
            return False

    def _validate_link(self, link: dict) -> tuple[bool, str]:
        required = ["id", "url", "added_at", "status"]
        for field in required:
            if field not in link:
                return False, f"Missing required field: {field}"
        url = str(link.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return False, f"Invalid URL: {url}"
        if not LinkStatus.is_valid(link.get("status", "")):
            return False, f"Invalid status: {link.get('status')}"
        return True, ""

    def load_links(self) -> list[dict]:
        if not self.links_file.exists():
            return []
        try:
            payload = json.loads(self.links_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.error("Failed to load links: %s", exc)
            return []

        links = payload.get("links", []) if isinstance(payload, dict) else []
        valid: list[dict] = []
        for link in links:
            ok, error = self._validate_link(link)
            if ok:
                valid.append(link)
            else:
                self.logger.warning("Skipping invalid link: %s", error)
        return valid

    def save_links(self, links: list[dict]) -> bool:
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "links": links,
        }
        return self._atomic_write(
            self.links_file,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )

    def get_link(self, link_id: str) -> Optional[dict]:
        return next((item for item in self.load_links() if item.get("id") == link_id), None)

    def update_link(self, link_id: str, **updates) -> bool:
        links = self.load_links()
        changed = False
        for item in links:
            if item.get("id") == link_id:
                item.update(updates)
                changed = True
                break
        return self.save_links(links) if changed else False

    def update_links(self, link_ids: list[str], **updates) -> int:
        links = self.load_links()
        changed = 0
        wanted = set(link_ids)
        for item in links:
            if item.get("id") in wanted:
                item.update(updates)
                changed += 1
        if changed:
            self.save_links(links)
        return changed

    def delete_link(self, link_id: str) -> bool:
        links = self.load_links()
        kept = [item for item in links if item.get("id") != link_id]
        if len(kept) == len(links):
            return False
        if not self.save_links(kept):
            return False
        (self.analyses_dir / f"{link_id}.json").unlink(missing_ok=True)
        return True

    def load_integration_log(self) -> list[dict]:
        if not self.integration_log_file.exists():
            return []
        try:
            data = json.loads(self.integration_log_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("Failed to load integration log: %s", exc)
            return []
        return data if isinstance(data, list) else data.get("entries", [])

    def append_integration_log(self, entry: dict) -> bool:
        entries = self.load_integration_log()
        entries.append(entry)
        return self._atomic_write(
            self.integration_log_file,
            json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
        )

    def load_integ_buffer(self) -> list[dict]:
        if not self.integ_buf_file.exists():
            return []
        try:
            payload = json.loads(self.integ_buf_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("Failed to load integration buffer: %s", exc)
            return []
        return payload.get("integrations", []) if isinstance(payload, dict) else []

    def save_integ_buffer(self, entries: list[dict]) -> bool:
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "integrations": entries,
        }
        return self._atomic_write(
            self.integ_buf_file,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )

    def load_prefs(self) -> dict:
        prefs = dict(DEFAULT_PREFS)
        if not self.prefs_file.exists():
            return prefs
        try:
            data = json.loads(self.prefs_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("Failed to load prefs: %s", exc)
            return prefs
        loaded = data.get("prefs", data) if isinstance(data, dict) else {}
        if isinstance(loaded, dict):
            prefs.update(loaded)
        return prefs

    def save_prefs(self, prefs: dict) -> bool:
        merged = dict(DEFAULT_PREFS)
        merged.update(prefs or {})
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "prefs": merged,
        }
        return self._atomic_write(
            self.prefs_file,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )

    def update_prefs(self, **updates) -> dict:
        prefs = self.load_prefs()
        prefs.update(updates)
        self.save_prefs(prefs)
        return prefs

    def save_analysis_file(self, link: dict) -> Optional[Path]:
        path = self.analyses_dir / f"{link['id']}.json"
        analysis = link.get("analysis", {})
        if isinstance(analysis, str):
            try:
                analysis = json.loads(analysis)
            except json.JSONDecodeError:
                analysis = {"raw": link.get("analysis")}

        payload = {
            "id": link.get("id"),
            "url": link.get("url"),
            "title": link.get("title"),
            "summary": link.get("summary"),
            "source_type": link.get("source_type"),
            "relevance_score": (analysis or {}).get("relevance_score"),
            "technologies": (analysis or {}).get("technologies", link.get("tags", [])),
            "integration_ideas": (analysis or {}).get("integration_ideas", []),
            "thinking_trace": link.get("thinking_trace"),
            "agent_used": link.get("agent_used"),
            "analyzed_by_model": link.get("analyzed_by_model"),
            "analyzed_at": link.get("analyzed_at"),
            "fetch_mode": link.get("fetch_mode"),
            "fetch_chars": link.get("fetch_chars"),
            "fetch_error": link.get("fetch_error"),
            "is_thread": link.get("is_thread", False),
            "thread_urls": link.get("thread_urls", []),
            "thread_author": link.get("thread_author"),
            "thread_post_count": link.get("thread_post_count", 0),
            "raw_analysis": link.get("analysis"),
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if self._atomic_write(path, text):
            self._update_analysis_index()
            return path
        return None

    def _update_analysis_index(self) -> None:
        scored: list[tuple[int, dict]] = []
        for link in self.load_links():
            if link.get("status") not in (
                LinkStatus.ANALYZED.value,
                LinkStatus.TRIAL.value,
                LinkStatus.IMPLEMENTED.value,
                LinkStatus.DONE.value,
            ):
                continue
            analysis = link.get("analysis", {})
            if isinstance(analysis, str):
                try:
                    analysis = json.loads(analysis)
                except json.JSONDecodeError:
                    analysis = {}
            try:
                score = int((analysis or {}).get("relevance_score", 0) or 0)
            except Exception:
                score = 0
            scored.append((score, link))

        scored.sort(key=lambda item: item[0], reverse=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total": len(scored),
            "note": "Top analyzed links by relevance.",
            "top": [
                {
                    "id": link["id"],
                    "url": link["url"],
                    "title": link.get("title"),
                    "relevance_score": score,
                    "analyzed_at": link.get("analyzed_at"),
                    "analysis_file": str(self.analyses_dir / f"{link['id']}.json"),
                }
                for score, link in scored[:10]
            ],
        }
        (self.analyses_dir / "index.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def get_stats(self) -> dict:
        links = self.load_links()
        by_status: dict[str, int] = {}
        for link in links:
            status = str(link.get("status", "unknown"))
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "total_links": len(links),
            "by_status": by_status,
            "storage_dir": str(self.store_dir),
            "links_file": str(self.links_file),
            "analyses_dir": str(self.analyses_dir),
            "integ_buffer": str(self.integ_buf_file),
            "integration_log": str(self.integration_log_file),
            "prefs_file": str(self.prefs_file),
        }


_default_storage: Optional[StorageManager] = None
_default_storage_dir: Optional[Path] = None


def get_storage(store_dir: Optional[Path] = None, *, refresh: bool = False) -> StorageManager:
    """Return a cached storage manager for the active data directory."""
    global _default_storage, _default_storage_dir
    target = (store_dir or app_config.get_data_dir()).expanduser()
    if refresh or _default_storage is None or _default_storage_dir != target:
        _default_storage = StorageManager(store_dir=target)
        _default_storage_dir = target
    return _default_storage


def reset_cache() -> None:
    """Clear the cached storage manager."""
    global _default_storage, _default_storage_dir
    _default_storage = None
    _default_storage_dir = None


def load() -> list[dict]:
    """Backward-compatible convenience alias."""
    return get_storage().load_links()


def save(links: list[dict]) -> bool:
    """Backward-compatible convenience alias."""
    return get_storage().save_links(links)


def patch(link_id: str, **kwargs) -> bool:
    """Backward-compatible convenience alias."""
    return get_storage().update_link(link_id, **kwargs)


__all__ = [
    "StorageManager",
    "get_storage",
    "reset_cache",
    "load",
    "save",
    "patch",
]
