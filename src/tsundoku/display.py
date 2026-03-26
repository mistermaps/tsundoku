#!/usr/bin/env python3
"""
tsundoku - Display Module
=====================
Rich terminal UI with pagination, vim keybindings, and responsive design.

Features:
- Pagination for large lists
- Vim-style navigation (j/k/h/l/gg/G)
- Search/filter functionality
- Responsive banner that respects terminal width
- Help system
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional, Callable, Any

from . import __version__

# Try to import Rich, fall back to plain text
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.markup import escape
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
escape = lambda s: str(s)

try:
    import termios
    import tty
    import select
    HAS_RAW_INPUT = True
except ImportError:
    HAS_RAW_INPUT = False

# ============================================================================
# CONFIGURATION
# ============================================================================

VERSION = __version__

LOGO_LINES = [
    "@@@@@@@   @@@@@@   @@@  @@@  @@@  @@@  @@@@@@@    @@@@@@   @@@  @@@  @@@  @@@",
    "@@@@@@@  @@@@@@@   @@@  @@@  @@@@ @@@  @@@@@@@@  @@@@@@@@  @@@  @@@  @@@  @@@",
    "  @@!    !@@       @@!  @@@  @@!@!@@@  @@!  @@@  @@!  @@@  @@!  !@@  @@!  @@@",
    "  !@!    !@!       !@!  @!@  !@!!@!@!  !@!  @!@  !@!  @!@  !@!  @!!  !@!  @!@",
    "  @!!    !!@@!!    @!@  !@!  @!@ !!@!  @!@  !@!  @!@  !@!  @!@@!@!   @!@  !@!",
    "  !!!     !!@!!!   !@!  !!!  !@!  !!!  !@!  !!!  !@!  !!!  !!@!!!    !@!  !!!",
    "  !!:         !:!  !!:  !!!  !!:  !!!  !!:  !!!  !!:  !!!  !!: :!!   !!:  !!!",
    "  :!:        !:!   :!:  !:!  :!:  !:!  :!:  !:!  :!:  !:!  :!:  !:!  :!:  !:!",
    "   ::    :::: ::   ::::: ::   ::   ::   :::: ::  ::::: ::   ::  :::  ::::: ::",
    "   :     :: : :     : :  :   ::    :   :: :  :    : :  :    :   :::   : :  :",
]

TAGLINES = [
    "read later, act sooner",
    "capture links and turn them into plans",
    "public release by cassette, aka maps",
]

# Color schemes
SRC_LABEL = {
    "twitter":     "[bold cyan][X][/bold cyan]",
    "github":      "[bold white][GH][/bold white]",
    "arxiv":       "[bold magenta][Ar][/bold magenta]",
    "youtube":     "[bold red][YT][/bold red]",
    "reddit":      "[bold red][Rd][/bold red]",
    "huggingface": "[bold yellow][HF][/bold yellow]",
    "bluesky":     "[bold cyan][BS][/bold cyan]",
    "web":         "[dim][wb][/dim]",
}

STATUS_LABEL = {
    "unread": "[dim]○ unread[/dim]",
    "analyzing": "[yellow]◌ analyzing[/yellow]",
    "analyzed": "[cyan]◉ analyzed[/cyan]",
    "done": "[green]● done[/green]",
    "trial": "[magenta]◈ trial[/magenta]",
    "implemented": "[green bold]✦ implemented[/green bold]",
    "archived": "[dim strikethrough]⊘ archived[/dim strikethrough]",
    "fetch_error": "[red]✗ fetch error[/red]",
}

_PENDING_ESCAPE = False
_PENDING_ESCAPE_PREFIX = ""
_TRANSIENT_STATUS_WIDTH = 0

# ============================================================================
# KEYBOARD INPUT
# ============================================================================

def _read_key() -> str:
    """Read a single keypress with full escape sequence support."""
    if not HAS_RAW_INPUT or not sys.stdin.isatty():
        line = input().strip()
        return line[:1] if line else ""

    def _read_escape_suffix(fd: int, first_timeout: float = 0.15, tail_timeout: float = 0.01) -> str:
        """Read the remaining bytes of an escape sequence without blocking forever."""
        suffix = ""
        deadline = time.monotonic() + first_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not select.select([sys.stdin], [], [], remaining)[0]:
                break
            chunk = os.read(fd, 16).decode(errors="ignore")
            if not chunk:
                break
            suffix += chunk
            deadline = time.monotonic() + tail_timeout
        return suffix

    def _decode_escape_sequence(seq: str) -> str:
        """Map CSI/SS3 arrow suffixes to logical keys."""
        if seq.startswith("[A") or seq.startswith("OA"):
            return "UP"
        if seq.startswith("[B") or seq.startswith("OB"):
            return "DOWN"
        if seq.startswith("[C") or seq.startswith("OC"):
            return "RIGHT"
        if seq.startswith("[D") or seq.startswith("OD"):
            return "LEFT"
        return ""

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        global _PENDING_ESCAPE, _PENDING_ESCAPE_PREFIX

        if _PENDING_ESCAPE_PREFIX:
            seq = _PENDING_ESCAPE_PREFIX + ch
            _PENDING_ESCAPE_PREFIX = ""
            decoded = _decode_escape_sequence(seq)
            if decoded:
                return decoded
            if seq in {"[", "O"}:
                _PENDING_ESCAPE_PREFIX = seq
            return ""

        if _PENDING_ESCAPE:
            _PENDING_ESCAPE = False
            seq = ch
            if ch in {"[", "O"}:
                seq += _read_escape_suffix(fd, first_timeout=0.05, tail_timeout=0.01)
            decoded = _decode_escape_sequence(seq)
            if decoded:
                return decoded
            if seq in {"[", "O"}:
                _PENDING_ESCAPE_PREFIX = seq
            return ""

        if ch == "\x1b":
            suffix = _read_escape_suffix(fd, first_timeout=0.15, tail_timeout=0.01)
            decoded = _decode_escape_sequence(suffix)
            if decoded:
                return decoded
            if suffix in {"[", "O"}:
                _PENDING_ESCAPE_PREFIX = suffix
                return ""
            if not suffix:
                _PENDING_ESCAPE = True
                return ""
            return ""
        if ch == "\x03":
            return "CTRL_C"
        if ch == "\x04":
            return "CTRL_D"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ============================================================================
# CONSOLE SETUP
# ============================================================================

class Display:
    """Terminal display handler with Rich formatting and fallbacks."""
    
    def __init__(self):
        self.console = Console(highlight=False, force_terminal=True) if HAS_RICH else None
        self._width = self._get_terminal_width()
    
    def _get_terminal_width(self) -> int:
        """Get terminal width, defaulting to 80 if not detectable."""
        try:
            return os.get_terminal_size().columns
        except (OSError, AttributeError):
            return 80
    
    @property
    def width(self) -> int:
        """Current terminal width."""
        return self._width
    
    def print(self, *args, **kwargs):
        """Print to console."""
        if self.console:
            self.console.print(*args, **kwargs)
        else:
            # Plain text fallback
            text = str(args[0]) if args else ""
            text = re.sub(r'\[/?[^\]]+\]', '', text)
            text = text.replace(r"\[", "[").replace(r"\]", "]")
            print(text, **kwargs)
    
    def clear(self):
        """Clear screen."""
        os.system("cls" if os.name == "nt" else "clear")
    
    def centered(self, text: str, plain_text: Optional[str] = None) -> str:
        """Center a Rich markup string using its plain-text width."""
        plain = plain_text if plain_text is not None else re.sub(r'\[/?[^\]]+\]', '', text)
        plain = plain.replace(r"\[", "[").replace(r"\]", "]")
        pad = max(0, (self.width - len(plain)) // 2)
        return f"{' ' * pad}{text}"

    def banner(self, subtitle: str = "") -> str:
        """Generate the shared tsundoku banner."""
        logo_lines = LOGO_LINES if self.width >= 92 else ["TSUNDOKU"]
        result = [
            self.centered(f"[bold #f3c97a]{line}[/]", line)
            for line in logo_lines
        ]

        result.append("")
        tagline_styles = [
            "[bold #f5e8c7]{text}[/]",
            "[#dbc59a]{text}[/]",
            "[dim #b8a68b]{text}[/]",
        ]
        for text, style in zip(TAGLINES, tagline_styles):
            result.append(self.centered(style.format(text=text), text))

        if subtitle:
            result.append("")
            result.append(self.centered(f"[dim]{subtitle}[/dim]", subtitle))

        return "\n".join(result)
    
    def show_header(self, subtitle: str = ""):
        """Show header banner."""
        self.clear()
        self.print(self.banner(subtitle))
        self.print("")


# Create global display instance
_display: Optional[Display] = None

def get_display() -> Display:
    """Get or create display instance."""
    global _display
    if _display is None:
        _display = Display()
    return _display


def rp(*args, **kwargs):
    """Rich print wrapper."""
    get_display().print(*args, **kwargs)


def clr():
    """Clear screen."""
    get_display().clear()


def banner(subtitle: str = "") -> str:
    """Generate the shared tsundoku banner."""
    return get_display().banner(subtitle)


def centered(text: str, plain_text: Optional[str] = None) -> str:
    """Center a Rich markup string using a provided plain-text representation."""
    return get_display().centered(text, plain_text)


def _plain_text(text: str) -> str:
    """Strip Rich markup for direct terminal writes."""
    plain = re.sub(r'\[/?[^\]]+\]', '', str(text))
    return plain.replace(r"\[", "[").replace(r"\]", "]")


def transient_status(text: str):
    """Draw a single-line transient status message without scrolling the terminal."""
    global _TRANSIENT_STATUS_WIDTH
    plain = _plain_text(text)
    if not sys.stdout.isatty():
        rp(plain)
        return

    _TRANSIENT_STATUS_WIDTH = max(_TRANSIENT_STATUS_WIDTH, len(plain))
    padded = plain.ljust(_TRANSIENT_STATUS_WIDTH)
    sys.stdout.write("\r\033[2K" + padded)
    sys.stdout.flush()


def clear_transient_status():
    """Clear the active transient status line, if any."""
    global _TRANSIENT_STATUS_WIDTH
    if not sys.stdout.isatty():
        return
    if _TRANSIENT_STATUS_WIDTH:
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()
    _TRANSIENT_STATUS_WIDTH = 0


# ============================================================================
# PAGINATED TABLE
# ============================================================================

@dataclass
class Paginator:
    """Paginated list display with vim-style navigation."""
    
    items: list[Any]
    page_size: int = 20
    current_page: int = 0
    filter_fn: Optional[Callable[[Any], bool]] = None
    
    @property
    def filtered_items(self) -> list:
        """Get filtered items."""
        if self.filter_fn:
            return [i for i in self.items if self.filter_fn(i)]
        return self.items
    
    @property
    def total_pages(self) -> int:
        """Total number of pages."""
        items = self.filtered_items
        return max(1, (len(items) + self.page_size - 1) // self.page_size)
    
    @property
    def current_page_items(self) -> list:
        """Items on current page."""
        items = self.filtered_items
        start = self.current_page * self.page_size
        end = start + self.page_size
        return items[start:end]
    
    def next_page(self):
        """Go to next page."""
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
    
    def prev_page(self):
        """Go to previous page."""
        if self.current_page > 0:
            self.current_page -= 1
    
    def first_page(self):
        """Go to first page."""
        self.current_page = 0
    
    def last_page(self):
        """Go to last page."""
        self.current_page = self.total_pages - 1
    
    def set_filter(self, fn: Optional[Callable[[Any], bool]]):
        """Set filter function."""
        self.filter_fn = fn
        self.current_page = 0  # Reset to first page
    
    def handle_key(self, key: str) -> bool:
        """Handle a keypress. Returns True if quit."""
        if key in ('q', 'Q', 'esc'):
            return True
        elif key in ('j', 'down', '\n'):
            self.next_page()
        elif key in ('k', 'up'):
            self.prev_page()
        elif key in ('g', 'gg'):
            self.first_page()
        elif key == 'G':
            self.last_page()
        return False


# ============================================================================
# INPUT HELPERS
# ============================================================================

def ri(prompt: str) -> str:
    """Prompt for input."""
    rp(f"[bold yellow]{escape(prompt)}[/bold yellow]", end=" ")
    try:
        return input().strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)


def pause(msg: str = "  ↩  enter to continue"):
    """Wait for user input."""
    rp(f"[dim]{msg}[/dim]", end="")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)


# ============================================================================
# TABLE DISPLAY
# ============================================================================

def _terminal_rows(default: int = 24) -> int:
    """Best-effort terminal height."""
    try:
        return os.get_terminal_size().lines
    except OSError:
        return default


def _analysis_score(link: dict) -> int:
    """Extract relevance score from a link's analysis payload."""
    analysis = link.get("analysis")
    if not analysis:
        return 0
    try:
        if isinstance(analysis, str):
            analysis = json.loads(analysis)
        return int((analysis or {}).get("relevance_score", 0) or 0)
    except Exception:
        return 0


def _render_links_table(
    links: list,
    page: int = 0,
    page_size: int = 20,
    selected_indices: Optional[set[int]] = None,
    cursor_index: Optional[int] = None,
    show_checkbox: bool = False,
):
    """Render a page of links."""
    total = len(links)
    start = page * page_size
    end = min(start + page_size, total)
    page_links = links[start:end]

    if HAS_RICH:
        table = Table(
            show_header=True,
            header_style="bold yellow",
            border_style="dim",
            box=None,
            padding=(0, 1),
        )
        if show_checkbox:
            table.add_column("", no_wrap=True, width=3)
        table.add_column("#", style="dim", width=4)
        table.add_column("src", no_wrap=True, width=6)
        table.add_column("status", no_wrap=True, width=16)
        table.add_column("id", style="dim cyan", width=12)
        table.add_column("rel", no_wrap=True, width=10)
        table.add_column("url", min_width=28, max_width=50)
        table.add_column("title", style="dim", min_width=14, max_width=26)

        for absolute_index, link in enumerate(page_links, start):
            row_number = absolute_index + 1
            source_label = SRC_LABEL.get(link.get("source_type", "web"), "[dim][wb][/dim]")
            if link.get("is_thread"):
                source_label = f"{source_label}[dim][T][/dim]"

            status_label = STATUS_LABEL.get(link.get("status", ""), escape(link.get("status", "")))
            if link.get("fetch_error"):
                status_label = f"[red]![/red] {status_label}"

            url = link.get("url", "")
            if len(url) > 48:
                url = url[:45] + "..."

            title = (link.get("title") or "")[:24]
            score = _analysis_score(link)
            rel_bar = relevance_bar(score) if score else ""

            style = "reverse" if cursor_index == absolute_index else ""
            row_values = []
            if show_checkbox:
                mark = "☑" if selected_indices and absolute_index in selected_indices else "☐"
                row_values.append(mark)
            row_values.extend(
                [
                    str(row_number),
                    source_label,
                    status_label,
                    link.get("id", ""),
                    rel_bar,
                    escape(url),
                    escape(title),
                ]
            )
            table.add_row(*row_values, style=style)

        get_display().console.print(table)
    else:
        header = f"{'#':4} {'src':6} {'status':16} {'id':12} {'rel':7} url"
        if show_checkbox:
            header = f"{'sel':4} " + header
        print(header)
        print("-" * min(len(header), 100))
        for absolute_index, link in enumerate(page_links, start):
            url = link.get("url", "")
            if len(url) > 50:
                url = url[:47] + "..."
            source = link.get("source_type", "web")[:6]
            if link.get("is_thread"):
                source = f"{source[:4]}[T]"
            status = link.get("status", "")
            if link.get("fetch_error"):
                status = f"!{status}"
            prefix = ""
            if show_checkbox:
                mark = "[x]" if selected_indices and absolute_index in selected_indices else "[ ]"
                prefix = f"{mark:4} "
            print(
                f"{prefix}{absolute_index + 1:<4} {source:6} {status[:16]:16} "
                f"{link.get('id', '')[:12]:12} {_analysis_score(link):<7} {url}"
            )

    if total > page_size:
        rp(
            f"\n  [dim]showing {start + 1}-{end} of {total}  "
            f"[page {page + 1}/{max(1, (total + page_size - 1) // page_size)}][/dim]"
        )


def show_table(links: list, page: int = 0, page_size: int = 20):
    """Show paginated table of links."""
    if not links:
        rp("  [dim](no links)[/dim]")
        return
    _render_links_table(links, page=page, page_size=page_size)


def show_selectable_table(links: list, prompt: str = "select links", page_size: Optional[int] = None) -> list[dict]:
    """Interactively select one or more links."""
    if not links:
        rp("  [dim](no links)[/dim]")
        return []

    page_size = page_size or max(10, _terminal_rows() - 12)

    if not HAS_RAW_INPUT or not sys.stdin.isatty():
        show_table(links, page=0, page_size=page_size)
        rp("")
        rp("  [dim]numeric selection: 1,3,5-8[/dim]")
        selection = ri(f"  {prompt}:")
        if not selection:
            return []
        try:
            from . import models
            indices = models.parse_numeric_selection(selection, len(links))
        except Exception:
            indices = []
        return [links[i] for i in indices]

    selected: set[int] = set()
    cursor = 0
    page = 0

    while True:
        total_pages = max(1, (len(links) + page_size - 1) // page_size)
        page = min(max(page, 0), total_pages - 1)
        start = page * page_size
        end = min(start + page_size, len(links))
        if cursor < start:
            cursor = start
        elif cursor >= end:
            cursor = max(start, end - 1)

        clr()
        rp(banner(prompt))
        rp("")
        _render_links_table(
            links,
            page=page,
            page_size=page_size,
            selected_indices=selected,
            cursor_index=cursor,
            show_checkbox=True,
        )
        rp("")
        rp(
            "  [dim]j/k move  h/l or n/p page  g/gg top  G bottom  "
            "space toggle  enter confirm  : numeric select  q cancel[/dim]"
        )

        key = _read_key()
        if key in ("UP", "k"):
            cursor = max(0, cursor - 1)
        elif key in ("DOWN", "j"):
            cursor = min(len(links) - 1, cursor + 1)
        elif key in ("LEFT", "h", "p"):
            page = max(0, page - 1)
        elif key in ("RIGHT", "l", "n"):
            page = min(total_pages - 1, page + 1)
        elif key in ("g",):
            page = 0
            cursor = 0
        elif key == "G":
            page = total_pages - 1
            cursor = len(links) - 1
        elif key == " ":
            if cursor in selected:
                selected.remove(cursor)
            else:
                selected.add(cursor)
        elif key == ":":
            rp("")
            selection = ri("  numbers:")
            if not selection:
                continue
            try:
                from . import models
                selected = set(models.parse_numeric_selection(selection, len(links)))
                if selected:
                    cursor = min(selected)
            except Exception:
                pass
        elif key in ("\r", "\n"):
            if not selected:
                selected.add(cursor)
            return [links[i] for i in sorted(selected)]
        elif key == "q":
            return []
        elif key in ("CTRL_C", "CTRL_D"):
            return []

        if cursor < start:
            page = cursor // page_size
        elif cursor >= end:
            page = cursor // page_size


# ============================================================================
# RELEVANCE BAR
# ============================================================================

def relevance_bar(score: int, max_score: int = 5) -> str:
    """Amber-colored relevance bar."""
    score = max(0, min(score, max_score))
    filled = "█" * score
    empty = "░" * (max_score - score)
    color = "green" if score >= 4 else ("yellow" if score >= 2 else "red")
    return f"[{color}]{filled}[/{color}][dim]{empty}[/dim]  {score}/{max_score}"


# ============================================================================
# HELP SYSTEM
# ============================================================================

HELP_TEXT = """
[bold yellow]Keyboard Shortcuts[/bold yellow]

[bold]Navigation:[/bold]
  j/k or ↓/↑     Next/previous item
  h/l or ←/→     Previous/next page
  g / gg         Go to first
  G               Go to last
  /               Search/filter
  n               Next search result
  N               Previous search result

[bold]Actions:[/bold]
  enter           Select/view item
  space           Toggle selection
  d               Delete selected
  a               Add new
  e               Edit
  q               Quit

[bold]Tips:[/bold]
  - Type / followed by search term to filter
  - Press numbers 1-9 for quick actions
  - ESC cancels current operation
"""

def show_help():
    """Display help screen."""
    rp("")
    if HAS_RICH:
        get_display().console.print(Panel(
            HELP_TEXT,
            title="[bold yellow]tsundoku help[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        ))
    else:
        print(HELP_TEXT)
    pause()


# ============================================================================
# SEARCH
# ============================================================================

class Searcher:
    """Incremental search/filter."""
    
    def __init__(self, items: list):
        self.items = items
        self.query: str = ""
        self.results: list = []
        self.current_result: int = 0
    
    def search(self, query: str) -> list:
        """Search items by query."""
        self.query = query
        if not query:
            self.results = []
            return self.items
        
        query_lower = query.lower()
        self.results = [
            item for item in self.items
            if self._matches(item, query_lower)
        ]
        self.current_result = 0
        return self.results
    
    def _matches(self, item: dict, query: str) -> bool:
        """Check if item matches query."""
        text = " ".join(str(v) for v in item.values()).lower()
        return query in text
    
    def next_result(self):
        """Go to next result."""
        if self.results:
            self.current_result = (self.current_result + 1) % len(self.results)
    
    def prev_result(self):
        """Go to previous result."""
        if self.results:
            self.current_result = (self.current_result - 1) % len(self.results)


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    'Display', 'get_display', 'rp', 'clr', 'banner', 'centered',
    'Paginator', 'Searcher', 'show_table', 'show_selectable_table',
    'relevance_bar', 'show_help', 'ri', 'pause', 'VERSION',
    '_read_key', 'HAS_RAW_INPUT', 'escape', 'transient_status',
    'clear_transient_status',
]
