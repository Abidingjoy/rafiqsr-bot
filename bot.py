"""
RafiqSr Telegram Bot
Bridges Telegram ↔ Claude Managed Agents
Supports text + voice notes (transcribed via Groq Whisper) + images + PDFs
"""
import asyncio
import base64
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
AGENT_ID       = os.environ["AGENT_ID"]
ENVIRONMENT_ID = os.environ["ENVIRONMENT_ID"]
VAULT_REPO     = os.environ.get("VAULT_GITHUB_REPO", "")
ALLOWED_USER_ID = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")

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
    tools_used = []

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

            if etype == "agent.message":
                for block in event.content:
                    if hasattr(block, "text") and block.text:
                        parts.append(block.text)

            elif etype == "agent.tool_use":
                name = getattr(event, "name", "tool")
                tools_used.append(name)
                logger.info(f"Tool used: {name}")

            elif etype == "session.status_idle":
                break

    response = "".join(parts).strip()
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
            logger.warning(f"Session error ({session_id}): {e} — creating new session")
            clear_session(chat_id)
            session_id = new_session()
            save_session(chat_id, session_id)
            response = await asyncio.to_thread(ask_rafiq, session_id, text, extra_content)

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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("RafiqSr bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
