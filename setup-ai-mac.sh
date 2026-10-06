#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1
if ! command -v python3.11 >/dev/null 2>&1; then
  echo "Нужен Python 3.11. Установите его с python.org или командой brew install python@3.11."
  exit 1
fi
exec python3.11 setup_ai.py --cpu
