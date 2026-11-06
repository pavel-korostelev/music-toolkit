#!/bin/zsh
set -eu
cd "${0:A:h}"
export PYTHONDONTWRITEBYTECODE=1
echo 'Создание локального окружения и установка зависимости для экспорта Яндекса.'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Нужен Python 3.9 или новее: https://www.python.org/downloads/macos/")'
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
echo 'Готово. Открой Start.command.'
