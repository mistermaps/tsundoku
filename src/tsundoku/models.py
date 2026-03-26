#!/usr/bin/env python3
"""Data models for tsundoku."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any
import uuid


# ============================================================================
# ENUMS
# ============================================================================

class LinkStatus(Enum):
    """Valid states for a link in the system."""
    UNREAD = "unread"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    TRIAL = "trial"
    IMPLEMENTED = "implemented"
    DONE = "done"
    ARCHIVED = "archived"
    FETCH_ERROR = "fetch_error"

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        return status in (cls.DONE.value, cls.TRIAL.value,
                          cls.IMPLEMENTED.value, cls.ARCHIVED.value)

    @classmethod
    def is_active(cls, status: str) -> bool:
        """True for statuses shown in default views (not archived)."""
        return status != cls.ARCHIVED.value

    @classmethod
    def is_valid(cls, status: str) -> bool:
        return status in [s.value for s in cls]


class SourceType(Enum):
    TWITTER = "twitter"
    GITHUB = "github"
    ARXIV = "arxiv"
    YOUTUBE = "youtube"
    REDDIT = "reddit"
    HUGGINGFACE = "huggingface"
    BLUESKY = "bluesky"
    WEB = "web"


class IntegrationStatus(Enum):
    STAGED = "staged"
    PROMOTED = "promoted"
    DISCARDED = "discarded"


class IdeaType(Enum):
    TASK = "task"
    SKILL = "skill"
    CHAT = "chat"
    CUSTOM = "custom"


class SortMode(Enum):
    RELEVANCE_DESC = "relevance-desc"
    RELEVANCE_ASC = "relevance-asc"
    DATE_DESC = "date-desc"
    DATE_ASC = "date-asc"
    STATUS = "status"


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class Analysis:
    """Structured analysis result from an agent."""
    title: str = ""
    summary: str = ""
    technologies: list[str] = field(default_factory=list)
    relevance_score: int = 0
    integration_ideas: list[str] = field(default_factory=list)
    relevance_notes: str = ""
    skill_ideas: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> Optional[Analysis]:
        try:
            data = json.loads(json_str)
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def is_valid(self) -> bool:
        return bool(self.title or self.summary)


@dataclass
class FetchMetadata:
    fetch_chars: Optional[int] = None
    fetch_mode: Optional[str] = None
    fetch_elapsed: Optional[float] = None
    fetch_error: Optional[str] = None


@dataclass
class Link:
    """A bookmarked URL with analysis data."""
    id: str = ""
    url: str = ""
    added_at: str = ""
    status: str = LinkStatus.UNREAD.value
    title: Optional[str] = None
    summary: Optional[str] = None
    analysis_json: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    source_type: str = SourceType.WEB.value
    notes: str = ""
    tasks_created: list[str] = field(default_factory=list)
    agent_used: Optional[str] = None
    analyzed_at: Optional[str] = None
    thinking_trace: Optional[str] = None
    fetch_chars: Optional[int] = None
    fetch_mode: Optional[str] = None
    fetch_elapsed: Optional[float] = None
    fetch_error: Optional[str] = None
    analyzed_by_model: Optional[str] = None
    # Thread support — single Link can represent a thread
    is_thread: bool = False
    thread_urls: list[str] = field(default_factory=list)
    thread_author: Optional[str] = None
    thread_post_count: int = 0
    # Archival
    archived_at: Optional[str] = None
    archived_reason: Optional[str] = None
    # Skillbuild tracking
    skill_name: Optional[str] = None
    skill_path: Optional[str] = None

    def __post_init__(self):
        if not self.id:
            self.id = f"lnk-{uuid.uuid4().hex[:8]}"
        if not self.added_at:
            self.added_at = datetime.now(timezone.utc).isoformat()

    @property
    def analysis(self) -> Optional[Analysis]:
        if not self.analysis_json:
            return None
        return Analysis.from_json(self.analysis_json)

    @analysis.setter
    def analysis(self, value: Analysis):
        self.analysis_json = value.to_json() if value else None

    @property
    def is_terminal(self) -> bool:
        return LinkStatus.is_terminal(self.status)

    @property
    def is_analyzed(self) -> bool:
        return bool(self.analysis_json) and self.status in (
            LinkStatus.ANALYZED.value,
            LinkStatus.TRIAL.value,
            LinkStatus.IMPLEMENTED.value,
            LinkStatus.DONE.value,
        )

    @property
    def relevance_score(self) -> int:
        if a := self.analysis:
            return a.relevance_score
        return 0

    @property
    def display_url(self) -> str:
        """URL for display — shows thread indicator if applicable."""
        if self.is_thread:
            return f"{self.url} [thread: {self.thread_post_count} posts]"
        return self.url

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Link:
        if "analysis" in data and "analysis_json" not in data:
            data["analysis_json"] = data.pop("analysis")
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class IntegrationBufferEntry:
    """A staged integration for agent execution."""
    id: str = ""
    link_id: str = ""
    link_url: str = ""
    link_title: str = ""
    idea: str = ""
    idea_type: str = IdeaType.TASK.value
    agent: str = "assistant"
    staged_at: str = ""
    status: str = IntegrationStatus.STAGED.value
    promoted_at: Optional[str] = None
    task_id: Optional[str] = None
    notes: str = ""
    skill_name: Optional[str] = None
    skill_path: Optional[str] = None

    def __post_init__(self):
        if not self.id:
            self.id = f"integ-{uuid.uuid4().hex[:8]}"
        if not self.staged_at:
            self.staged_at = datetime.now(timezone.utc).isoformat()

    @property
    def is_staged(self) -> bool:
        return self.status == IntegrationStatus.STAGED.value

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> IntegrationBufferEntry:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class IntegrationLogEntry:
    """Immutable log of an integration action."""
    timestamp: str = ""
    link_id: str = ""
    link_url: str = ""
    action: str = ""       # archived, implemented, trial, done, task_created, task_staged
    agent: str = ""
    task_id: Optional[str] = None
    skill_name: Optional[str] = None
    notes: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisIndex:
    updated_at: str = ""
    total: int = 0
    note: str = ""
    top: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AnalysisIndex:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def detect_source(url: str) -> str:
    u = url.lower()
    if "twitter.com" in u or "x.com" in u:
        return SourceType.TWITTER.value
    if "github.com" in u:
        return SourceType.GITHUB.value
    if "arxiv.org" in u:
        return SourceType.ARXIV.value
    if "youtube.com" in u or "youtu.be" in u:
        return SourceType.YOUTUBE.value
    if "reddit.com" in u:
        return SourceType.REDDIT.value
    if "huggingface.co" in u:
        return SourceType.HUGGINGFACE.value
    if "bsky.app" in u:
        return SourceType.BLUESKY.value
    return SourceType.WEB.value


def create_link(url: str, notes: str = "") -> Link:
    return Link(url=url, notes=notes, source_type=detect_source(url))


def parse_numeric_selection(text: str, max_val: int) -> list[int]:
    """Parse a numeric selection string like '1,3,5-8,11' into sorted indices.

    Returns 0-based indices. Invalid or out-of-range values are silently skipped.
    """
    result = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                lo, hi = int(bounds[0]), int(bounds[1])
                for n in range(lo, hi + 1):
                    if 1 <= n <= max_val:
                        result.add(n - 1)
            except ValueError:
                continue
        else:
            try:
                n = int(part)
                if 1 <= n <= max_val:
                    result.add(n - 1)
            except ValueError:
                continue
    return sorted(result)


_TWITTER_STATUS_RE = re.compile(
    r'(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)', re.IGNORECASE
)


def extract_twitter_author_and_id(url: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (author, status_id) from a Twitter/X URL."""
    m = _TWITTER_STATUS_RE.search(url)
    if m:
        return m.group(1).lower(), m.group(2)
    return None, None


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    'LinkStatus', 'SourceType', 'IntegrationStatus', 'IdeaType', 'SortMode',
    'Analysis', 'FetchMetadata', 'Link', 'IntegrationBufferEntry',
    'IntegrationLogEntry', 'AnalysisIndex',
    'detect_source', 'create_link', 'parse_numeric_selection',
    'extract_twitter_author_and_id',
]
