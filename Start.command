#!/bin/zsh
set -eu
cd "${0:A:h}"
export PYTHONDONTWRITEBYTECODE=1
if [[ -x .venv/bin/python3 ]]; then
  .venv/bin/python3 music_tool.py menu
else
  python3 music_tool.py menu
fi
