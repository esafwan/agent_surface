#!/bin/bash
cd "$$(dirname "$$0")" || exit 1
BIN="$${SURFACE_BIN:-}"
if [ -z "$$BIN" ]; then
  BIN="$$(/bin/zsh -lc 'command -v surface' 2>/dev/null)"
fi
if [ -z "$$BIN" ]; then
  BIN="$fallback_bin"
fi
if [ -z "$$BIN" ] || [ ! -x "$$BIN" ]; then
  echo "Could not find the 'surface' command. Set SURFACE_BIN or reinstall agent_surface."
  read -r -p "Press Enter to close this window..."
  exit 1
fi
"$$BIN" resume . --app-window || { echo "surface resume failed (exit $$?)"; read -r -p "Press Enter to close this window..."; exit 1; }
