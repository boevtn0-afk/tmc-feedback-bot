"""
Telegram-бот сбора обратной связи (ОС) по MVP «Учёт ТМЦ».

Модель: бота добавляют в групповой чат тестировщиков. Он молча логирует все
сообщения (текст + скриншоты) в SQLite, скриншоты скачивает в папку screenshots/.
В личке тоже принимает свободные сообщения. Никаких анкет — люди пишут как
привыкли. Структуру по контексту потом восстанавливает Claude при анализе.

Раз в день бот сам присылает администраторам инкрементальную выгрузку в личку.

Админ-команды (только для ADMIN_IDS):
    /export     — новое с прошлой выгрузки (двигает метку)
    /export_all — вся переписка целиком (метку не трогает)
    /stats      — сколько сообщений/скринов и по каким чатам
    /whereami   — показать id текущего чата

Конфиг — через переменные окружения (.env). Запуск: см. README.md.

ВАЖНО: чтобы бот видел ВСЕ сообщения в группе, у него должен быть отключён
"Group Privacy" в @BotFather (/setprivacy -> Disable), после чего бота нужно
заново добавить в чат. Иначе он увидит только команды и ответы на свои сообщения.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).parent
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "feedback.db"))
SCREENSHOTS_DIR = Path(os.environ.get("SCREENSHOTS_DIR", str(BASE_DIR / "screenshots")))
# Час (UTC) ежедневной авто-выгрузки. По умолчанию 6 UTC = 9:00 МСК.
DAILY_EXPORT_HOUR_UTC = int(os.environ.get("DAILY_EXPORT_HOUR_UTC", "6"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("feedback-bot")

# --------------------------------------------------------------------------- #
# Хранилище (SQLite)
# --------------------------------------------------------------------------- #


def init_db() -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at   TEXT NOT NULL,
                chat_id      INTEGER,
                chat_title   TEXT,
                chat_type    TEXT,
                message_id   INTEGER,
                user_id      INTEGER,
                username     TEXT,
                name         TEXT,
                text         TEXT,
                photo_file   TEXT,
                reply_to     INTEGER
            )
            """
        )
        # Метка последней выгрузки: докуда (по messages.id) уже выгружено.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS export_state (id INTEGER PRIMARY KEY, last_id INTEGER)"
        )
        conn.commit()


def get_cursor() -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT last_id FROM export_state WHERE id = 1").fetchone()
        return row[0] if row else 0


def set_cursor(last_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO export_state (id, last_id) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_id = excluded.last_id",
            (last_id,),
        )
        conn.commit()


def save_message(data: dict) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (
                created_at, chat_id, chat_title, chat_type, message_id,
                user_id, username, name, text, photo_file, reply_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["created_at"],
                data["chat_id"],
                data["chat_title"],
                data["chat_type"],
                data["message_id"],
                data["user_id"],
                data["username"],
                data["name"],
                data["text"],
                data["photo_file"],
                data["reply_to"],
            ),
        )
        conn.commit()
        return cur.lastrowid


def fetch_all() -> list[dict]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY chat_id, id"
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_since(last_id: int) -> list[dict]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY chat_id, id",
            (last_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Логирование входящих сообщений
# --------------------------------------------------------------------------- #

router = Router()


def full_name(message: Message) -> str:
    u = message.from_user
    if not u:
        return "—"
    return " ".join(filter(None, [u.first_name, u.last_name])) or "—"


async def download_photo(bot: Bot, message: Message) -> str | None:
    """Скачивает самый крупный размер фото в screenshots/. Возвращает имя файла."""
    if not message.photo:
        return None
    photo = message.photo[-1]
    fname = f"{message.chat.id}_{message.message_id}.jpg"
    try:
        await bot.download(photo, destination=SCREENSHOTS_DIR / fname)
        return fname
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось скачать фото %s: %s", fname, e)
        return None


async def log_message(message: Message, bot: Bot) -> None:
    text = message.text or message.caption
    photo_file = await download_photo(bot, message)
    if not text and not photo_file:
        return  # нечего сохранять (стикер/сервисное и т.п.)

    record = {
        "created_at": (message.date or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds"),
        "chat_id": message.chat.id,
        "chat_title": message.chat.title or message.chat.full_name or "—",
        "chat_type": message.chat.type,
        "message_id": message.message_id,
        "user_id": message.from_user.id if message.from_user else None,
        "username": message.from_user.username if message.from_user else None,
        "name": full_name(message),
        "text": text,
        "photo_file": photo_file,
        "reply_to": message.reply_to_message.message_id
        if message.reply_to_message
        else None,
    }
    save_message(record)
    log.info(
        "Logged msg from chat %s (%s), photo=%s",
        record["chat_id"],
        record["name"],
        bool(photo_file),
    )


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я собираю обратную связь по системе <b>«Учёт ТМЦ»</b>.\n\n"
        "Добавьте меня в рабочий чат тестирования — и просто пишите туда, "
        "что не так или что стоит добавить. Можно со скриншотом, можно без. "
        "Я всё сохраню, а команда разберёт.\n\n"
        "Можно и здесь, в личке: напишите сообщение (со скрином или без) — тоже приму."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Я собираю обратную связь по <b>«Учёт ТМЦ»</b>. Просто пишите сюда, "
        "что не так или что стоит добавить — можно со скриншотом, можно словами.\n"
    )
    if is_admin(message):
        text += (
            "\n<b>Команды администратора:</b>\n"
            "/export — новое с прошлой выгрузки (Markdown + JSON)\n"
            "/export_all — вся переписка целиком\n"
            "/stats — статистика по сообщениям и чатам\n"
            "/whereami — id текущего чата\n\n"
            "<i>Раз в день пришлю авто-выгрузку нового сюда, в личку.</i>"
        )
    await message.answer(text)


@router.message(Command("whereami"))
async def cmd_whereami(message: Message) -> None:
    await message.answer(
        f"Чат: <b>{message.chat.title or message.chat.full_name}</b>\n"
        f"chat_id: <code>{message.chat.id}</code>\n"
        f"тип: {message.chat.type}"
    )


def build_markdown(rows: list[dict]) -> str:
    lines = [
        "# Обратная связь по MVP «Учёт ТМЦ»",
        "",
        f"Выгружено: {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC  ",
        f"Всего сообщений: **{len(rows)}**",
        "",
        "> Время в UTC. Скриншоты лежат в папке `screenshots/` на сервере "
        "(имя файла указано у сообщения).",
        "",
    ]
    current_chat = object()
    for r in rows:
        if r["chat_id"] != current_chat:
            current_chat = r["chat_id"]
            lines.append("")
            lines.append(f"## Чат: {r['chat_title']} (`{r['chat_id']}`)")
            lines.append("")
        ts = r["created_at"].replace("T", " ").replace("+00:00", "")
        author = r["name"]
        if r["username"]:
            author += f" (@{r['username']})"
        head = f"**[{ts}] {author}** (#{r['message_id']}"
        if r["reply_to"]:
            head += f", в ответ на #{r['reply_to']}"
        head += ")"
        lines.append(head)
        if r["text"]:
            lines.append(f"> {r['text']}")
        if r["photo_file"]:
            lines.append(f"> 🖼️ скриншот: `screenshots/{r['photo_file']}`")
        lines.append("")
    return "\n".join(lines)


async def send_export(bot: Bot, chat_id: int, rows: list[dict], note: str) -> None:
    """Отправляет выгрузку (Markdown + JSON) в указанный чат."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    md = build_markdown(rows).encode("utf-8")
    js = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    n_photos = sum(1 for r in rows if r["photo_file"])
    await bot.send_document(
        chat_id,
        BufferedInputFile(md, filename=f"feedback-{ts}.md"),
        caption=(
            f"📄 {note}: {len(rows)} сообщений, {n_photos} скринов.\n"
            "Скриншоты — в папке screenshots/ на сервере."
        ),
    )
    await bot.send_document(
        chat_id,
        BufferedInputFile(js, filename=f"feedback-{ts}.json"),
        caption="🗃️ Тот же набор в JSON.",
    )


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    """Инкрементально: только новое с прошлой выгрузки; двигает метку."""
    if not is_admin(message):
        await message.answer("Команда доступна только администраторам.")
        return
    rows = fetch_since(get_cursor())
    if not rows:
        await message.answer(
            "Новых сообщений с прошлой выгрузки нет.\n"
            "Полная переписка целиком — /export_all."
        )
        return
    await send_export(message.bot, message.chat.id, rows, "Новое с прошлой выгрузки")
    set_cursor(max(r["id"] for r in rows))


@router.message(Command("export_all"))
async def cmd_export_all(message: Message) -> None:
    """Полная переписка целиком; метку не трогает."""
    if not is_admin(message):
        await message.answer("Команда доступна только администраторам.")
        return
    rows = fetch_all()
    if not rows:
        await message.answer("Пока нет ни одного сообщения.")
        return
    await send_export(message.bot, message.chat.id, rows, "Полная переписка")


async def scheduled_export(bot: Bot) -> None:
    """Ежедневная авто-выгрузка новых сообщений администраторам в личку."""
    rows = fetch_since(get_cursor())
    if not rows:
        log.info("Scheduled export: нет новых сообщений, пропуск.")
        return
    new_cursor = max(r["id"] for r in rows)
    sent = False
    for admin_id in ADMIN_IDS:
        try:
            await send_export(bot, admin_id, rows, "Авто-выгрузка за сутки (новое)")
            sent = True
        except Exception as e:  # noqa: BLE001
            log.warning("Scheduled export -> %s не удалась: %s", admin_id, e)
    if sent:
        set_cursor(new_cursor)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Команда доступна только администраторам.")
        return
    rows = fetch_all()
    if not rows:
        await message.answer("Пока нет ни одного сообщения.")
        return
    by_chat: dict[str, int] = {}
    photos = 0
    for r in rows:
        by_chat[r["chat_title"]] = by_chat.get(r["chat_title"], 0) + 1
        if r["photo_file"]:
            photos += 1
    lines = [
        f"<b>Всего сообщений:</b> {len(rows)}",
        f"<b>Скриншотов:</b> {photos}",
        "",
        "<b>По чатам:</b>",
    ]
    lines += [f"• {k}: {v}" for k, v in by_chat.items()]
    await message.answer("\n".join(lines))


# --------------------------------------------------------------------------- #
# Приветствие при добавлении в группу
# --------------------------------------------------------------------------- #


@router.message(F.new_chat_members)
async def on_added(message: Message, bot: Bot) -> None:
    me = await bot.me()
    if any(u.id == me.id for u in message.new_chat_members):
        await message.answer(
            "Привет! 👋 Я собираю обратную связь по <b>«Учёт ТМЦ»</b>.\n\n"
            "Просто пишите сюда, что не так или что стоит добавить — "
            "можно со скриншотом, можно словами. Я всё сохраню."
        )


# --------------------------------------------------------------------------- #
# Логирование: групповые и личные сообщения (регистрируется ПОСЛЕ команд)
# --------------------------------------------------------------------------- #


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_group_message(message: Message, bot: Bot) -> None:
    await log_message(message, bot)


@router.message(F.chat.type == "private")
async def on_private_message(message: Message, bot: Bot) -> None:
    # игнорируем прочие команды в личке
    if message.text and message.text.startswith("/"):
        return
    await log_message(message, bot)
    await message.answer("Записал, спасибо! 🙏 Можно ещё — пишите или кидайте скрин.")


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. См. .env.example / README.md")
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="UTC")
    if ADMIN_IDS:
        scheduler.add_job(
            scheduled_export,
            CronTrigger(hour=DAILY_EXPORT_HOUR_UTC, minute=0),
            args=[bot],
            id="daily_export",
            replace_existing=True,
        )
    scheduler.start()

    log.info(
        "Bot started. Admins: %s. DB: %s. Screenshots: %s. Daily export at %02d:00 UTC",
        ADMIN_IDS or "—",
        DB_PATH,
        SCREENSHOTS_DIR,
        DAILY_EXPORT_HOUR_UTC,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        log.info("Stopped: %s", e)
