#!/usr/bin/env python3
"""Portable URL fetching helpers used by tsundoku."""

from __future__ import annotations

import argparse
from html import unescape
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Optional
from urllib.request import Request, urlopen


STEALTH_DOMAINS = {
    "twitter.com",
    "x.com",
    "t.co",
    "instagram.com",
    "threads.net",
    "tiktok.com",
    "linkedin.com",
    "facebook.com",
    "fb.com",
}

MAX_TEXT_LEN = 12_000
TIMEOUT = 30
TIMEOUT_STEALTH = 45
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)
_PLAYWRIGHT_EXECUTABLE_PATTERNS = (
    "**/chrome-linux/chrome",
    "**/chromium-*/chrome-linux/chrome",
    "**/headless_shell",
    "**/chromium",
)


def _domain(url: str) -> str:
    match = re.search(r"https?://(?:www\.)?([^/]+)", url.lower())
    return match.group(1) if match else ""


def _pick_mode(url: str) -> str:
    domain = _domain(url)
    for candidate in STEALTH_DOMAINS:
        if domain == candidate or domain.endswith("." + candidate):
            return "stealth"
    return "http"


def _truncate(text: str, max_len: int = MAX_TEXT_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n\n[... truncated at {max_len} chars ...]"


def _playwright_browser_available() -> bool:
    browsers_root = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        or (Path.home() / ".cache" / "ms-playwright")
    )
    if not browsers_root.exists():
        return False
    for pattern in _PLAYWRIGHT_EXECUTABLE_PATTERNS:
        for candidate in browsers_root.glob(pattern):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return True
    return False


def _result(
    url: str,
    mode: str,
    ok: bool,
    *,
    text: str = "",
    title: str = "",
    links: Optional[list[str]] = None,
    error: str = "",
    elapsed: float = 0.0,
) -> dict:
    return {
        "url": url,
        "mode": mode,
        "ok": ok,
        "title": title,
        "text": text,
        "links": links or [],
        "error": error,
        "elapsed": round(elapsed, 2),
        "chars": len(text),
    }


def _browser_unavailable_result(url: str, mode: str) -> dict:
    return _result(
        url,
        mode,
        False,
        error="browser fetch unavailable on this host (no Playwright browser executable)",
    )


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|section|article|main|li|h[1-6])>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = unescape(html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return _truncate(html.strip())


def _extract_links_from_html(html: str) -> list[str]:
    found = re.findall(r'href=["\'](https?://[^"\']+)["\']', html, re.IGNORECASE)
    return list(dict.fromkeys(found))[:30]


def _extract_title_from_html(html: str) -> str:
    match = re.search(r"(?is)<title>(.*?)</title>", html)
    if not match:
        return ""
    return _truncate(unescape(match.group(1)).strip(), 200)


def _fetch_http_stdlib(url: str) -> dict:
    started = time.time()
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
        return _result(
            url,
            "http",
            True,
            text=_strip_html(raw),
            title=_extract_title_from_html(raw),
            links=_extract_links_from_html(raw),
            elapsed=time.time() - started,
        )
    except Exception as exc:
        return _result(url, "http", False, error=str(exc), elapsed=time.time() - started)


def _fetch_http(url: str) -> dict:
    try:
        from scrapling.fetchers import Fetcher
    except Exception:
        return _fetch_http_stdlib(url)

    started = time.time()
    try:
        page = Fetcher.get(url, timeout=TIMEOUT, stealthy_headers=True)
        text = []
        for selector in ("main", "article", "[role='main']", ".content", "body"):
            nodes = page.css(selector)
            if nodes:
                text = [item.strip() for item in nodes[0].css("*::text").getall() if item.strip()]
                if text:
                    break
        if not text:
            text = [item.strip() for item in page.css("body *::text").getall() if item.strip()]
        return _result(
            url,
            "http",
            True,
            text=_truncate("\n".join(text)),
            title=(page.css("title::text").get() or "").strip()[:200],
            links=[href for href in page.css("a::attr(href)").getall() if href.startswith("http")][:30],
            elapsed=time.time() - started,
        )
    except Exception as exc:
        return _result(url, "http", False, error=str(exc), elapsed=time.time() - started)


def _fetch_stealth(url: str) -> dict:
    try:
        from scrapling.fetchers import StealthyFetcher
    except Exception:
        return _browser_unavailable_result(url, "stealth")

    started = time.time()
    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            solve_cloudflare=True,
            timeout=TIMEOUT_STEALTH * 1000,
            network_idle=True,
        )
        text = [item.strip() for item in page.css("body *::text").getall() if item.strip()]
        return _result(
            url,
            "stealth",
            True,
            text=_truncate("\n".join(text)),
            title=(page.css("title::text").get() or "").strip()[:200],
            links=[href for href in page.css("a::attr(href)").getall() if href.startswith("http")][:30],
            elapsed=time.time() - started,
        )
    except Exception as exc:
        return _result(url, "stealth", False, error=str(exc), elapsed=time.time() - started)


def _fetch_dynamic(url: str) -> dict:
    try:
        from scrapling.fetchers import DynamicFetcher
    except Exception:
        return _browser_unavailable_result(url, "dynamic")

    started = time.time()
    try:
        page = DynamicFetcher.fetch(
            url,
            headless=True,
            timeout=TIMEOUT_STEALTH * 1000,
            network_idle=True,
        )
        text = [item.strip() for item in page.css("body *::text").getall() if item.strip()]
        return _result(
            url,
            "dynamic",
            True,
            text=_truncate("\n".join(text)),
            title=(page.css("title::text").get() or "").strip()[:200],
            links=[href for href in page.css("a::attr(href)").getall() if href.startswith("http")][:30],
            elapsed=time.time() - started,
        )
    except Exception as exc:
        return _result(url, "dynamic", False, error=str(exc), elapsed=time.time() - started)


def fetch_url(url: str, mode: str = "auto") -> dict:
    """Fetch a URL and return normalized content metadata."""
    if mode == "auto":
        mode = _pick_mode(url)
    if mode == "http":
        result = _fetch_http(url)
        if not result["ok"] and _playwright_browser_available():
            return _fetch_stealth(url)
        return result
    if mode == "stealth":
        if not _playwright_browser_available():
            return _browser_unavailable_result(url, mode)
        return _fetch_stealth(url)
    if mode == "dynamic":
        if not _playwright_browser_available():
            return _browser_unavailable_result(url, mode)
        return _fetch_dynamic(url)
    return _result(url, mode, False, error=f"unknown mode: {mode}")


def fetch_urls(urls: list[str], mode: str = "auto") -> list[dict]:
    """Fetch multiple URLs sequentially."""
    return [fetch_url(url, mode) for url in urls]


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Fetch a URL and emit content as JSON.")
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument("--mode", default="auto", choices=["auto", "http", "stealth", "dynamic"])
    parser.add_argument("--text-only", action="store_true")
    args = parser.parse_args()

    result = fetch_url(args.url, args.mode)
    if args.text_only:
        if result["ok"]:
            if result.get("title"):
                print(f"TITLE: {result['title']}\n")
            print(result["text"])
            return 0
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI helper
    raise SystemExit(_cli())
