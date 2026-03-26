# tsundoku

`tsundoku` is a terminal-first read-it-later tool for engineers, operators, and agent-heavy workflows. It stores links locally, fetches and summarizes them, lets you stage integration ideas, and can talk to a user-configured HTTP JSON backend for analysis and task creation.

This 1.0 release is intentionally public and machine-neutral. It does not assume a specific host layout, credential file, install prefix, or backend product. Users configure their own system context and agent/backend profile from the built-in settings menu.

Created by cassette, aka maps  
https://cassette.help

## Highlights

- Public Python package with a `tsundoku` console command
- OS-aware config and data directories, with override env vars
- Interactive first-run setup and editable settings menu
- Profile-based backend configuration for custom agent systems
- Local storage for links, analyses, staged tasks, and logs
- Plain-stdlib fetching by default, with optional richer fetch behavior if `scrapling` is already installed
- Rich terminal UI when `rich` is available, with a plain-text fallback otherwise

## Installation

With `pipx`:

```bash
pipx install .
```

With `pip`:

```bash
python3 -m pip install .
```

From source during development:

```bash
PYTHONPATH=src python3 -m tsundoku
```

## Build

Build release artifacts into `dist/`:

```bash
./build.sh
```

Or directly:

```bash
python3 -m build
```

## First Run

The first launch opens a setup flow that asks for:

- your system name
- a short description of what you are building or evaluating links for
- an integration goal
- backend base URL and endpoint paths, if you want remote analysis/task creation
- default agent names for analysis, meta-analysis, and task creation
- authentication mode and env var name, if required

You can reopen and edit this configuration any time from `Settings`.

## Configuration Paths

By default `tsundoku` uses OS-standard locations:

- Linux: `~/.config/tsundoku` and `~/.local/share/tsundoku`
- macOS: `~/Library/Application Support/tsundoku`
- Windows: `%APPDATA%\\tsundoku` and `%LOCALAPPDATA%\\tsundoku`

For portable setups or tests, you can override them:

```bash
export TSUNDOKU_CONFIG_HOME=/path/to/config-root
export TSUNDOKU_DATA_HOME=/path/to/data-root
```

`tsundoku` will create a `tsundoku/` subdirectory under each override root.

## Backends

`tsundoku` does not ship with a product-specific backend. Instead, it can be configured for any HTTP JSON system that can:

- accept a message prompt for analysis
- optionally create a task or backlog item

That makes it suitable for custom in-house tools, agent routers, and deployments such as OpenClaw, Hermes-based systems, or other compatible APIs, without hardcoding any of them into the package.

## Commands

```text
tsundoku add <url...>
tsundoku list
tsundoku analyze [link-id]
tsundoku analyze-all
tsundoku reappraise
tsundoku view [link-id]
tsundoku integrate [link-id]
tsundoku archive
tsundoku meta
tsundoku settings
tsundoku info
```

Launching `tsundoku` with no arguments opens the interactive menu.

## License

Released under the MIT License. See `LICENSE`.
