#!/bin/bash
# Запуск установщика.
# Использование:
#   bash install.sh
#   bash install.sh --debug
set -euo pipefail

cd "$(dirname "$0")"
exec python3 wine.py "$@"