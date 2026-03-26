#!/usr/bin/env python3
"""Interactive workflows and CLI entrypoints for tsundoku."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import __version__
from . import backend
from . import config as app_config
from . import display
from . import fetch
from . import models
from . import storage


VERSION = __version__
ANALYSIS_REQUEST_TIMEOUT = 45
ANALYSIS_MAX_RETRIES = 1
NO_CONTENT_ANALYSIS_TIMEOUT = 15
NO_CONTENT_ANALYSIS_MAX_RETRIES = 0

SORT_MODES = [
    models.SortMode.RELEVANCE_DESC.value,
    models.SortMode.DATE_DESC.value,
    models.SortMode.RELEVANCE_ASC.value,
    models.SortMode.DATE_ASC.value,
    models.SortMode.STATUS.value,
]

ANALYZE_PROMPT = """\
You are analyzing a bookmarked URL for a user's software system.

System name: {system_name}
System description: {system_description}
Integration goal: {integration_goal}
URL: {url}
Source type: {source_type}
User notes: {notes}
Thread context: {thread_note}
{fetched_block}
Based on the above:
1. Provide a concise title.
2. Summarize the content in 2-3 sentences.
3. List key technologies, tools, libraries, or concepts mentioned.
4. Rate relevance to this system on a 1-5 scale, where 5 is immediately actionable.
5. Suggest 1-3 concrete integration ideas or follow-up actions.
6. If there is a credible automation, component, or project that could be created from this item, propose a short slug-safe `automation_name`. Otherwise return an empty string.

Respond with ONLY a complete and valid JSON object, with no markdown fences.
{{"title":"...","summary":"...","technologies":[...],"relevance_score":N,"integration_ideas":[...],"automation_name":"optional-automation-slug"}}
"""

META_PROMPT = """\
You are performing a synthesis of curated tsundoku links for a user's software system.

System name: {system_name}
System description: {system_description}
Integration goal: {integration_goal}

Analyze the links and produce:
1. 3-5 synthesis themes or patterns.
2. 3-5 candidate automation or project ideas. Each idea should include:
   - name
   - description
   - priority (high|med|low)
   - draws_from (list of link ids)
3. Strategic gaps in the current reading list.
4. A priority recommendation for what to build or investigate next.

Return ONLY valid JSON:
{{
  "themes": ["theme1", "theme2"],
  "automation_ideas": [
    {{"name": "idea-name", "description": "...", "priority": "high", "draws_from": ["lnk-1", "lnk-2"]}}
  ],
  "gaps": ["gap1"],
  "priority_recommendation": "..."
}}

=== ANALYZED LINKS ===
{links_block}
"""

EXPECTED_ANALYSIS_KEYS = (
    "title",
    "summary",
    "technologies",
    "relevance_score",
    "integration_ideas",
)
REAPPRAISABLE_STATUSES = {
    models.LinkStatus.ANALYZED.value,
    models.LinkStatus.TRIAL.value,
    models.LinkStatus.IMPLEMENTED.value,
    models.LinkStatus.DONE.value,
}
ANALYSIS_SPINNER_FRAMES = ("|", "/", "-", "\\")
HEADLESS_FETCH_ERROR_MARKERS = (
    "browser fetch unavailable on this host",
    "launch_persistent_context",
    "executable doesn't exist",
    "playwright",
)


def extract_urls(text: str) -> list[str]:
    """Extract HTTP URLs from free-form text."""
    return re.findall(r'https?://[^\s\'"<>]+', text)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or f"item-{uuid.uuid4().hex[:8]}"


def _page_size() -> int:
    try:
        rows = os.get_terminal_size().lines
    except OSError:
        rows = 24
    return max(10, rows - 12)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def _default_agent(role: str) -> str:
    profile = app_config.get_active_profile()
    return str(profile.agents.get(role) or "assistant")


def _system_context() -> tuple[str, str, str]:
    profile = app_config.get_active_profile()
    system_name = profile.system_name or "My System"
    system_description = profile.system_description or "No system description configured."
    integration_goal = profile.integration_goal or "Turn useful links into actionable work."
    return system_name, system_description, integration_goal


def _get_relevance(link: dict) -> int:
    try:
        analysis = link.get("analysis", {})
        if isinstance(analysis, str):
            analysis = json.loads(analysis)
        return int((analysis or {}).get("relevance_score", 0) or 0)
    except Exception:
        return 0


def _sort_links(links: list[dict], sort_mode: str) -> list[dict]:
    sorted_links = list(links)
    if sort_mode == models.SortMode.RELEVANCE_DESC.value:
        sorted_links.sort(key=_get_relevance, reverse=True)
    elif sort_mode == models.SortMode.RELEVANCE_ASC.value:
        sorted_links.sort(key=_get_relevance)
    elif sort_mode == models.SortMode.DATE_ASC.value:
        sorted_links.sort(key=lambda link: link.get("added_at", ""))
    elif sort_mode == models.SortMode.STATUS.value:
        sorted_links.sort(key=lambda link: (link.get("status", ""), -_get_relevance(link), link.get("added_at", "")))
    else:
        sorted_links.sort(key=lambda link: link.get("added_at", ""), reverse=True)
    return sorted_links


def _filter_links(
    links: list[dict],
    *,
    show_archived: bool = False,
    status_filter: str = "",
    search_query: str = "",
) -> list[dict]:
    filtered = []
    query = search_query.lower().strip()
    wanted_status = status_filter.lower().strip()
    for link in links:
        status = str(link.get("status", ""))
        if not show_archived and status == models.LinkStatus.ARCHIVED.value:
            continue
        if wanted_status and status != wanted_status:
            continue
        if query:
            haystack = " ".join(str(value) for value in link.values()).lower()
            if query not in haystack:
                continue
        filtered.append(link)
    return filtered


def _status_counts(links: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for link in links:
        status = str(link.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _prefs() -> dict:
    return storage.get_storage().load_prefs()


def _update_prefs(**updates) -> dict:
    return storage.get_storage().update_prefs(**updates)


def _parse_json_from_response(raw: str) -> dict:
    if not raw:
        return {}
    patterns = [
        r"```(?:json)?\n(.*?)\n```",
        r"\{.*\}",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, re.DOTALL)
        if not match:
            continue
        try:
            return json.loads(match.group(1) if match.lastindex else match.group())
        except Exception:
            continue
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except Exception:
            return {}
    return {}


def _extract_thinking(raw: str) -> str:
    patterns = [
        r"<think(?:ing)?>(.*?)</think(?:ing)?>",
        r"\*\*(?:Thinking|Reasoning|Analysis)\*\*\n(.*?)(?=\n\{|\Z)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _is_headless_fetch_error(error: Optional[str]) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in HEADLESS_FETCH_ERROR_MARKERS)


def _ensure_setup(*, interactive: bool = True) -> app_config.AppConfig:
    config_path = app_config.default_config_path()
    if config_path.exists():
        return app_config.get_config(refresh=True)

    config = app_config.create_default_config()
    if interactive and sys.stdin.isatty():
        display.clr()
        display.rp(display.banner("first-run setup"))
        display.rp("  [bold yellow]tsundoku needs a local config before first use.[/bold yellow]")
        display.rp("  [dim]Press enter to accept defaults. You can reopen this menu later from Settings.[/dim]\n")
        data_dir = display.ri(f"  data directory [dim](default: {app_config.default_data_dir()})[/dim]:").strip()
        config.data_dir = data_dir or str(app_config.default_data_dir())
        fetch_mode = (display.ri("  fetch mode [dim](default: auto)[/dim]:").strip().lower() or "auto")
        config.fetch_mode = fetch_mode if fetch_mode in ("auto", "http", "stealth", "dynamic") else "auto"
        profile = _profile_setup_form(config.profiles["default"], allow_blank_name=False)
        config.profiles = {profile.name: profile}
        config.active_profile = profile.name
    else:
        config.data_dir = str(app_config.default_data_dir())
    app_config.save_config(config, config_path)
    _refresh_runtime_caches()
    return app_config.get_config(refresh=True)


def _refresh_runtime_caches() -> None:
    storage.reset_cache()
    backend.reset_cache()


def _send_message_with_liveness_policy(
    agent: str,
    prompt: str,
    *,
    timeout: int,
    max_retries: int,
    retry_on_timeout: bool,
) -> tuple[dict, float]:
    started = time.monotonic()
    try:
        client = backend.get_client()
        if not client.configured():
            return (
                {
                    "error": (
                        "No backend is configured. Open Settings and configure an HTTP JSON profile "
                        "before running analysis or meta-analysis."
                    )
                },
                0.0,
            )
    except Exception as exc:
        return ({"error": str(exc)}, 0.0)

    if not sys.stdout.isatty():
        try:
            return (
                client.send_message(
                    agent,
                    prompt,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_on_timeout=retry_on_timeout,
                ),
                time.monotonic() - started,
            )
        except Exception as exc:
            return ({"error": str(exc)}, time.monotonic() - started)

    result_box: dict = {}
    error_box: dict = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            result_box["result"] = client.send_message(
                agent,
                prompt,
                timeout=timeout,
                max_retries=max_retries,
                retry_on_timeout=retry_on_timeout,
            )
        except Exception as exc:  # pragma: no cover - defensive wrapper
            error_box["error"] = str(exc)
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    frame_idx = 0
    while True:
        elapsed = time.monotonic() - started
        mins, secs = divmod(int(elapsed), 60)
        frame = ANALYSIS_SPINNER_FRAMES[frame_idx % len(ANALYSIS_SPINNER_FRAMES)]
        display.transient_status(f"  [{frame}] waiting on {agent}  {mins:02d}:{secs:02d}")
        if done.wait(0.12):
            break
        frame_idx += 1

    display.clear_transient_status()
    if "error" in error_box:
        return {"error": error_box["error"]}, time.monotonic() - started
    return result_box.get("result", {"error": "backend returned no result"}), time.monotonic() - started


def _analysis_missing_fields(link: dict) -> list[str]:
    if link.get("status") not in REAPPRAISABLE_STATUSES:
        return []
    missing: list[str] = []
    raw_analysis = link.get("analysis")
    parsed = _parse_json_from_response(raw_analysis or "")
    if not raw_analysis:
        missing.append("analysis")
    if not parsed:
        missing.append("parsed_json")
    else:
        for key in EXPECTED_ANALYSIS_KEYS:
            if key not in parsed:
                missing.append(key)
    if not link.get("title"):
        missing.append("title")
    if not link.get("summary"):
        missing.append("summary")
    return list(dict.fromkeys(missing))


def _needs_reappraisal(link: dict) -> bool:
    return bool(_analysis_missing_fields(link))


def _analysis_result_status(link: dict) -> str:
    status = link.get("status") or models.LinkStatus.UNREAD.value
    if status in REAPPRAISABLE_STATUSES:
        return status
    return models.LinkStatus.ANALYZED.value


def _fetch_link_content(url: str) -> tuple[str, str, dict]:
    fetch_meta = {
        "fetch_chars": None,
        "fetch_mode": None,
        "fetch_elapsed": None,
        "fetch_error": None,
    }
    mode = app_config.get_config().fetch_mode or "auto"
    started = time.monotonic()
    result = fetch.fetch_url(url, mode=mode) or {}
    fetch_meta["fetch_elapsed"] = round(time.monotonic() - started, 3)
    if not result.get("ok"):
        fetch_meta["fetch_error"] = _truncate(str(result.get("error") or "fetch failed"), 200)
        fetch_meta["fetch_mode"] = result.get("mode", mode)
        return "", "", fetch_meta

    text = result.get("text") or ""
    title = result.get("title") or ""
    fetch_meta["fetch_chars"] = result.get("chars", len(text))
    fetch_meta["fetch_mode"] = result.get("mode", mode)
    if not text:
        fetch_meta["fetch_error"] = "fetch returned no text"
        return "", "", fetch_meta

    fetched_text = text[:8000]
    fetched_block = f"\nFETCHED CONTENT:\n{title}\n\n{fetched_text}"
    return fetched_block, fetched_text, fetch_meta


def _detect_twitter_thread(url: str, fetched_text: str) -> dict:
    author, _status_id = models.extract_twitter_author_and_id(url)
    if not author:
        return {}
    all_statuses = re.findall(
        rf"(?:twitter\.com|x\.com)/{re.escape(author)}/status/(\d+)",
        fetched_text,
        re.IGNORECASE,
    )
    unique_statuses = list(dict.fromkeys(all_statuses))
    if len(unique_statuses) < 2:
        return {}
    reply_to_self = bool(
        re.search(
            rf"(?:replying\s+to|in\s+reply\s+to)\s+@{re.escape(author)}",
            fetched_text,
            re.IGNORECASE,
        )
    )
    show_thread = "show this thread" in fetched_text.lower()
    if len(unique_statuses) >= 2 or reply_to_self or show_thread:
        return {
            "is_thread": True,
            "thread_urls": [f"https://x.com/{author}/status/{sid}" for sid in unique_statuses],
            "thread_author": author,
            "thread_post_count": len(unique_statuses),
        }
    return {}


def _append_integration_log(
    link: dict,
    *,
    action: str,
    agent: str = "",
    task_id: Optional[str] = None,
    notes: str = "",
) -> None:
    entry = models.IntegrationLogEntry(
        link_id=link.get("id", ""),
        link_url=link.get("url", ""),
        action=action,
        agent=agent,
        task_id=task_id,
        notes=notes,
    )
    storage.get_storage().append_integration_log(entry.to_dict())


def _resolve_link_input(links: list[dict], value: str) -> Optional[dict]:
    if not value:
        return None
    if value.startswith("lnk-"):
        return next((link for link in links if link.get("id") == value), None)
    try:
        index = int(value) - 1
    except ValueError:
        return None
    if 0 <= index < len(links):
        return links[index]
    return None


def pick_links(links: list[dict], *, multi: bool = False, prompt: str = "select:") -> list[dict]:
    if not links:
        display.rp("  [dim]no links available[/dim]")
        return []
    if multi:
        return display.show_selectable_table(links, prompt=prompt, page_size=_page_size())

    display.show_table(links, page=0, page_size=_page_size())
    display.rp("")
    value = display.ri(f"  {prompt}")
    if not value or value.lower() == "q":
        return []
    selected = _resolve_link_input(links, value)
    if selected:
        return [selected]
    display.rp(f"  [red]not found: {display.escape(value)}[/red]")
    return []


def do_add() -> None:
    display.clr()
    display.rp(display.banner("add links"))
    display.rp("  [bold yellow]paste URLs, one per line. Submit a blank line to finish.[/bold yellow]")
    display.rp("  [dim]Type  file  to load URLs from a text file.[/dim]\n")

    raw_lines: list[str] = []
    while True:
        line = display.ri("  >")
        if not line:
            if raw_lines:
                break
            continue
        if line.lower() == "file":
            file_path = display.ri("  file path:")
            try:
                candidate = Path(file_path).expanduser()
                if ".." in file_path:
                    display.rp("  [red]error: path traversal not allowed[/red]")
                    continue
                raw_lines = candidate.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                display.rp(f"  [red]error reading file: {display.escape(str(exc))}[/red]")
            break
        raw_lines.append(line)

    urls: list[str] = []
    for line in raw_lines:
        found = extract_urls(line)
        if found:
            urls.extend(found)
        elif re.match(r"^https?://", line.strip()):
            urls.append(line.strip())
    seen: set[str] = set()
    urls = [url for url in urls if not (url in seen or seen.add(url))]

    if not urls:
        display.rp("  [red]no URLs found[/red]")
        display.pause()
        return

    display.rp(f"\n  [bold yellow]{len(urls)} URL(s) found:[/bold yellow]")
    for url in urls[:8]:
        display.rp(f"  [dim]-[/dim] {display.escape(_truncate(url, 72))}")
    if len(urls) > 8:
        display.rp(f"  [dim]... and {len(urls) - 8} more[/dim]")

    notes = display.ri("\n  notes (optional):") or ""
    storage_mgr = storage.get_storage()
    links = storage_mgr.load_links()
    existing = {link["url"] for link in links}
    added = 0
    duplicates = 0
    for url in urls:
        if url in existing:
            duplicates += 1
            continue
        links.append(models.create_link(url, notes).to_dict())
        existing.add(url)
        added += 1

    storage_mgr.save_links(links)
    display.rp(f"\n  [green]added {added} link(s)[/green]", end="")
    if duplicates:
        display.rp(f"  [dim]({duplicates} duplicate(s) skipped)[/dim]", end="")
    display.rp("")
    display.pause()


def do_list(sort_mode: Optional[str] = None) -> None:
    prefs = _prefs()
    sort_mode = sort_mode or str(prefs.get("sort_mode") or models.SortMode.DATE_DESC.value)
    page = 0
    show_archived = False
    status_filter = ""
    search_query = ""
    raw_mode = display.HAS_RAW_INPUT and sys.stdin.isatty()

    while True:
        all_links = storage.get_storage().load_links()
        if not all_links:
            display.clr()
            display.rp(display.banner("all links"))
            display.rp("  [dim]no links yet[/dim]")
            display.pause()
            return

        filtered = _filter_links(
            all_links,
            show_archived=show_archived,
            status_filter=status_filter,
            search_query=search_query,
        )
        links = _sort_links(filtered, sort_mode)
        page_size = _page_size()
        total_pages = max(1, (max(1, len(links)) + page_size - 1) // page_size)
        page = min(page, total_pages - 1)

        display.clr()
        subtitle = f"all links  [sort: {sort_mode}]"
        display.rp(display.banner(subtitle))
        counts = _status_counts(all_links)
        display.rp("  " + "  ".join(f"[dim]{k}:[/dim] {v}" for k, v in sorted(counts.items())))
        display.rp(
            "  [dim]archived:[/dim] "
            f"{'shown' if show_archived else 'hidden'}"
            f"  [dim]filter:[/dim] {status_filter or 'none'}"
            f"  [dim]search:[/dim] {display.escape(search_query or 'none')}"
        )
        display.rp("")
        if links:
            display.show_table(links, page=page, page_size=page_size)
        else:
            display.rp("  [yellow]no links match current filters[/yellow]")

        display.rp("")
        display.rp(
            "  [dim]j/k or n/p page  g/gg top  G bottom  [s] sort  [a] archived on/off  "
            "[f] filter status  [/] search  [v] view  [r] reappraise  [x] archive  [q] back[/dim]"
        )
        choice = display._read_key() if raw_mode else display.ri("  >").strip()
        if not choice:
            continue

        lowered = choice.lower() if isinstance(choice, str) else str(choice).lower()
        if lowered == "q":
            return
        if choice == "G":
            page = total_pages - 1
            continue
        if lowered in ("g", "gg"):
            page = 0
            continue
        if lowered in ("n", "j", "l", "right", "down"):
            page = min(total_pages - 1, page + 1)
            continue
        if lowered in ("p", "k", "h", "left", "up"):
            page = max(0, page - 1)
            continue
        if lowered == "s":
            idx = SORT_MODES.index(sort_mode) if sort_mode in SORT_MODES else 0
            sort_mode = SORT_MODES[(idx + 1) % len(SORT_MODES)]
            _update_prefs(sort_mode=sort_mode)
            page = 0
            continue
        if lowered == "a":
            show_archived = not show_archived
            page = 0
            continue
        if lowered == "f":
            status_filter = (display.ri("  status filter (blank clears):") or "").strip()
            page = 0
            continue
        if lowered == "/":
            search_query = (display.ri("  search (blank clears):") or "").strip()
            page = 0
            continue
        if lowered.startswith("/") and len(choice) > 1:
            search_query = choice[1:].strip()
            page = 0
            continue
        if lowered == "v":
            picked = pick_links(links, prompt="enter link # or id:")
            if picked:
                do_view(picked[0]["id"])
            continue
        if lowered == "r":
            do_reappraise(links_override=links, prompt="reappraise which visible links")
            continue
        if lowered == "x":
            if links:
                do_archive(links_override=links)
            continue

        selected = _resolve_link_input(links, choice)
        if selected:
            do_view(selected["id"])
        else:
            display.rp(f"  [red]unknown selection: {display.escape(str(choice))}[/red]")
            display.pause()


def do_view(link_id: Optional[str] = None) -> None:
    display.clr()
    display.rp(display.banner("view link"))
    links = storage.get_storage().load_links()
    if link_id:
        link = next((item for item in links if item.get("id") == link_id), None)
    else:
        picked = pick_links(links, prompt="enter link # or id:")
        link = picked[0] if picked else None

    if not link:
        if link_id:
            display.rp(f"  [red]not found: {display.escape(link_id)}[/red]")
            display.pause()
        return

    display.rp(
        f"""
  [bold yellow]id:[/bold yellow]         {display.escape(link['id'])}
  [bold yellow]url:[/bold yellow]        {display.escape(link['url'])}
  [bold yellow]source:[/bold yellow]     {display.escape(link.get('source_type', 'web'))}
  [bold yellow]status:[/bold yellow]     {display.escape(link.get('status', 'unknown'))}
  [bold yellow]added:[/bold yellow]      {display.escape(link.get('added_at', ''))[:19].replace('T', ' ')} UTC
  [bold yellow]title:[/bold yellow]      {display.escape(link.get('title') or '-')}
  [bold yellow]summary:[/bold yellow]    {display.escape(link.get('summary') or '-')}
  [bold yellow]tags:[/bold yellow]       {display.escape(', '.join(link.get('tags', [])) or '-')}
  [bold yellow]notes:[/bold yellow]      {display.escape(link.get('notes') or '-')}
  [bold yellow]agent:[/bold yellow]      {display.escape(link.get('agent_used') or '-')}
  [bold yellow]model:[/bold yellow]      {display.escape(link.get('analyzed_by_model') or '-')}
  [bold yellow]fetch:[/bold yellow]      {display.escape(link.get('fetch_mode') or '-')} / {link.get('fetch_chars') or '-'} chars
"""
    )
    if link.get("automation_name"):
        display.rp(f"  [bold yellow]automation:[/bold yellow] {display.escape(link.get('automation_name'))}")
    if link.get("is_thread"):
        display.rp(
            f"  [bold yellow]thread:[/bold yellow]     "
            f"{link.get('thread_post_count', 0)} posts by @{display.escape(link.get('thread_author') or '')}"
        )
    if link.get("archived_at"):
        display.rp(
            f"  [bold yellow]archived:[/bold yellow]   "
            f"{display.escape(link.get('archived_at', ''))}  "
            f"({display.escape(link.get('archived_reason') or 'no reason')})"
        )
    if link.get("fetch_error"):
        display.rp(f"  [bold yellow]fetch err:[/bold yellow]  {display.escape(link['fetch_error'])}")

    if link.get("analysis"):
        display.rp("\n  [bold yellow]analysis:[/bold yellow]")
        parsed = _parse_json_from_response(link.get("analysis") or "")
        if parsed:
            for key, value in parsed.items():
                if key == "relevance_score":
                    display.rp(f"    [dim]{key}:[/dim] {display.relevance_bar(int(value))}")
                elif isinstance(value, list):
                    display.rp(f"    [dim]{key}:[/dim] {display.escape(', '.join(str(item) for item in value[:8]))}")
                else:
                    display.rp(f"    [dim]{key}:[/dim] {display.escape(_truncate(str(value), 160))}")
        else:
            display.rp(f"    {display.escape(_truncate(str(link['analysis']), 600))}")

    thinking = link.get("thinking_trace")
    if thinking:
        display.rp("\n  [dim]thinking trace:[/dim]")
        for chunk in textwrap.wrap(thinking[:600], width=84):
            display.rp(f"  {display.escape(chunk)}")
        if len(thinking) > 600:
            display.rp(f"  [dim]... {len(thinking) - 600} more chars[/dim]")
    display.pause()


def do_analyze(link_id: Optional[str] = None) -> None:
    display.clr()
    display.rp(display.banner("analyze selected"))
    links = storage.get_storage().load_links()
    if link_id:
        selected = [item for item in links if item.get("id") == link_id]
    else:
        candidates = [
            item
            for item in links
            if item.get("status") not in (models.LinkStatus.ARCHIVED.value, models.LinkStatus.ANALYZING.value)
        ]
        if not candidates:
            display.rp("  [yellow]no analyzable links[/yellow]")
            display.pause()
            return
        display.rp("  [bold yellow]select link(s) to analyze:[/bold yellow]\n")
        selected = pick_links(candidates, multi=True, prompt="select links to analyze")

    if not selected:
        if link_id:
            display.rp(f"  [red]not found: {display.escape(link_id)}[/red]")
            display.pause()
        return

    if len(selected) == 1:
        link = selected[0]
        display.rp(f"\n  [cyan]url:[/cyan] {display.escape(_truncate(link['url'], 80))}")
        display.rp(
            "  [dim]source: "
            f"{display.escape(link.get('source_type', 'web'))}  "
            f"status: {display.escape(link.get('status', 'unknown'))}[/dim]\n"
        )
    else:
        display.rp(f"\n  [cyan]selected:[/cyan] {len(selected)} link(s)\n")

    default_agent = _default_agent("analysis")
    agent = display.ri(f"  agent [dim](default: {default_agent})[/dim]:") or default_agent
    _run_analysis_batch(selected, agent)
    display.pause()


def do_analyze_all() -> None:
    display.clr()
    display.rp(display.banner("analyze all"))
    links = storage.get_storage().load_links()
    pending = [
        link
        for link in links
        if link.get("status") in (models.LinkStatus.UNREAD.value, models.LinkStatus.FETCH_ERROR.value)
    ]
    if not pending:
        display.rp("  [yellow]no unread or fetch-error links[/yellow]")
        display.pause()
        return

    display.rp(f"  [bold yellow]{len(pending)} link(s) pending analysis[/bold yellow]\n")
    display.show_table(pending, page=0, page_size=min(len(pending), _page_size()))
    default_agent = _default_agent("analysis")
    agent = display.ri(f"\n  agent [dim](default: {default_agent})[/dim]:") or default_agent
    confirm = display.ri(f"\n  analyze all {len(pending)} with {agent}? [y/N]:").lower()
    if confirm != "y":
        return
    _run_analysis_batch(pending, agent)
    display.pause()


def do_reappraise(links_override: Optional[list[dict]] = None, prompt: str = "reappraise which links") -> None:
    display.clr()
    display.rp(display.banner("reappraise incomplete"))
    links = links_override or storage.get_storage().load_links()
    candidates = [link for link in links if _needs_reappraisal(link)]
    if not candidates:
        display.rp("  [green]no incomplete analyzed links found[/green]")
        display.pause()
        return

    display.rp(f"  [bold yellow]{len(candidates)} incomplete analyzed link(s)[/bold yellow]")
    display.rp("  [dim]these links have analysis statuses but missing structured analysis fields.[/dim]\n")
    selected = pick_links(candidates, multi=True, prompt=prompt)
    if not selected:
        return

    default_agent = _default_agent("analysis")
    agent = display.ri(f"\n  agent [dim](default: {default_agent})[/dim]:") or default_agent
    _run_analysis_batch(selected, agent)
    display.pause()


def _run_analysis_batch(links: list[dict], agent: str) -> tuple[int, int]:
    if not links:
        return 0, 0
    display.rp("")
    success_count = 0
    total = len(links)
    for idx, link in enumerate(links, 1):
        missing = _analysis_missing_fields(link)
        suffix = f"  [dim](missing: {', '.join(missing)})[/dim]" if missing else ""
        display.rp(f"  [{idx}/{total}] [cyan]{display.escape(_truncate(link['url'], 68))}[/cyan]{suffix}")
        ok = _run_analysis(
            link,
            agent,
            final_status=_analysis_result_status(link),
            on_error_status=link.get("status") or models.LinkStatus.UNREAD.value,
        )
        if not ok:
            display.rp("      [red]failed[/red]")
            continue
        refreshed = storage.get_storage().get_link(link["id"]) or link
        display.rp(
            f"      [green]ok[/green] {display.escape(_truncate(refreshed.get('title') or refreshed['url'], 58))}"
            f"  [dim](status: {display.escape(refreshed.get('status', 'unknown'))}, rel: {_get_relevance(refreshed)}/5)[/dim]"
        )
        success_count += 1
    display.rp(f"\n  [green]done[/green]  [dim]{success_count}/{total} succeeded[/dim]")
    return success_count, total - success_count


def _run_analysis(
    link: dict,
    agent: str,
    *,
    final_status: Optional[str] = None,
    on_error_status: Optional[str] = None,
) -> bool:
    display.rp(f"\n  [dim]analyzing with {display.escape(agent)}...[/dim]")
    storage_mgr = storage.get_storage()
    original_status = link.get("status") or models.LinkStatus.UNREAD.value
    final_status = final_status or _analysis_result_status(link)
    on_error_status = on_error_status or original_status
    storage_mgr.update_link(link["id"], status=models.LinkStatus.ANALYZING.value)

    fetched_block = ""
    fetched_text = ""
    fetch_meta = {
        "fetch_chars": None,
        "fetch_mode": None,
        "fetch_elapsed": None,
        "fetch_error": None,
    }
    thread_info: dict = {}

    try:
        fetched_block, fetched_text, fetch_meta = _fetch_link_content(link["url"])
        if fetched_text:
            display.rp(
                f"  [green]fetched[/green] {fetch_meta['fetch_chars']:,} chars "
                f"via {display.escape(fetch_meta['fetch_mode'] or 'unknown')}"
            )
    except Exception as exc:
        fetch_meta["fetch_error"] = _truncate(str(exc), 200)

    if fetch_meta["fetch_error"]:
        display.rp(f"  [dim]fetch unavailable: {display.escape(fetch_meta['fetch_error'])} - continuing[/dim]")

    if (
        link.get("source_type") == models.SourceType.TWITTER.value
        and not fetched_text
        and fetch_meta["fetch_error"]
        and _is_headless_fetch_error(fetch_meta["fetch_error"])
    ):
        display.rp("  [yellow]skipping agent analysis: browser-backed fetch is unavailable on this host[/yellow]")
        fallback_status = (
            models.LinkStatus.FETCH_ERROR.value
            if original_status in (
                models.LinkStatus.UNREAD.value,
                models.LinkStatus.FETCH_ERROR.value,
                models.LinkStatus.ANALYZING.value,
            )
            else on_error_status
        )
        storage_mgr.update_link(
            link["id"],
            status=fallback_status,
            fetch_error=fetch_meta["fetch_error"],
            fetch_chars=fetch_meta["fetch_chars"],
            fetch_mode=fetch_meta["fetch_mode"],
            fetch_elapsed=fetch_meta["fetch_elapsed"],
        )
        return False

    if fetched_text and link.get("source_type") == models.SourceType.TWITTER.value:
        thread_info = _detect_twitter_thread(link["url"], fetched_text)
        if thread_info:
            display.rp(f"  [dim]thread detected: {thread_info.get('thread_post_count', 0)} posts[/dim]")

    thread_note = "not a thread"
    if thread_info:
        thread_note = (
            f"THREAD with {thread_info['thread_post_count']} posts by "
            f"@{thread_info['thread_author']}. Analyze the thread as a whole."
        )

    system_name, system_description, integration_goal = _system_context()
    prompt = ANALYZE_PROMPT.format(
        system_name=system_name,
        system_description=system_description,
        integration_goal=integration_goal,
        url=link["url"],
        source_type=link.get("source_type", "web"),
        notes=link.get("notes") or "none",
        thread_note=thread_note,
        fetched_block=fetched_block,
    )

    request_timeout = ANALYSIS_REQUEST_TIMEOUT if fetched_text else NO_CONTENT_ANALYSIS_TIMEOUT
    request_retries = ANALYSIS_MAX_RETRIES if fetched_text else NO_CONTENT_ANALYSIS_MAX_RETRIES
    result, analysis_wait = _send_message_with_liveness_policy(
        agent,
        prompt,
        timeout=request_timeout,
        max_retries=request_retries,
        retry_on_timeout=False,
    )
    if "error" in result:
        display.rp(f"  [red]error: {display.escape(str(result['error']))}[/red]")
        fallback_status = on_error_status
        if fetch_meta["fetch_error"] and original_status in (
            models.LinkStatus.UNREAD.value,
            models.LinkStatus.FETCH_ERROR.value,
            models.LinkStatus.ANALYZING.value,
        ):
            fallback_status = models.LinkStatus.FETCH_ERROR.value
        storage_mgr.update_link(
            link["id"],
            status=fallback_status,
            fetch_error=fetch_meta["fetch_error"],
            fetch_chars=fetch_meta["fetch_chars"],
            fetch_mode=fetch_meta["fetch_mode"],
            fetch_elapsed=fetch_meta["fetch_elapsed"],
        )
        return False

    envelope = backend.get_client().normalize_message_response(result)
    raw_response = envelope.text or result.get("response", "") or result.get("message", "")
    now = datetime.now(timezone.utc).isoformat()
    thinking = _extract_thinking(raw_response)
    analysis = _parse_json_from_response(raw_response)
    update_kwargs = {
        "status": final_status,
        "agent_used": agent,
        "analyzed_at": now,
        "thinking_trace": thinking,
        "fetch_chars": fetch_meta["fetch_chars"],
        "fetch_mode": fetch_meta["fetch_mode"],
        "fetch_elapsed": fetch_meta["fetch_elapsed"],
        "fetch_error": fetch_meta["fetch_error"],
        "analyzed_by_model": envelope.model,
    }
    if thread_info:
        update_kwargs.update(thread_info)
    if analysis:
        update_kwargs.update(
            {
                "title": analysis.get("title"),
                "summary": analysis.get("summary"),
                "analysis": json.dumps(analysis, indent=2, ensure_ascii=False),
                "tags": analysis.get("technologies", []),
                "automation_name": analysis.get("automation_name") or link.get("automation_name"),
            }
        )
    else:
        update_kwargs["analysis"] = raw_response
    storage_mgr.update_link(link["id"], **update_kwargs)
    refreshed = storage_mgr.get_link(link["id"])
    if refreshed:
        storage_mgr.save_analysis_file(refreshed)
    display.rp(f"  [green]analysis complete[/green]  [dim]({analysis_wait:.1f}s backend wait)[/dim]")
    return True


def _stage_integration(link: dict, idea_text: str, *, agent: str) -> None:
    entry = models.IntegrationBufferEntry(
        link_id=link.get("id", ""),
        link_url=link.get("url", ""),
        link_title=link.get("title") or link.get("url", ""),
        idea=idea_text,
        idea_type=models.IdeaType.TASK.value,
        agent=agent,
    )
    buffer_entries = storage.get_storage().load_integ_buffer()
    buffer_entries.append(entry.to_dict())
    storage.get_storage().save_integ_buffer(buffer_entries)


def _integration_text(link: dict) -> str:
    analysis = _parse_json_from_response(link.get("analysis") or "")
    ideas = analysis.get("integration_ideas", [])
    if ideas:
        display.rp("\n  [bold yellow]integration ideas:[/bold yellow]")
        for idx, idea in enumerate(ideas, 1):
            display.rp(f"    [dim]{idx}.[/dim] {display.escape(_truncate(str(idea), 88))}")
    default = link.get("summary") or link.get("title") or link["url"]
    raw = display.ri("\n  idea #, or custom text [dim](blank uses summary/title)[/dim]:").strip()
    if not raw:
        return str(default)
    if ideas:
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(ideas):
                return str(ideas[idx])
        except ValueError:
            pass
    return raw


def _build_task_text(link: dict, idea_text: str) -> str:
    title = link.get("title") or link["url"]
    summary = link.get("summary") or "(none)"
    automation = link.get("automation_name") or ""
    lines = [
        f"[tsundoku] {title}",
        f"URL: {link['url']}",
        f"Summary: {summary}",
        f"Idea: {idea_text}",
    ]
    if automation:
        lines.append(f"Automation: {automation}")
    return "\n".join(lines)


def do_integrate(link_id: Optional[str] = None) -> None:
    display.clr()
    display.rp(display.banner("integrate link"))
    links = storage.get_storage().load_links()
    candidates = [
        link
        for link in links
        if link.get("status") in (
            models.LinkStatus.ANALYZED.value,
            models.LinkStatus.TRIAL.value,
            models.LinkStatus.IMPLEMENTED.value,
            models.LinkStatus.DONE.value,
        )
    ]
    if not candidates:
        display.rp("  [yellow]no analyzed links - run analyze first[/yellow]")
        display.pause()
        return

    if link_id:
        link = next((item for item in candidates if item.get("id") == link_id), None)
    else:
        display.rp("  [bold yellow]select a link to integrate:[/bold yellow]\n")
        picked = pick_links(candidates, prompt="enter link # or id:")
        link = picked[0] if picked else None

    if not link:
        if link_id:
            display.rp(f"  [red]not found: {display.escape(link_id)}[/red]")
            display.pause()
        return

    title = link.get("title") or link["url"]
    summary = link.get("summary") or ""
    display.rp(f"\n  [cyan]url:[/cyan]     {display.escape(_truncate(link['url'], 74))}")
    display.rp(f"  [cyan]title:[/cyan]   {display.escape(_truncate(title, 74))}")
    if summary:
        display.rp(f"  [cyan]summary:[/cyan] {display.escape(_truncate(summary, 120))}")
    if link.get("automation_name"):
        display.rp(f"  [cyan]automation:[/cyan] {display.escape(link.get('automation_name'))}")

    while True:
        display.rp(
            """
  [bold yellow]what would you like to do?[/bold yellow]

  [bold yellow][1][/bold yellow] stage idea in local integration buffer
  [bold yellow][2][/bold yellow] create task immediately on backend
  [bold yellow][t][/bold yellow] mark as trial
  [bold yellow][i][/bold yellow] mark as implemented
  [bold yellow][d][/bold yellow] mark as done
  [bold yellow][q][/bold yellow] back
"""
        )
        choice = (display.ri("  >") or "").strip().lower()
        if not choice or choice == "q":
            return

        if choice == "t":
            storage.get_storage().update_link(link["id"], status=models.LinkStatus.TRIAL.value)
            _append_integration_log(link, action="trial", notes="marked trial")
            display.rp("  [magenta]marked as trial[/magenta]")
            display.pause()
            return
        if choice == "i":
            storage.get_storage().update_link(link["id"], status=models.LinkStatus.IMPLEMENTED.value)
            _append_integration_log(link, action="implemented", notes="marked implemented")
            display.rp("  [green bold]marked as implemented[/green bold]")
            display.pause()
            return
        if choice == "d":
            storage.get_storage().update_link(link["id"], status=models.LinkStatus.DONE.value)
            _append_integration_log(link, action="done", notes="marked done")
            display.rp("  [green]marked as done[/green]")
            display.pause()
            return
        if choice not in ("1", "2"):
            display.rp("  [red]invalid choice[/red]")
            display.pause()
            return

        idea_text = _integration_text(link)
        default_agent = _default_agent("task")
        agent = display.ri(f"  agent [dim](default: {default_agent})[/dim]:").strip() or default_agent
        task_text = _build_task_text(link, idea_text)

        if choice == "1":
            _stage_integration(link, task_text, agent=agent)
            _append_integration_log(link, action="task_staged", agent=agent, notes="staged to buffer")
            display.rp("  [green]staged to buffer[/green]")
            display.pause()
            return

        client = backend.get_client()
        if not client.configured():
            display.rp("  [red]no backend configured. Use Settings before creating remote tasks.[/red]")
            display.pause()
            return
        priority = display.ri("  priority [dim](default: med)[/dim]:").strip().lower() or "med"
        notes = display.ri("  task notes (optional):").strip()
        result = client.create_task(task_text, agent=agent, priority=priority, notes=notes)
        if "error" in result:
            display.rp(f"  [red]error: {display.escape(str(result['error']))}[/red]")
            display.pause()
            return

        task_id = backend.get_client().normalize_message_response(result).task_id or "?"
        storage.get_storage().update_link(link["id"], status=models.LinkStatus.IMPLEMENTED.value)
        _append_integration_log(
            link,
            action="task_created",
            agent=agent,
            task_id=task_id,
            notes="created immediately",
        )
        display.rp(f"  [green]task created: {display.escape(task_id)}[/green]")
        display.pause()
        return


def do_archive(links_override: Optional[list[dict]] = None) -> None:
    display.clr()
    display.rp(display.banner("archive links"))
    source_links = links_override or storage.get_storage().load_links()
    archivable = [link for link in source_links if link.get("status") != models.LinkStatus.ARCHIVED.value]
    if not archivable:
        display.rp("  [dim]nothing to archive[/dim]")
        display.pause()
        return
    selected = pick_links(archivable, multi=True, prompt="archive which links")
    if not selected:
        return
    reason = (display.ri("  reason (optional):") or "manually archived").strip()
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    for link in selected:
        if storage.get_storage().update_link(
            link["id"],
            status=models.LinkStatus.ARCHIVED.value,
            archived_at=now,
            archived_reason=reason,
        ):
            _append_integration_log(link, action="archived", notes=reason)
            updated += 1
    display.rp(f"  [green]archived {updated} link(s)[/green]")
    display.pause()


def do_meta() -> None:
    display.clr()
    display.rp(display.banner("meta analysis"))
    links = storage.get_storage().load_links()
    analyzed = [
        link
        for link in links
        if link.get("status") in (
            models.LinkStatus.ANALYZED.value,
            models.LinkStatus.TRIAL.value,
            models.LinkStatus.IMPLEMENTED.value,
            models.LinkStatus.DONE.value,
        )
        and link.get("analysis")
    ]
    if len(analyzed) < 3:
        display.rp(f"  [yellow]need at least 3 analyzed links - have {len(analyzed)}[/yellow]")
        display.pause()
        return

    scored = sorted(analyzed, key=_get_relevance, reverse=True)[:20]
    lines_block = []
    for link in scored:
        analysis = _parse_json_from_response(link.get("analysis") or "")
        title = link.get("title") or link["url"]
        summary = link.get("summary") or analysis.get("summary") or "(no summary)"
        ideas = analysis.get("integration_ideas", [])
        block = (
            f"[{link['id']}] {title} (relevance {_get_relevance(link)}/5)\n"
            f"  URL: {link['url']}\n"
            f"  Summary: {_truncate(summary, 220)}\n"
        )
        if ideas:
            block += f"  Ideas: {'; '.join(_truncate(str(idea), 80) for idea in ideas[:3])}\n"
        lines_block.append(block)

    system_name, system_description, integration_goal = _system_context()
    default_agent = _default_agent("meta")
    chosen_agent = display.ri(f"  agent [dim](default: {default_agent})[/dim]:").strip() or default_agent
    display.rp(f"\n  [dim]meta-analyzing {len(scored)} links with {display.escape(chosen_agent)}...[/dim]")

    result, _elapsed = _send_message_with_liveness_policy(
        chosen_agent,
        META_PROMPT.format(
            system_name=system_name,
            system_description=system_description,
            integration_goal=integration_goal,
            n=len(scored),
            links_block="\n".join(lines_block),
        ),
        timeout=240,
        max_retries=0,
        retry_on_timeout=False,
    )
    if "error" in result:
        display.rp(f"  [red]error: {display.escape(str(result['error']))}[/red]")
        display.pause()
        return

    raw = backend.get_client().normalize_message_response(result).text or result.get("response", "") or result.get("message", "")
    meta = _parse_json_from_response(raw)
    if not meta:
        display.rp("  [yellow]could not parse JSON[/yellow]")
        display.rp(f"  [dim]{display.escape(_truncate(raw, 400))}[/dim]")
        display.pause()
        return

    themes = meta.get("themes", [])
    if themes:
        display.rp("\n  [bold yellow]themes:[/bold yellow]")
        for theme in themes:
            display.rp(f"  [yellow]-[/yellow] {display.escape(str(theme))}")

    automation_ideas = meta.get("automation_ideas", [])
    if automation_ideas:
        display.rp("\n  [bold yellow]automation ideas:[/bold yellow]")
        for idx, idea in enumerate(automation_ideas, 1):
            priority = str(idea.get("priority", "med"))
            color = "green" if priority == "high" else ("yellow" if priority == "med" else "dim")
            display.rp(
                f"  [bold yellow]{idx}.[/bold yellow] "
                f"[{color}]{display.escape(priority)}[/{color}] "
                f"{display.escape(idea.get('name', f'idea-{idx}'))}"
            )
            display.rp(f"      {display.escape(_truncate(str(idea.get('description', '')), 96))}")
            draws_from = idea.get("draws_from", [])
            if draws_from:
                display.rp(f"      [dim]draws from: {display.escape(', '.join(draws_from[:6]))}[/dim]")

    gaps = meta.get("gaps", [])
    if gaps:
        display.rp("\n  [bold yellow]gaps:[/bold yellow]")
        for gap in gaps:
            display.rp(f"  [dim]-[/dim] {display.escape(_truncate(str(gap), 96))}")

    recommendation = meta.get("priority_recommendation", "")
    if recommendation:
        display.rp("\n  [bold yellow]priority recommendation:[/bold yellow]")
        for line in textwrap.wrap(str(recommendation), width=78):
            display.rp(f"  {display.escape(line)}")
    display.pause()


def _prompt_paths(prompt: str, values: list[str]) -> list[str]:
    raw = display.ri(f"  {prompt} [dim](comma-separated; default: {', '.join(values)})[/dim]:").strip()
    if not raw:
        return list(values)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _prompt_auth(auth: app_config.AuthConfig) -> app_config.AuthConfig:
    current = auth.type or "none"
    choice = (
        display.ri("  auth mode [dim](none|bearer|header|body|query; default: " + current + ")[/dim]:")
        .strip()
        .lower()
        or current
    )
    updated = app_config.AuthConfig(type=choice)
    if choice in ("bearer", "header", "body", "query"):
        updated.env_var = display.ri(
            f"  secret env var [dim](default: {auth.env_var or 'TSUNDOKU_API_TOKEN'})[/dim]:"
        ).strip() or auth.env_var or "TSUNDOKU_API_TOKEN"
    if choice in ("bearer", "header"):
        updated.header_name = display.ri(
            f"  header name [dim](default: {auth.header_name})[/dim]:"
        ).strip() or auth.header_name
        updated.header_prefix = display.ri(
            f"  header prefix [dim](default: {auth.header_prefix})[/dim]:"
        ).strip() or auth.header_prefix
    if choice == "body":
        updated.body_field = display.ri(
            f"  body field [dim](default: {auth.body_field})[/dim]:"
        ).strip() or auth.body_field
    if choice == "query":
        updated.query_field = display.ri(
            f"  query field [dim](default: {auth.query_field})[/dim]:"
        ).strip() or auth.query_field
    return updated


def _profile_setup_form(
    existing: Optional[app_config.BackendProfile] = None,
    *,
    allow_blank_name: bool = True,
) -> app_config.BackendProfile:
    base = existing or app_config.BackendProfile()
    display.rp("\n  [bold yellow]backend profile[/bold yellow]")
    raw_name = display.ri(f"  profile name [dim](default: {base.name})[/dim]:").strip()
    name = raw_name or base.name
    if not allow_blank_name and not name:
        name = "default"
    name = _slugify(name).replace("-", "_")
    system_name = display.ri(f"  system name [dim](default: {base.system_name})[/dim]:").strip() or base.system_name
    system_description = (
        display.ri("  system description [dim](what are you building or evaluating links for?)[/dim]:").strip()
        or base.system_description
    )
    integration_goal = (
        display.ri("  integration goal [dim](how should useful links turn into action?)[/dim]:").strip()
        or base.integration_goal
    )
    base_url = display.ri(
        f"  backend base URL [dim](blank disables remote analysis; default: {base.base_url or 'disabled'})[/dim]:"
    ).strip() or base.base_url
    health_path = display.ri(f"  health path [dim](default: {base.health_path})[/dim]:").strip() or base.health_path
    message_path = display.ri(f"  message path [dim](default: {base.message_path})[/dim]:").strip() or base.message_path
    create_task_path = display.ri(
        f"  task path [dim](default: {base.create_task_path})[/dim]:"
    ).strip() or base.create_task_path
    analysis_agent = display.ri(
        f"  analysis agent [dim](default: {base.agents.get('analysis', 'assistant')})[/dim]:"
    ).strip() or base.agents.get("analysis", "assistant")
    meta_agent = display.ri(
        f"  meta agent [dim](default: {base.agents.get('meta', 'assistant')})[/dim]:"
    ).strip() or base.agents.get("meta", "assistant")
    task_agent = display.ri(
        f"  task agent [dim](default: {base.agents.get('task', 'assistant')})[/dim]:"
    ).strip() or base.agents.get("task", "assistant")
    message_field = display.ri(f"  message field [dim](default: {base.message_field})[/dim]:").strip() or base.message_field
    timeout_field = display.ri(f"  timeout field [dim](default: {base.timeout_field})[/dim]:").strip() or base.timeout_field
    agent_field = display.ri(f"  agent field [dim](default: {base.agent_field})[/dim]:").strip() or base.agent_field
    task_text_field = display.ri(
        f"  task text field [dim](default: {base.task_text_field})[/dim]:"
    ).strip() or base.task_text_field
    task_priority_field = display.ri(
        f"  task priority field [dim](default: {base.task_priority_field})[/dim]:"
    ).strip() or base.task_priority_field
    task_notes_field = display.ri(
        f"  task notes field [dim](default: {base.task_notes_field})[/dim]:"
    ).strip() or base.task_notes_field
    response_text_paths = _prompt_paths("response text paths", base.response_text_paths)
    response_model_paths = _prompt_paths("response model paths", base.response_model_paths)
    task_id_paths = _prompt_paths("task id paths", base.task_id_paths)
    auth = _prompt_auth(base.auth)
    return app_config.BackendProfile(
        name=name,
        system_name=system_name,
        system_description=system_description,
        integration_goal=integration_goal,
        base_url=base_url,
        health_path=health_path,
        message_path=message_path,
        create_task_path=create_task_path,
        message_field=message_field,
        timeout_field=timeout_field,
        agent_field=agent_field,
        task_text_field=task_text_field,
        task_priority_field=task_priority_field,
        task_notes_field=task_notes_field,
        response_text_paths=response_text_paths,
        response_model_paths=response_model_paths,
        task_id_paths=task_id_paths,
        agents={"analysis": analysis_agent, "meta": meta_agent, "task": task_agent},
        auth=auth,
    )


def do_settings() -> None:
    while True:
        config = app_config.get_config(refresh=True)
        profile = app_config.get_active_profile(config)
        display.clr()
        display.rp(display.banner("settings"))
        display.rp(
            f"""  [bold yellow]configuration[/bold yellow]

  [dim]config file:[/dim]   {app_config.default_config_path()}
  [dim]data dir:[/dim]      {app_config.get_data_dir(config)}
  [dim]fetch mode:[/dim]    {config.fetch_mode}
  [dim]active profile:[/dim] {config.active_profile}
  [dim]system:[/dim]        {display.escape(profile.system_name)}
  [dim]backend:[/dim]       {display.escape(profile.base_url or 'disabled')}
  [dim]analysis agent:[/dim] {display.escape(profile.agents.get('analysis', 'assistant'))}
  [dim]meta agent:[/dim]    {display.escape(profile.agents.get('meta', 'assistant'))}
  [dim]task agent:[/dim]    {display.escape(profile.agents.get('task', 'assistant'))}

  [bold yellow][1][/bold yellow] edit active profile
  [bold yellow][2][/bold yellow] switch active profile
  [bold yellow][3][/bold yellow] add new profile
  [bold yellow][4][/bold yellow] remove a profile
  [bold yellow][5][/bold yellow] change data directory
  [bold yellow][6][/bold yellow] change fetch mode
  [bold yellow][7][/bold yellow] test backend connectivity
  [bold yellow][q][/bold yellow] back
"""
        )
        choice = (display.ri("  >") or "").strip().lower()
        if not choice or choice == "q":
            return

        if choice == "1":
            updated = _profile_setup_form(profile)
            config.profiles.pop(config.active_profile, None)
            config.profiles[updated.name] = updated
            config.active_profile = updated.name
            app_config.save_config(config)
            _refresh_runtime_caches()
            continue

        if choice == "2":
            names = list(config.profiles.keys())
            display.rp("\n  [bold yellow]profiles:[/bold yellow]")
            for idx, name in enumerate(names, 1):
                marker = "*" if name == config.active_profile else " "
                display.rp(f"  {marker} {idx}. {display.escape(name)}")
            raw = display.ri("  choose profile #:").strip()
            try:
                idx = int(raw) - 1
            except ValueError:
                continue
            if 0 <= idx < len(names):
                config.active_profile = names[idx]
                app_config.save_config(config)
                _refresh_runtime_caches()
            continue

        if choice == "3":
            updated = _profile_setup_form()
            config.profiles[updated.name] = updated
            config.active_profile = updated.name
            app_config.save_config(config)
            _refresh_runtime_caches()
            continue

        if choice == "4":
            names = list(config.profiles.keys())
            if len(names) <= 1:
                display.rp("  [red]at least one profile must remain[/red]")
                display.pause()
                continue
            display.rp("\n  [bold yellow]profiles:[/bold yellow]")
            for idx, name in enumerate(names, 1):
                display.rp(f"  {idx}. {display.escape(name)}")
            raw = display.ri("  remove profile #:").strip()
            try:
                idx = int(raw) - 1
            except ValueError:
                continue
            if 0 <= idx < len(names):
                removed = names[idx]
                config.profiles.pop(removed, None)
                if config.active_profile == removed:
                    config.active_profile = next(iter(config.profiles))
                app_config.save_config(config)
                _refresh_runtime_caches()
            continue

        if choice == "5":
            current = str(app_config.get_data_dir(config))
            value = display.ri(f"  data directory [dim](default/current: {current})[/dim]:").strip()
            config.data_dir = value or current
            app_config.save_config(config)
            _refresh_runtime_caches()
            continue

        if choice == "6":
            mode = display.ri(
                f"  fetch mode [dim](auto|http|stealth|dynamic; current: {config.fetch_mode})[/dim]:"
            ).strip().lower()
            if mode in ("auto", "http", "stealth", "dynamic"):
                config.fetch_mode = mode
                app_config.save_config(config)
                _refresh_runtime_caches()
            continue

        if choice == "7":
            client = backend.get_client(refresh=True)
            if not client.configured():
                display.rp("  [yellow]backend is disabled for the active profile[/yellow]")
            elif client.check_health():
                display.rp("  [green]backend looks reachable[/green]")
            else:
                display.rp("  [red]backend health check failed[/red]")
            display.pause()
            continue


def do_info() -> None:
    display.clr()
    display.rp(display.banner("info"))
    stats = storage.get_storage().get_stats()
    prefs = _prefs()
    config = app_config.get_config(refresh=True)
    profile = app_config.get_active_profile(config)
    client = backend.get_client(refresh=True)
    api_ok = client.check_health() if client.configured() else False
    log_entries = storage.get_storage().load_integration_log()
    display.rp(
        f"""  [bold yellow]tsundoku[/bold yellow] v{VERSION}

  [dim]config:[/dim]         {app_config.default_config_path()}
  [dim]data dir:[/dim]       {stats['storage_dir']}
  [dim]links file:[/dim]     {stats['links_file']}
  [dim]analysis dir:[/dim]   {stats['analyses_dir']}
  [dim]integration buf:[/dim] {stats['integ_buffer']}
  [dim]integration log:[/dim] {stats['integration_log']}  ({len(log_entries)} entries)
  [dim]prefs:[/dim]          {stats['prefs_file']}
  [dim]links:[/dim]          {stats['total_links']} total
  [dim]system:[/dim]         {display.escape(profile.system_name)}
  [dim]backend:[/dim]        {display.escape(profile.base_url or 'disabled')}  {'[green]online[/green]' if api_ok else '[red]offline[/red]'}
  [dim]analysis agent:[/dim] {display.escape(profile.agents.get('analysis', 'assistant'))}
  [dim]meta agent:[/dim]     {display.escape(profile.agents.get('meta', 'assistant'))}
  [dim]task agent:[/dim]     {display.escape(profile.agents.get('task', 'assistant'))}
  [dim]sort mode:[/dim]      {prefs.get('sort_mode')}
  [dim]fetch mode:[/dim]     {config.fetch_mode}

  [dim]status flow:[/dim]    unread -> analyzing -> analyzed -> (trial | implemented | done | archived)
  [dim]special:[/dim]        fetch_error is retained when fetch fails but analysis still completes
"""
    )
    display.pause()


MENU_ITEMS = [
    ("p", "&Paste URLs", "do_add"),
    ("l", "&List links", "do_list"),
    ("v", "&View detail", "do_view"),
    ("n", "A&nalyze selected", "do_analyze"),
    ("a", "&Analyze all", "do_analyze_all"),
    ("r", "&Reappraise incomplete", "do_reappraise"),
    ("i", "&Integrate", "do_integrate"),
    ("x", "Ar&chive", "do_archive"),
    ("m", "&Meta analysis", "do_meta"),
    ("s", "&Settings", "do_settings"),
    ("o", "Inf&o", "do_info"),
    ("q", "&Quit", None),
]

MENU_HINT = "↑ ↓ / j k move  ·  enter select"
SELECTED_BOX = "[#846136]▕[/][#a6753e]░[/][#ca934e]▒[/][#e6ae64]▓[/][#f6d58e]█[/][#846136]▏[/]"
IDLE_BOX = "[#60574c]▕    ▏[/]"
MENU_LABEL_WIDTH = max(len(label.replace("&", "")) for _, label, _ in MENU_ITEMS)


def _plain_menu_label(template: str) -> str:
    return template.replace("&", "")


def _styled_menu_label(template: str, selected: bool) -> str:
    marker = template.find("&")
    if marker == -1 or marker == len(template) - 1:
        plain = template.replace("&", "")
        style = "bold #f5e3b8" if selected else "#bfb5a5"
        return f"[{style}]{plain}[/]"
    before = display.escape(template[:marker])
    key = display.escape(template[marker + 1])
    after = display.escape(template[marker + 2 :])
    if selected:
        return f"[bold #f5e3b8]{before}[/][bold underline #fff4d8]{key}[/][bold #f5e3b8]{after}[/]"
    return f"[#bfb5a5]{before}[/][underline #ece2cf]{key}[/][#bfb5a5]{after}[/]"


def _menu_row(idx: int, selected: int) -> tuple[str, str]:
    _, template, _ = MENU_ITEMS[idx]
    label_plain = _plain_menu_label(template)
    label_markup = _styled_menu_label(template, idx == selected)
    padding = " " * (MENU_LABEL_WIDTH - len(label_plain))
    indicator_markup = SELECTED_BOX if idx == selected else IDLE_BOX
    indicator_plain = "▕░▒▓█▏" if idx == selected else "▕    ▏"
    return (
        f"{indicator_markup}  {label_markup}{padding}",
        f"{indicator_plain}  {label_plain}{padding}",
    )


def _draw_menu(selected: int, subtitle: str) -> None:
    display.clr()
    display.rp(display.banner(subtitle))
    display.rp("")
    for idx in range(len(MENU_ITEMS)):
        row_markup, row_plain = _menu_row(idx, selected)
        display.rp(display.centered(row_markup, row_plain))
    display.rp("")
    display.rp(display.centered(f"[dim]{MENU_HINT}[/dim]", MENU_HINT))


def _run_menu_item(func_name: Optional[str]) -> None:
    if func_name:
        globals()[func_name]()


def _handle_cli_args() -> None:
    cmd = sys.argv[1].lower()
    if cmd in ("add", "p") and len(sys.argv) > 2:
        storage_mgr = storage.get_storage()
        links = storage_mgr.load_links()
        existing = {link["url"] for link in links}
        added = 0
        for raw in sys.argv[2:]:
            for url in extract_urls(raw) or ([raw] if raw.startswith("http") else []):
                if url in existing:
                    continue
                links.append(models.create_link(url).to_dict())
                existing.add(url)
                added += 1
        storage_mgr.save_links(links)
        print(f"tsundoku: added {added} link(s)")
        return
    if cmd in ("list", "l"):
        links = _sort_links(storage.get_storage().load_links(), models.SortMode.DATE_DESC.value)
        for link in links:
            print(f"{link['id']}  {link['status']:12}  {_get_relevance(link):3}  {link['url']}")
        return
    if cmd in ("analyze", "analyse", "n"):
        do_analyze(sys.argv[2] if len(sys.argv) > 2 else None)
        return
    if cmd in ("analyze-all", "analyze_all", "all", "a"):
        do_analyze_all()
        return
    if cmd in ("reappraise", "retry", "r"):
        do_reappraise()
        return
    if cmd in ("view", "v"):
        do_view(sys.argv[2] if len(sys.argv) > 2 else None)
        return
    if cmd in ("integrate", "i"):
        do_integrate(sys.argv[2] if len(sys.argv) > 2 else None)
        return
    if cmd in ("archive", "x"):
        do_archive()
        return
    if cmd in ("meta", "m"):
        do_meta()
        return
    if cmd in ("settings", "config", "s"):
        do_settings()
        return
    if cmd in ("help", "info", "o", "?"):
        do_info()
        return
    print(f"tsundoku v{VERSION}")
    print("usage: tsundoku [command]")
    print("commands: add, list, analyze, analyze-all, reappraise, view, integrate, archive, meta, settings, info")


def _run_interactive() -> None:
    selected = 0
    while True:
        links = storage.get_storage().load_links()
        active_links = [link for link in links if link.get("status") != models.LinkStatus.ARCHIVED.value]
        unread = sum(1 for link in active_links if link.get("status") == models.LinkStatus.UNREAD.value)
        analyzed = sum(
            1
            for link in active_links
            if link.get("status") in (
                models.LinkStatus.ANALYZED.value,
                models.LinkStatus.TRIAL.value,
                models.LinkStatus.IMPLEMENTED.value,
                models.LinkStatus.DONE.value,
            )
        )
        subtitle = f"v{VERSION}  ·  links {len(links)}  ·  unread {unread}  ·  analyzed {analyzed}"
        _draw_menu(selected, subtitle)

        key = display._read_key()
        if key in ("UP", "k"):
            selected = (selected - 1) % len(MENU_ITEMS)
            continue
        if key in ("DOWN", "j"):
            selected = (selected + 1) % len(MENU_ITEMS)
            continue
        if key in ("\r", "\n", " "):
            _, _, func_name = MENU_ITEMS[selected]
            if func_name is None:
                return
            _run_menu_item(func_name)
            continue
        if key in ("CTRL_C", "CTRL_D"):
            return
        if key == "ESC":
            continue

        matched = False
        for idx, (hotkey, _label, func_name) in enumerate(MENU_ITEMS):
            if key.lower() != hotkey:
                continue
            selected = idx
            matched = True
            if func_name is None:
                return
            _run_menu_item(func_name)
            break

        if not matched and not display.HAS_RAW_INPUT and key:
            display.rp("  [red]unknown command[/red]")
            display.pause()


def main() -> None:
    _ensure_setup(interactive=True)
    if len(sys.argv) > 1:
        _handle_cli_args()
        return
    _run_interactive()


__all__ = [
    "do_add",
    "do_list",
    "do_view",
    "do_analyze",
    "do_analyze_all",
    "do_reappraise",
    "do_integrate",
    "do_archive",
    "do_meta",
    "do_settings",
    "do_info",
    "main",
    "MENU_ITEMS",
    "_analysis_missing_fields",
]
