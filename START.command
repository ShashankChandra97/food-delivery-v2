#!/bin/zsh
cd "${0:A:h}"
if curl -fsS http://127.0.0.1:8787/health >/dev/null 2>&1; then
  open http://localhost:8787
  exit 0
fi
RUNTIME_PYTHON="/Users/shashankchandra/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ ! -x "$RUNTIME_PYTHON" ]]; then
  RUNTIME_PYTHON="$(command -v python3)"
fi
open http://localhost:8787
"$RUNTIME_PYTHON" service.py
