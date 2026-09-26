#!/bin/zsh
cd "${0:A:h}"
RUNTIME_PYTHON="/Users/shashankchandra/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ ! -x "$RUNTIME_PYTHON" ]]; then
  RUNTIME_PYTHON="$(command -v python3)"
fi
"$RUNTIME_PYTHON" send_request.py "$@"
