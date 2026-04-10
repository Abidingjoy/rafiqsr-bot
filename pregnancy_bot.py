"""
Pregnancy companion bot — Ruh.
Runs in the SAME Railway process as Rafiq, via build_pregnancy_app().

Required env vars:
    PREG_BOT_TOKEN          — from BotFather (new bot)
    PREG_AGENT_ID           — from setup_pregnancy.py
    ENVIRONMENT_ID          — reuses Rafiq's environment

Optional env vars:
    PREG_ALLOWED_USER_IDS   — comma-separated Telegram user IDs (leave empty = allow all)
    PREG_REMINDER_CHAT_ID   — Telegram chat ID to send scheduled reminders to
    PREG_DUE_DATE           — due date in YYYY-MM-DD format (default: 2026-10-15)

Features:
    - Text chat with Ruh (warm, bilingual, pregnancy-focused)
    - Voice note support (Groq Whisper transcription)
    - Image support (USG photos, etc.)
    - Daily vitamin reminder (08:00 WIB)
    - Weekly pregnancy update every Sunday (09:00 WIB)
    - /week command — on-demand week status + baby update
    - /reset — fresh session
"""
import asyncio
import base64
import datetime
import logging
import mimetypes
import os
import sqlite3
import tempfile

import pytz
from anthropic import Anthropic
from groq import Groq
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
_groq   = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

JAKARTA = pytz.timezone("Asia/Jakarta")


# ── Pregnancy week calculator ─────────────────────────────────────────────────

def _get_week_info(due_date_str: str) -> dict:
    """Returns current pregnancy week and days remaining."""
    try:
        due = datetime.datetime.strptime(due_date_str, "%Y-%m-%d").date()
    except ValueError:
        due = datetime.date(2026, 10, 15)

    today = datetime.date.today()
    days_remaining = (due - today).days
    weeks_remaining = days_remaining / 7
    current_week = round(40 - weeks_remaining)
    current_week = max(1, min(42, current_week))  # clamp to valid range

    return {
        "week": current_week,
        "days_remaining": max(0, days_remaining),
        "weeks_remaining": max(0, round(weeks_remaining, 1)),
        "due_date": due.strftime("%-d %B %Y") if os.name != "nt" else due.strftime("%d %B %Y"),
    }


# ── DB ────────────────────────────────────────────────────────────────────────

def _make_db():
    db = sqlite3.connect("preg_sessions.db", check_same_thread=False)
    db.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(chat_id INTEGER PRIMARY KEY, session_id TEXT)"
    )
    db.commit()
    return db


# ── Agent call ────────────────────────────────────────────────────────────────

def _ask(session_id: str, text: str, extra_content: list | None = None) -> str:
    parts = []
    content = []
    if extra_content:
        content.extend(extra_content)
    if text:
        content.append({"type": "text", "text": text})

    with _client.beta.sessions.events.stream(session_id) as stream:
        _client.beta.sessions.events.send(
            session_id,
            events=[{"type": "user.message", "content": content}],
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


def _transcribe(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = _groq.audio.transcriptions.create(
            file=(os.path.basename(file_path), f),
            model="whisper-large-v3",
            language="id",
        )
    return result.text.strip()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _download_b64(file) -> tuple[str, str]:
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
    await file.download_to_drive(tmp_path)
    mime = mimetypes.guess_type(getattr(file, "file_path", "") or "")[0] or "image/jpeg"
    with open(tmp_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    os.unlink(tmp_path)
    return data, mime


async def _send(bot, chat_id: int, text: str):
    chunks = [text[i:i + 4096] for i in range(0, max(len(text), 1), 4096)]
    for chunk in chunks:
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id=chat_id, text=chunk)


# ── Build function ────────────────────────────────────────────────────────────

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

    reminder_chat_id_raw = os.environ.get("PREG_REMINDER_CHAT_ID", "").strip()
    reminder_chat_id = int(reminder_chat_id_raw) if reminder_chat_id_raw else None

    due_date_str = os.environ.get("PREG_DUE_DATE", "2026-10-15").strip()

    db = _make_db()

    # ── Session management ────────────────────────────────────────────────────

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

    # ── Core message processor ────────────────────────────────────────────────

    async def process(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      text: str, extra_content: list | None = None):
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        session_id = get_sess(chat_id) or new_sess()
        save_sess(chat_id, session_id)

        try:
            response = await asyncio.to_thread(_ask, session_id, text, extra_content)
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
                    response = await asyncio.to_thread(_ask, session_id, text, extra_content)
                except Exception as e2:
                    response = f"⚠️ Error: {e2}"

        chunks = [response[i:i + 4096] for i in range(0, max(len(response), 1), 4096)]
        for chunk in chunks:
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        await process(update, context, update.message.text)

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            await file.download_to_drive(tmp_path)
            transcript = await asyncio.to_thread(_transcribe, tmp_path)
            await update.message.reply_text(f"_{transcript}_", parse_mode="Markdown")
            await process(update, context, transcript)
        except Exception as e:
            logger.error(f"[preg] voice error: {e}")
            await update.message.reply_text("Gagal denger voice note-nya. Coba ketik aja ya 💛")
        finally:
            os.unlink(tmp_path)

    async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        try:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            data, _ = await _download_b64(file)
            caption = update.message.caption or "Ini foto apa? Ceritain dong 💛"
            extra = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}]
            await process(update, context, caption, extra)
        except Exception as e:
            logger.error(f"[preg] image error: {e}")
            await update.message.reply_text("Gambarnya belum kebaca. Coba lagi ya 💛")

    async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        doc = update.message.document
        supported = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"}
        if doc.mime_type not in supported:
            await update.message.reply_text("Format ini belum didukung. Kirim gambar atau PDF ya 💛")
            return
        try:
            file = await context.bot.get_file(doc.file_id)
            data, _ = await _download_b64(file)
            caption = update.message.caption or f"Ini file {doc.file_name}, tolong baca ya."
            if doc.mime_type == "application/pdf":
                extra = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}]
            else:
                extra = [{"type": "image", "source": {"type": "base64", "media_type": doc.mime_type, "data": data}}]
            await process(update, context, caption, extra)
        except Exception as e:
            logger.error(f"[preg] doc error: {e}")
            await update.message.reply_text("Filenya belum kebaca. Coba lagi ya 💛")

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        info = _get_week_info(due_date_str)
        await update.message.reply_text(
            f"Halo sayang 💛 Aku Ruh — temenin kamu selama kehamilan.\n\n"
            f"Sekarang minggu ke-*{info['week']}*. Tinggal {info['days_remaining']} hari lagi!\n\n"
            f"Cerita apa aja ya — mual, capek, excited, takut. Semua boleh.\n"
            f"/week — update minggu ini\n"
            f"/reset — mulai obrolan baru",
            parse_mode="Markdown",
        )

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        clear_sess(update.effective_chat.id)
        await update.message.reply_text("Fresh start ya 💛")

    async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            return
        info = _get_week_info(due_date_str)
        prompt = (
            f"Kasih update kehamilan minggu ke-{info['week']}. "
            f"Sisa {info['days_remaining']} hari ({info['weeks_remaining']} minggu) sampai due date {info['due_date']}.\n\n"
            f"Format:\n"
            f"- Dede lagi ngapain minggu ini (perkembangan fisik/sensoris)\n"
            f"- Apa yang mungkin dirasain ibunya\n"
            f"- Satu tip praktis\n"
            f"- Satu kalimat penyemangat yang genuine\n\n"
            f"Singkat, hangat, bahasa campuran ID/EN. Pakai emoji secukupnya."
        )
        await process(update, context, prompt)

    # ── Scheduled reminders ───────────────────────────────────────────────────

    async def send_vitamin_reminder(context: ContextTypes.DEFAULT_TYPE):
        if not reminder_chat_id:
            return
        logger.info("[preg] Sending vitamin reminder...")
        messages = [
            "Pagi sayang! ☀️ Jangan lupa vitamin prenatalnya ya 💊💛",
            "Selamat pagi! Sudah minum vitamin hari ini? 💛",
            "Morning! Vitamin dulu ya sebelum aktivitas 🌸",
            "Pagi! Dede nunggu vitaminnya nih 💛👶",
            "Good morning! Yuk, vitamin dulu — untuk kamu dan dede 💊🌿",
        ]
        import random
        msg = random.choice(messages)
        try:
            await context.bot.send_message(chat_id=reminder_chat_id, text=msg)
        except Exception as e:
            logger.error(f"[preg] vitamin reminder error: {e}")

    async def send_weekly_update(context: ContextTypes.DEFAULT_TYPE):
        if not reminder_chat_id:
            return
        logger.info("[preg] Sending weekly pregnancy update...")
        info = _get_week_info(due_date_str)
        try:
            # Fresh session for weekly update
            sess = new_sess()
            prompt = (
                f"Ini weekly update otomatis untuk kehamilan minggu ke-{info['week']}. "
                f"Sisa {info['days_remaining']} hari sampai due date {info['due_date']}.\n\n"
                f"Tulis pesan yang hangat dan personal untuk Jinan — bukan laporan medis. "
                f"Sertakan: perkembangan dede minggu ini, apa yang mungkin Jinan rasain, "
                f"satu hal kecil yang bisa dilakuin minggu ini, dan kalimat penyemangat. "
                f"Campuran ID/EN, singkat, pakai emoji secukupnya."
            )
            response = await asyncio.to_thread(_ask, sess, prompt)
            if response and response != "...":
                await _send(context.bot, reminder_chat_id, response)
        except Exception as e:
            logger.error(f"[preg] weekly update error: {e}")

    # ── Build app ─────────────────────────────────────────────────────────────

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Schedule reminders (only if reminder_chat_id is set)
    if reminder_chat_id and app.job_queue:
        # Daily vitamin reminder — 08:00 WIB
        app.job_queue.run_daily(
            send_vitamin_reminder,
            time=datetime.time(hour=8, minute=0, tzinfo=JAKARTA),
            name="preg_vitamin_reminder",
        )
        # Weekly pregnancy update — every Sunday 09:00 WIB
        app.job_queue.run_daily(
            send_weekly_update,
            time=datetime.time(hour=9, minute=0, tzinfo=JAKARTA),
            days=(6,),  # Sunday
            name="preg_weekly_update",
        )
        logger.info(f"Pregnancy reminders scheduled → chat {reminder_chat_id}")
    elif reminder_chat_id:
        logger.warning("[preg] job_queue unavailable — reminders disabled. Install APScheduler.")
    else:
        logger.info("[preg] No PREG_REMINDER_CHAT_ID set — scheduled reminders disabled.")

    logger.info("Pregnancy bot (Ruh) built.")
    return app
