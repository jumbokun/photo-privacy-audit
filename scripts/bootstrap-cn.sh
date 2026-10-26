#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INDEX_URL="${PHOTO_AUDIT_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
if [[ -n "${PHOTO_AUDIT_WHEELHOUSE:-}" ]]; then
  .venv/bin/python -m pip install --no-index --find-links "$PHOTO_AUDIT_WHEELHOUSE" -r requirements-dev.txt
else
  .venv/bin/python -m pip install --index-url "$INDEX_URL" -r requirements-dev.txt
fi
.venv/bin/python audit.py doctor
