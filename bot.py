"""
RafiqSr Telegram Bot
Bridges Telegram ↔ Claude Managed Agents
Supports text + voice notes (transcribed via Groq Whisper) + images + PDFs
"""
import asyncio
import base64
import datetime
import logging
import mimetypes
import os
import sqlite3
import tempfile

from dotenv import load_dotenv
load_dotenv(override=True)

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
import pytz

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
AGENT_ID        = os.environ["AGENT_ID"]
ENVIRONMENT_ID  = os.environ["ENVIRONMENT_ID"]
VAULT_REPO      = os.environ.get("VAULT_GITHUB_REPO", "")
ALLOWED_USER_ID = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")

BRIEF_PROMPT = (
    "Generate a CEO brief for me. "
    "Clone the vault first (git clone $VAULT_GITHUB_REPO /tmp/vault), then read:\n"
    "- raw/data/hablum-pipeline.csv — pipeline status\n"
    "- wiki/projects/hablum.md — Hablum overview\n"
    "- wiki/projects/matter-mos.md — Matter Mos overview\n"
    "- wiki/projects/kaum.md — KAUM overview\n"
    "- wiki/projects/cortexin.md — Cortexin overview\n\n"
    "Format the brief like this:\n"
    "🔴🟡🟢 status per project (one line each)\n"
    "📋 Pipeline snapshot — who responded, who needs follow-up\n"
    "⚡ Top 3 things I should do today\n"
    "🚧 What's blocked and needs a decision\n\n"
    "Be direct. No fluff. Telegram format — short lines, no walls of text."
)

client = Anthropic()
groq   = Groq(api_key=os.environ["GROQ_API_KEY"])

# ── Session store (SQLite) ────────────────────────────────────────────────────

db = sqlite3.connect("sessions.db", check_same_thread=False)
db.execute(
    "CREATE TABLE IF NOT EXISTS sessions "
    "(chat_id INTEGER PRIMARY KEY, session_id TEXT)"
)
db.commit()


def get_session(chat_id: int) -> str | None:
    row = db.execute(
        "SELECT session_id FROM sessions WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return row[0] if row else None


def save_session(chat_id: int, session_id: str):
    db.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?, ?)", (chat_id, session_id)
    )
    db.commit()


def clear_session(chat_id: int):
    db.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
    db.commit()


def new_session() -> str:
    session = client.beta.sessions.create(
        agent=AGENT_ID,
        environment_id=ENVIRONMENT_ID,
    )
    logger.info(f"New session: {session.id}")

    if VAULT_REPO:
        client.beta.sessions.events.send(
            session.id,
            events=[{
                "type": "user.message",
                "content": [{
                    "type": "text",
                    "text": (
                        f"[SYSTEM CONTEXT] Vault repo: {VAULT_REPO}\n"
                        f"Kalau butuh info dari vault, clone dengan:\n"
                        f"git clone {VAULT_REPO} /tmp/vault\n"
                        f"lalu baca wiki/ directory. Acknowledge singkat saja."
                    ),
                }],
            }],
        )

    return session.id


# ── Transcription (Groq Whisper) ──────────────────────────────────────────────

def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = groq.audio.transcriptions.create(
            file=(os.path.basename(file_path), f),
            model="whisper-large-v3",
            language="id",  # supports mixed ID/EN automatically
        )
    return result.text.strip()


# ── Core agent call (sync — runs in thread) ───────────────────────────────────

def ask_rafiq(session_id: str, text: str, extra_content: list | None = None) -> str:
    parts = []

    content = []
    if extra_content:
        content.extend(extra_content)
    if text:
        content.append({"type": "text", "text": text})

    with client.beta.sessions.events.stream(session_id) as stream:
        client.beta.sessions.events.send(
            session_id,
            events=[{
                "type": "user.message",
                "content": content,
            }],
        )

        for event in stream:
            etype = getattr(event, "type", None)
            logger.info(f"[stream] event type: {etype}")

            # Collect any text content from agent messages
            if etype in ("agent.message", "message"):
                for block in getattr(event, "content", []):
                    text_val = getattr(block, "text", None)
                    if text_val:
                        parts.append(text_val)

            # Also handle streaming delta events
            elif etype in ("content_block_delta", "agent.message.delta"):
                delta = getattr(event, "delta", None)
                if delta:
                    text_val = getattr(delta, "text", None)
                    if text_val:
                        parts.append(text_val)

            elif etype in ("agent.tool_use", "tool_use"):
                logger.info(f"Tool used: {getattr(event, 'name', 'unknown')}")

            elif etype in ("session.status_idle", "session.idle", "done"):
                break

    response = "".join(parts).strip()
    if not response:
        logger.warning(f"[ask_rafiq] Empty response for session {session_id}")
    return response or "..."


# ── Typing indicator ──────────────────────────────────────────────────────────

async def keep_typing(chat_id: int, bot, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(4)


# ── Shared: process text through Rafiq ───────────────────────────────────────

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, extra_content: list | None = None):
    chat_id = update.effective_chat.id

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        keep_typing(chat_id, context.bot, stop_typing)
    )

    try:
        session_id = get_session(chat_id)
        if not session_id:
            session_id = new_session()
            save_session(chat_id, session_id)

        try:
            response = await asyncio.to_thread(ask_rafiq, session_id, text, extra_content)
        except Exception as e:
            err = str(e).lower()
            if "rate limit" in err or "429" in err or "rate_limit" in err:
                response = "⚠️ Kena rate limit Anthropic. Tunggu sebentar terus coba lagi."
            else:
                logger.warning(f"Session error ({session_id}): {e} — creating new session")
                clear_session(chat_id)
                session_id = new_session()
                save_session(chat_id, session_id)
                try:
                    response = await asyncio.to_thread(ask_rafiq, session_id, text, extra_content)
                except Exception as e2:
                    response = f"⚠️ Error: {e2}"

    finally:
        stop_typing.set()
        typing_task.cancel()

    chunks = [response[i : i + 4096] for i in range(0, max(len(response), 1), 4096)]
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


# ── File helpers ─────────────────────────────────────────────────────────────

async def download_to_base64(file) -> tuple[str, str]:
    """Download a Telegram file and return (base64_data, mime_type)."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
    await file.download_to_drive(tmp_path)
    mime_type = mimetypes.guess_type(file.file_path or "")[0] or "application/octet-stream"
    with open(tmp_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    os.unlink(tmp_path)
    return data, mime_type


# ── Telegram handlers ─────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == ALLOWED_USER_ID


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await process_message(update, context, update.message.text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Download voice note
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await file.download_to_drive(tmp_path)
        logger.info(f"Transcribing voice note ({voice.duration}s)...")

        # Transcribe
        transcript = await asyncio.to_thread(transcribe_audio, tmp_path)
        logger.info(f"Transcript: {transcript}")

        # Show transcript so Fadhil knows what was heard
        await update.message.reply_text(f"_{transcript}_", parse_mode="Markdown")

        # Send to Rafiq
        await process_message(update, context, transcript)

    except Exception as e:
        logger.error(f"Voice transcription error: {e}")
        await update.message.reply_text("Gagal transcribe voice note. Coba lagi atau ketik aja.")
    finally:
        os.unlink(tmp_path)


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Get highest-res photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        data, _ = await download_to_base64(file)

        caption = update.message.caption or "Ini gambar apa / maksudnya apa?"

        extra_content = [{
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": data,
            },
        }]
        await process_message(update, context, caption, extra_content)

    except Exception as e:
        logger.error(f"Image handling error: {e}")
        await update.message.reply_text("Gagal proses gambar. Coba lagi.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    doc = update.message.document

    # Only handle PDFs and common image types
    supported_mime = {
        "application/pdf",
        "image/jpeg", "image/png", "image/gif", "image/webp",
    }
    if doc.mime_type not in supported_mime:
        await update.message.reply_text(
            f"Format `{doc.mime_type}` belum didukung. Kirim PDF atau gambar.",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        file = await context.bot.get_file(doc.file_id)
        data, mime_type = await download_to_base64(file)
        mime_type = doc.mime_type  # use Telegram's mime_type, more reliable

        caption = update.message.caption or f"Ini file {doc.file_name}, tolong baca dan rangkum."

        if mime_type == "application/pdf":
            extra_content = [{
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data,
                },
            }]
        else:
            extra_content = [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": data,
                },
            }]

        await process_message(update, context, caption, extra_content)

    except Exception as e:
        logger.error(f"Document handling error: {e}")
        await update.message.reply_text("Gagal proses file. Coba lagi.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Rafiq Sr. online.\n\nKirim teks atau voice note. /reset untuk fresh session."
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    clear_session(update.effective_chat.id)
    await update.message.reply_text("Session cleared. Fresh start, bro.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    session_id = get_session(chat_id)
    if session_id:
        await update.message.reply_text(f"Active session: `{session_id}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("No active session. Send a message to start one.")


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await process_message(update, context, BRIEF_PROMPT)


async def cmd_kaum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    anchor = " ".join(context.args).upper() if context.args else None

    if not anchor:
        await update.message.reply_text(
            "Kasih anchor word-nya bro. Contoh: `/kaum WAKTU` atau `/kaum API`",
            parse_mode="Markdown",
        )
        return

    kaum_prompt = (
        f"KAUM creative session. Anchor word: {anchor}\n\n"
        f"Clone vault dulu (git clone $VAULT_GITHUB_REPO /tmp/vault), "
        f"baca wiki/projects/kaum.md — khususnya Canon Bank dan teknik yang udah dipakai.\n\n"
        f"Tugas:\n"
        f"1. Generate 5 bar kandidat dengan anchor '{anchor}'. "
        f"Satu anchor = satu image/metaphor yang di-explore dari berbagai sudut.\n"
        f"2. Score tiap bar: Orbit (0-5) / Rhyme (0-5) / Multi (0-5). "
        f"Composite = average. Threshold masuk canon: < 4.5.\n"
        f"3. Flag bar mana yang layak masuk canon bank.\n"
        f"4. Tandai teknik baru yang dipakai kalau ada.\n\n"
        f"Format output:\n"
        f"**[Bar]** — O:X R:X M:X → composite X.X ✅/❌\n"
        f"Satu baris notes per bar.\n\n"
        f"Tulis dalam bahasa yang natural — campuran EN/ID kayak biasa. "
        f"Jaga ruh KAUM: kerentanan, spiritual, pulang."
    )

    await process_message(update, context, kaum_prompt)


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def send_morning_brief(context: ContextTypes.DEFAULT_TYPE):
    """Sends the morning brief automatically to Fadhil every day."""
    if not ALLOWED_USER_ID:
        return

    chat_id = int(ALLOWED_USER_ID)
    logger.info("Sending morning brief...")

    try:
        # Always use a fresh dedicated session for the brief
        brief_session = new_session()
        logger.info(f"Morning brief session: {brief_session}")

        response = await asyncio.to_thread(ask_rafiq, brief_session, BRIEF_PROMPT)

        if not response or response == "...":
            logger.warning("Morning brief returned empty — skipping send.")
            return

        chunks = [response[i:i + 4096] for i in range(0, len(response), 4096)]
        for chunk in chunks:
            try:
                await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=chunk)

    except Exception as e:
        logger.error(f"Morning brief error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def build_rafiq_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("kaum", cmd_kaum))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Morning brief — 7:30 AM WIB
    if app.job_queue:
        jakarta = pytz.timezone("Asia/Jakarta")
        app.job_queue.run_daily(
            send_morning_brief,
            time=datetime.time(hour=7, minute=30, tzinfo=jakarta),
            name="morning_brief",
        )
        logger.info("Morning brief scheduled at 07:30 WIB daily.")
    else:
        logger.warning("job_queue not available — morning brief scheduler disabled. Install APScheduler.")

    return app


async def run_apps(apps: list[Application]):
    """Initialize, start, and poll multiple PTB Applications in one process."""
    for app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    logger.info(f"{len(apps)} bot(s) running. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()  # run until cancelled (SIGTERM/KeyboardInterrupt)
    except asyncio.CancelledError:
        pass
    finally:
        for app in apps:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception as e:
                logger.warning(f"Shutdown error: {e}")


def main():
    from pregnancy_bot import build_pregnancy_app

    rafiq_app = build_rafiq_app()
    apps: list[Application] = [rafiq_app]

    preg_app = build_pregnancy_app()
    if preg_app:
        apps.append(preg_app)
        logger.info("Running Rafiq + Pregnancy companion in one process.")
    else:
        logger.info("Running Rafiq only.")

    # Single-bot fast path — keeps existing behavior
    if len(apps) == 1:
        logger.info("RafiqSr bot starting...")
        rafiq_app.run_polling(allowed_updates=Update.ALL_TYPES)
        return

    # Multi-bot path
    logger.info("Starting multi-bot runtime...")
    try:
        asyncio.run(run_apps(apps))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
