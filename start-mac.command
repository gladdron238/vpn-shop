#!/bin/bash
# Запуск на Mac: дважды щёлкните по этому файлу в Finder.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null; then
  echo "Нужен Python 3. Установите его с https://www.python.org/downloads/ и запустите снова."
  read -r -p "Нажмите Enter для выхода"; exit 1
fi

[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан файл .env — впишите в него токен бота (откроется в редакторе)."
  open -e .env
fi

echo
echo "Сайт: http://localhost:8000   (остановить: Ctrl+C)"
echo
(sleep 2 && open http://localhost:8000) &
./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
