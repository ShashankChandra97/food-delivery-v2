#!/bin/zsh
cd "${0:A:h}"
if python3 launch.py; then
  open http://localhost:8787
else
  echo "Startup failed. See the message above and data/service.log."
  read -r '?Press Enter to close this window.'
  exit 1
fi
