# Бот сбора обратной связи — MVP «Учёт ТМЦ»

Telegram-бот, который в пошаговом диалоге собирает обратную связь (ОС) от
тестировщиков MVP и складывает её в SQLite. Для разбора у Claude есть команда
`/export` — она выгружает всё в Markdown + JSON.

## Что собирает

Портал → раздел → тип (баг / идея / непонятно / другое) → серьёзность (для багов)
→ описание → что ожидал (опц.) → скриншот (опц.). Плюс автоматически: кто оставил
(Telegram id/username/имя) и время.

Разделы соответствуют экранам обоих приложений (порталы сотрудника 5173 и
админа 5174).

## Быстрый старт (локально, для проверки)

```bash
cd feedback-bot
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env    # и впиши BOT_TOKEN и ADMIN_IDS
python bot.py
```

Токен получаешь у [@BotFather](https://t.me/BotFather) (`/newbot`).
Свой Telegram id — у [@userinfobot](https://t.me/userinfobot); впиши его в `ADMIN_IDS`,
иначе `/export` и `/stats` будут недоступны.

## Деплой на VPS (systemd)

```bash
sudo mkdir -p /opt/feedback-bot
# скопировать сюда bot.py, requirements.txt, .env
cd /opt/feedback-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sudo cp deploy/feedback-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feedback-bot
sudo systemctl status feedback-bot      # проверить
journalctl -u feedback-bot -f           # логи
```

Бот работает через long-polling — публичный порт/домен не нужен, только исходящий
доступ в интернет.

## Команды

| Команда   | Кому    | Что делает                                        |
|-----------|---------|---------------------------------------------------|
| `/start`  | все     | приветствие и кнопка «Оставить обратную связь»    |
| `/cancel` | все     | прервать текущий диалог                           |
| `/export` | админ   | выгрузка всей ОС в Markdown + JSON (файлами)       |
| `/stats`  | админ   | сводка: сколько ОС, по типам и порталам           |

## Как отдавать ОС в Claude

1. В боте отправь `/export`.
2. Бот пришлёт два файла: `feedback-*.md` и `feedback-*.json`.
3. Скинь `.md` (или `.json`) в чат с Claude Code с просьбой «разбери и собери
   бэклог / приоритизируй / сгруппируй по разделам».

> Скриншоты в экспорт попадают как `file_id` (ссылка внутри Telegram), не как
> картинки. Если для разбора нужны сами изображения — пересылай нужные скриншоты
> в чат отдельно.

## Дальнейшая автоматизация (когда захотим)

Архитектура готова к апгрейду без переписывания:

- **Авто-тегирование через Claude API** — вызывать модель на каждой новой записи
  в `save_feedback()` (тип/раздел/серьёзность/поиск дублей), складывать разбор
  в те же строки БД.
- **Недельный дайджест** — плановый агент (Claude Code `/schedule`) читает новые
  записи и постит сводку с приоритетами.
- **Синхронизация в трекер** — выгрузка подтверждённых пунктов в GitHub Issues /
  Notion как задач на доработку до прода.
```
