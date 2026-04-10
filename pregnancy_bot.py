"""
Pregnancy companion bot — Ruh.
Runs in the SAME Railway process as Rafiq, via build_pregnancy_app().

Needs env vars:
    PREG_BOT_TOKEN          — from BotFather (create a new bot)
    PREG_AGENT_ID           — from setup_pregnancy.py
    ENVIRONMENT_ID          — reuses Rafiq's environment
    PREG_ALLOWED_USER_IDS   — optional, comma-separated Telegram user IDs

If PREG_BOT_TOKEN or PREG_AGENT_ID are missing, build_pregnancy_app() returns None
and bot.py just runs Rafiq alone. Zero extra setup until Jinan's ready.
"""
import asyncio
import logging
import os
import sqlite3

from anthropic import Anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)

_client = Anthropic()


def _make_db():
    db = sqlite3.connect("preg_sessions.db", check_same_thread=False)
    db.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(chat_id INTEGER PRIMARY KEY, session_id TEXT)"
    )
    db.commit()
    return db


def _ask(session_id: str, text: str) -> str:
    parts = []
    with _client.beta.sessions.events.stream(session_id) as stream:
        _client.beta.sessions.events.send(
            session_id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": text}],
            }],
        )
        for event in stream:
            etype = getattr(event, "type", None)
            if etype in ("agent.message", "message"):
                for block in getattr(event, "content", []):
                    t = getattr(block, "text", None)
                    if t:
                        parts.append(t)
            elif etype in ("content_block_delta", "agent.message.delta"):
                delta = getattr(event, "delta", None)
                if delta:
                    t = getattr(delta, "text", None)
                    if t:
                        parts.append(t)
            elif etype in ("session.status_idle", "session.idle", "done"):
                break
    return "".join(parts).strip() or "..."


def build_pregnancy_app() -> Application | None:
    """Returns a configured PTB Application, or None if env vars missing."""
    token    = os.environ.get("PREG_BOT_TOKEN")
    agent_id = os.environ.get("PREG_AGENT_ID")
    env_id   = os.environ.get("ENVIRONMENT_ID")

    if not (token and agent_id and env_id):
        logger.info("Pregnancy bot not configured (missing PREG_BOT_TOKEN or PREG_AGENT_ID). Skipping.")
        return None

    allowed_raw = os.environ.get("PREG_ALLOWED_USER_IDS", "").strip()
    allowed_ids = {s.strip() for s in allowed_raw.split(",") if s.strip()}

    db = _make_db()

    def get_sess(chat_id: int) -> str | None:
        row = db.execute("SELECT session_id FROM sessions WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else None

    def save_sess(chat_id: int, session_id: str):
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (chat_id, session_id))
        db.commit()

    def clear_sess(chat_id: int):
        db.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
        db.commit()

    def new_sess() -> str:
        s = _client.beta.sessions.create(agent=agent_id, environment_id=env_id)
        logger.info(f"[preg] New session: {s.id}")
        return s.id

    def is_allowed(update: Update) -> bool:
        if not allowed_ids:
            return True
        return str(update.effective_user.id) in allowed_ids

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        session_id = get_sess(chat_id) or new_sess()
        save_sess(chat_id, session_id)

        try:
            response = await asyncio.to_thread(_ask, session_id, update.message.text)
        except Exception as e:
            err = str(e).lower()
            if "rate limit" in err or "429" in err:
                response = "Bentar ya sayang, lagi agak sibuk. Coba kirim lagi sebentar 💛"
            else:
                logger.warning(f"[preg] session error: {e} — new session")
                clear_sess(chat_id)
                session_id = new_sess()
                save_sess(chat_id, session_id)
                try:
                    response = await asyncio.to_thread(_ask, session_id, update.message.text)
                except Exception as e2:
                    response = f"⚠️ Error: {e2}"

        for i in range(0, max(len(response), 1), 4096):
            chunk = response[i:i + 4096]
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        await update.message.reply_text(
            "Halo sayang 💛 Aku Ruh, temenin kamu selama kehamilan.\n"
            "Cerita apa aja ya — mual, capek, excited, takut, semua boleh.\n"
            "/reset kalau mau mulai obrolan baru."
        )

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        clear_sess(update.effective_chat.id)
        await update.message.reply_text("Fresh start ya 💛")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Pregnancy bot built.")
    return app
