#!/usr/bin/env bash
# kb-workbench launcher (Linux). Usage: ./start.sh [setup|index|serve|status|stop|doctor] [args]
# Checks dependencies and prints the exact fix command. It never downloads anything
# itself: only `./start.sh setup` installs. All messages go to stderr because in
# `serve` mode stdout carries the MCP protocol.
set -euo pipefail
cd "$(dirname "$0")"
CMD="${1:-serve}"; [ $# -gt 0 ] && shift
say() { echo "[kbw] $*" >&2; }

PY=""
for c in python3.11 python3.12 python3.13 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then PY="$c"; break; fi
done
if [ ! -x .venv/bin/python ]; then
  if [ "$CMD" != "setup" ]; then say "not set up yet. Fix: ./start.sh setup   (offline: ./start.sh setup --offline)"; exit 1; fi
  [ -n "$PY" ] || { say "Python 3.11+ not found. Fix: install python3.11 and python3.11-venv"; exit 1; }
  say "creating .venv with $PY"; "$PY" -m venv .venv
fi
VPY=.venv/bin/python
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

if [ "$CMD" != "setup" ]; then
  "$VPY" -c 'import mcp, yaml' 2>/dev/null || { say "Python packages missing. Fix: ./start.sh setup"; exit 1; }
  [ -f kbw.yaml ] || [ -n "${KBW_CONFIG:-}" ] || { say "kbw.yaml missing. Fix: cp kbw.example.yaml kbw.yaml and edit it, or ./start.sh setup --examples"; exit 1; }
  command -v node >/dev/null 2>&1 || say "warning: node not found; knowledge and code downstreams will be unavailable. Fix: install Node.js 22 LTS"
  [ -x vendor/bin/rg ] || command -v rg >/dev/null 2>&1 || say "warning: ripgrep not found; exact search unavailable. Fix: ./start.sh setup"
fi
exec "$VPY" -m kbw "$CMD" "$@"
