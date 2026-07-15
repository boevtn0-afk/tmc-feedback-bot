#!/usr/bin/env bash
# Автоподтягивание обновлений бота из git и перезапуск при изменениях.
# Ставится в cron раз в минуту (см. README.md, раздел про автодеплой).
set -euo pipefail

cd /root/feedback-bot

git fetch -q origin

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse '@{u}')

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date -u +%FT%TZ) update: $LOCAL -> $REMOTE"
    git reset --hard "$REMOTE" -q
    # доустановить зависимости, если requirements.txt менялся (безопасно вызывать всегда)
    .venv/bin/pip install -q -r requirements.txt || true
    systemctl restart feedback-bot
    echo "$(date -u +%FT%TZ) restarted"
fi
