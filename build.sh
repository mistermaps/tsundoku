#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT_DIR"
rm -rf dist build

if python3 -m build --version >/dev/null 2>&1; then
  python3 -m build
else
  echo "python -m build is unavailable; falling back to local setuptools build"
  python3 setup.py sdist bdist_wheel
fi

echo "Built artifacts in $ROOT_DIR/dist"
