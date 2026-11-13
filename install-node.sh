#!/bin/sh
# Ставит web/node_modules по web/package-lock.json и собирает страницу в web/dist.
# Нужны Node.js и npm. Пакеты скачиваются из registry.npmjs.org.
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root/web"
if ! command -v npm >/dev/null 2>&1; then
  echo "Не найден npm. Установите Node.js и повторите: sh deploy/install-node.sh" >&2
  exit 1
fi
npm ci
npm run build
echo "Готово: $root/web/node_modules и $root/web/dist"
