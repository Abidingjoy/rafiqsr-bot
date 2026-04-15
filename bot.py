"""
RafiqSr Telegram Bot — Full OpenClaw Replacement
Bridges Telegram ↔ Claude Managed Agents
Features: text, voice, images, PDFs, persistent memory, nudges, shortcuts, wiki workflows
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

import memory

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

JAKARTA = pytz.timezone("Asia/Jakarta")

# Session timeout — auto-save if idle longer than this
SESSION_TIMEOUT = datetime.timedelta(hours=4)

# ── Shortcuts (ported from Mos SHORTCUTS.md) ─────────────────────────────────

SHORTCUTS = {
    "MOE": "Clone vault (git clone $VAULT_GITHUB_REPO /tmp/vault). Read wiki/entities/moe.md + wiki/concepts/nas-line.md. Context: perfumer, NAS formula progress.",
    "HADRA": "Clone vault. Read wiki/projects/hablum.md. Focus on Hadra architecture concept.",
    "EXECUTION": "Clone vault. Read 99 - EXECUTION/NOW.md + wiki/status/daily.md. What's pending, what's blocked?",
    "HABLUM B2B": "Clone vault. Read wiki/projects/hablum.md B2B section + raw/data/hablum-pipeline.csv. Pipeline status.",
    "HABLUM": "Clone vault. Read wiki/projects/hablum.md. Full project overview.",
    "PRICING": "Clone vault. Read wiki/projects/hablum.md pricing section. Unit economics, COGS, margins.",
    "KAUM ALBUM": "Clone vault. Read wiki/projects/kaum.md. Album timeline, demo status, canon bank.",
    "CORTEXIN": "Clone vault. Read wiki/projects/cortexin.md. Equity status, timeline, Pak Andrew.",
    "MNA": "Clone vault. Read wiki/entities/pt-mna.md + wiki/decisions/mna-structure.md. Family holding.",
    "NAS": "Clone vault. Read wiki/concepts/nas-line.md. NAS EDP development status.",
    "MATTER MOS": "Clone vault. Read wiki/projects/matter-mos.md. Music career overview.",
}

# ── Brief prompt ─────────────────────────────────────────────────────────────

BRIEF_PROMPT = (
    "Generate a CEO brief for me. "
    "Clone the vault first (git clone $VAULT_GITHUB_REPO /tmp/vault), then read the files below SILENTLY — do not output raw file contents, shell output, or git output.\n"
    "- raw/data/hablum-pipeline.csv — pipeline status\n"
    "- wiki/projects/hablum.md — Hablum overview\n"
    "- wiki/projects/matter-mos.md — Matter Mos overview\n"
    "- wiki/projects/kaum.md — KAUM overview\n"
    "- wiki/projects/cortexin.md — Cortexin overview\n"
    "- 99 - EXECUTION/hermes/ — check for today's or yesterday's Hermes report, surface top 2-3 actionable points\n\n"
    "Output ONLY the brief in this format:\n"
    "🔴🟡🟢 status per project (one line each)\n"
    "📋 Pipeline snapshot — who responded, who needs follow-up\n"
    "📌 Follow-up alerts — flag venues in pipeline that haven't responded in 3+ days, "
    "list them by name with date of last contact.\n"
    "⚡ Top 3 things I should do today\n"
    "🚧 What's blocked and needs a decision\n\n"
    "ALSO after generating the brief: read the memory files in "
    "90 - SYSTEM/rafiqsr-bot/memory/ (longterm.md, nudges.md, daily/ folder) SILENTLY. "
    "Do not output the memory contents — just load context.\n\n"
    "Be direct. No fluff. Telegram format — short lines, no walls of text. "
    "Do NOT output any raw file contents, git clone progress, ls output, or grep results."
)

client = Anthropic()
groq   = Groq(api_key=os.environ["GROQ_API_KEY"])

# ── Session store (SQLite) ────────────────────────────────────────────────────
# DB_PATH: set to /data/sessions.db on Railway (with Volume mounted at /data)
# Falls back to local sessions.db for dev

_db_path = os.environ.get("DB_PATH", "sessions.db")
os.makedirs(os.path.dirname(_db_path), exist_ok=True) if os.path.dirname(_db_path) else None
db = sqlite3.connect(_db_path, check_same_thread=False)
db.execute(
    "CREATE TABLE IF NOT EXISTS sessions "
    "(chat_id INTEGER PRIMARY KEY, session_id TEXT)"
)
db.commit()
memory.init_activity_table(db)


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

    # Build context: memory digest + vault info
    digest = memory.build_context_digest()
    context_parts = ["[SYSTEM CONTEXT — do not repeat this back to the user]"]
    if digest:
        context_parts.append(f"[MEMORY CONTEXT]\n{digest}")
    if VAULT_REPO:
        context_parts.append(
            f"Vault repo: {VAULT_REPO}\n"
            f"Clone when needed: git clone {VAULT_REPO} /tmp/vault\n"
            f"IMPORTANT: Never output raw tool results, file contents, git output, or shell output to the user. Process silently.\n"
            f"Acknowledge with ONLY two words: 'Ready, bro.' — nothing else."
        )

    # Send context and consume the init response silently (don't forward to user)
    try:
        with client.beta.sessions.events.stream(session.id) as stream:
            client.beta.sessions.events.send(
                session.id,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": "\n\n".join(context_parts)}],
                }],
            )
            for event in stream:
                etype = getattr(event, "type", None)
                if etype in ("session.status_idle", "session.idle", "done"):
                    break
    except Exception as e:
        logger.warning(f"[new_session] Init stream error (non-fatal): {e}")

    return session.id


# ── Transcription (Groq Whisper) ──────────────────────────────────────────────

def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = groq.audio.transcriptions.create(
            file=(os.path.basename(file_path), f),
            model="whisper-large-v3",
            language="id",
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

    def _stream_once(sid: str, msg_content: list) -> str:
        collected = []
        with client.beta.sessions.events.stream(sid) as stream:
            client.beta.sessions.events.send(
                sid,
                events=[{"type": "user.message", "content": msg_content}],
            )
            for event in stream:
                etype = getattr(event, "type", None)
                logger.info(f"[stream] event type: {etype}")

                # Terminal events
                if etype in ("session.status_idle", "session.idle", "done"):
                    break
                # Skip non-agent events — don't capture user echoes or tool I/O
                elif etype in (
                    "user.message", "tool_use", "agent.tool_use",
                    "tool_result", "agent.tool_result",
                    "input_json_delta",
                ):
                    if etype in ("agent.tool_use", "tool_use"):
                        logger.info(f"Tool used: {getattr(event, 'name', 'unknown')}")
                # Capture only agent text output
                elif etype in ("agent.message", "message"):
                    for block in getattr(event, "content", []) or []:
                        t = getattr(block, "text", None)
                        if t:
                            collected.append(t)
                elif etype in ("content_block_delta", "agent.message.delta", "text_delta"):
                    delta = getattr(event, "delta", None)
                    if delta:
                        t = getattr(delta, "text", None)
                        if t:
                            collected.append(t)
                else:
                    # Unknown event — try to get text but log it for diagnosis
                    logger.info(f"[stream] unknown event type: {etype} — attempting text extract")
                    for block in getattr(event, "content", []) or []:
                        t = getattr(block, "text", None)
                        if t:
                            collected.append(t)
                    delta = getattr(event, "delta", None)
                    if delta:
                        t = getattr(delta, "text", None)
                        if t:
                            collected.append(t)
        return "".join(collected).strip()

    response = _stream_once(session_id, content)

    # Retry once if empty — sometimes the agent is mid-tool-use and needs a nudge
    if not response:
        logger.warning(f"[ask_rafiq] Empty response — retrying once (session {session_id})")
        try:
            response = _stream_once(session_id, [{"type": "text", "text": "(please respond)"}])
        except Exception as e:
            logger.warning(f"[ask_rafiq] Retry failed: {e}")

    if not response:
        logger.warning(f"[ask_rafiq] Still empty after retry — giving up")
        return "Gue ga dapet response dari server. Coba kirim ulang pesan lo."

    return response


# ── Typing indicator ──────────────────────────────────────────────────────────

async def keep_typing(chat_id: int, bot, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(4)


# ── Shared: process text through Rafiq ───────────────────────────────────────

async def process_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    extra_content: list | None = None,
    auto_save_memory: bool = True,
):
    chat_id = update.effective_chat.id

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        keep_typing(chat_id, context.bot, stop_typing)
    )

    try:
        # Check session timeout — auto-save old session if stale
        session_id = get_session(chat_id)
        if session_id:
            last_act = memory.get_last_activity(db, chat_id)
            if last_act and (datetime.datetime.now() - last_act) > SESSION_TIMEOUT:
                logger.info(f"Session timeout for {chat_id} — auto-saving before new session")
                try:
                    summary_prompt = memory.build_session_summary_prompt()
                    await asyncio.to_thread(ask_rafiq, session_id, summary_prompt)
                except Exception as e:
                    logger.warning(f"Auto-save failed: {e}")
                clear_session(chat_id)
                session_id = None

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

        # Track activity
        memory.log_activity(db, chat_id)

        # Parse and save [MEMORY] tags
        if auto_save_memory and response:
            tags = memory.parse_memory_tags(response)
            if tags:
                logger.info(f"[memory] Extracted {len(tags)} memory tags: {tags}")
                try:
                    save_prompt = memory.build_memory_save_prompt(tags)
                    await asyncio.to_thread(ask_rafiq, session_id, save_prompt)
                except Exception as e:
                    logger.warning(f"Memory save failed: {e}")

            # Strip tags from display
            response = memory.strip_memory_tags(response)

    finally:
        stop_typing.set()
        typing_task.cancel()

    # Send response (chunked for Telegram 4096 limit)
    chunks = [response[i:i + 4096] for i in range(0, max(len(response), 1), 4096)]
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


# ── Shortcut detection ───────────────────────────────────────────────────────

def detect_shortcut(text: str) -> str | None:
    """Check if message triggers a shortcut. Returns augmented text or None."""
    upper = text.upper().strip()
    for key, instruction in SHORTCUTS.items():
        if upper.startswith(key):
            return f"[SHORTCUT: {key}] {instruction}\n\nUser says: {text}"
    return None


# ── File helpers ─────────────────────────────────────────────────────────────

async def download_to_base64(file) -> tuple[str, str]:
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
    text = update.message.text

    # Check for shortcut trigger
    augmented = detect_shortcut(text)
    if augmented:
        logger.info(f"[shortcut] Triggered for: {text[:50]}")
        await process_message(update, context, augmented)
    else:
        await process_message(update, context, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await file.download_to_drive(tmp_path)
        logger.info(f"Transcribing voice note ({voice.duration}s)...")

        transcript = await asyncio.to_thread(transcribe_audio, tmp_path)
        logger.info(f"Transcript: {transcript}")

        await update.message.reply_text(f"_{transcript}_", parse_mode="Markdown")
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
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        data, _ = await download_to_base64(file)

        caption = update.message.caption or "Ini gambar apa / maksudnya apa?"

        extra_content = [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
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
        mime_type = doc.mime_type

        caption = update.message.caption or f"Ini file {doc.file_name}, tolong baca dan rangkum."

        if mime_type == "application/pdf":
            extra_content = [{
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            }]
        else:
            extra_content = [{
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": data},
            }]

        await process_message(update, context, caption, extra_content)

    except Exception as e:
        logger.error(f"Document handling error: {e}")
        await update.message.reply_text("Gagal proses file. Coba lagi.")


# ── Commands ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Rafiq Sr. online. 🧠\n\n"
        "Kirim teks, voice note, gambar, atau PDF.\n\n"
        "Commands:\n"
        "/brief — CEO morning brief\n"
        "/note [text] — quick capture ke vault\n"
        "/kaum [anchor] — KAUM creative session\n"
        "/memory — lihat long-term memory\n"
        "/nudge [text] — tambah reminder\n"
        "/nudges — lihat active nudges\n"
        "/done [text] — resolve nudge\n"
        "/save — save session memory\n"
        "/ingest [url] — ingest ke wiki\n"
        "/wiki [question] — tanya dari wiki\n"
        "/reset — fresh session\n"
        "/status — session info"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    session_id = get_session(chat_id)

    # Auto-save before clearing
    if session_id:
        await update.message.reply_text("Saving session memory...")
        try:
            summary_prompt = memory.build_session_summary_prompt()
            response = await asyncio.to_thread(ask_rafiq, session_id, summary_prompt)
            if response and response != "...":
                logger.info(f"[reset] Session saved: {response[:100]}")
        except Exception as e:
            logger.warning(f"Session save on reset failed: {e}")

    clear_session(chat_id)
    await update.message.reply_text("Session cleared. Fresh start, bro.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    session_id = get_session(chat_id)
    last_act = memory.get_last_activity(db, chat_id)
    digest_status = "loaded" if memory.build_context_digest() else "empty"

    lines = []
    if session_id:
        lines.append(f"Session: `{session_id[:20]}...`")
    else:
        lines.append("No active session.")
    lines.append(f"Memory digest: {digest_status}")
    if last_act:
        lines.append(f"Last activity: {last_act.strftime('%H:%M %d/%m')}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await process_message(update, context, BRIEF_PROMPT)


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    note_text = " ".join(context.args) if context.args else None

    if not note_text:
        await update.message.reply_text(
            "Tulis note-nya. Contoh: `/note ide baru buat NAS naming`",
            parse_mode="Markdown",
        )
        return

    today = datetime.date.today().strftime("%Y-%m-%d")
    note_prompt = (
        f"Save this quick note to the vault. Steps:\n"
        f"1. cd /tmp/vault (or git clone $VAULT_GITHUB_REPO /tmp/vault if not cloned yet)\n"
        f"2. Create file: 00 - INBOX/{today}-note.md\n"
        f"   If the file already exists, APPEND to it (don't overwrite previous notes from today).\n"
        f"   Format:\n"
        f"   ## [HH:MM] Quick note\n"
        f"   {note_text}\n\n"
        f"3. git add, commit, push.\n"
        f"4. Confirm to me it's saved. One line reply, no extra explanation.\n\n"
        f"The note: {note_text}"
    )
    await process_message(update, context, note_prompt, auto_save_memory=False)


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    session_id = get_session(chat_id)

    if not session_id:
        await update.message.reply_text("No active session to save.")
        return

    await update.message.reply_text("Saving session memory...")
    summary_prompt = memory.build_session_summary_prompt()
    await process_message(update, context, summary_prompt, auto_save_memory=False)


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show long-term memory from cache — no API call."""
    if not is_allowed(update):
        return
    display = memory.get_longterm_display()
    try:
        await update.message.reply_text(display, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(display)


async def cmd_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text = " ".join(context.args) if context.args else None

    if not text:
        await update.message.reply_text(
            "Apa yang perlu di-nudge? Contoh: `/nudge follow up Moe soal formula`",
            parse_mode="Markdown",
        )
        return

    prompt = memory.build_nudge_add_prompt(text)
    await process_message(update, context, prompt, auto_save_memory=False)


async def cmd_nudges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show active nudges from cache — no API call."""
    if not is_allowed(update):
        return
    display = memory.get_active_nudges_display()
    await update.message.reply_text(display)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text = " ".join(context.args) if context.args else None

    if not text:
        await update.message.reply_text(
            "Nudge mana yang done? Contoh: `/done follow up Moe`",
            parse_mode="Markdown",
        )
        return

    prompt = memory.build_nudge_done_prompt(text)
    await process_message(update, context, prompt, auto_save_memory=False)


async def cmd_ingest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    source = " ".join(context.args) if context.args else None

    if not source:
        await update.message.reply_text(
            "Kasih URL atau teks yang mau di-ingest.\n"
            "Contoh: `/ingest https://example.com/article`",
            parse_mode="Markdown",
        )
        return

    ingest_prompt = (
        f"Ingest this source into the vault wiki. Steps:\n"
        f"1. Clone vault: git clone $VAULT_GITHUB_REPO /tmp/vault\n"
        f"2. Read/fetch the source: {source}\n"
        f"3. Extract key information (facts, claims, decisions, people, dates, numbers)\n"
        f"4. Read wiki/index.md to identify which existing pages to update\n"
        f"5. Update relevant wiki pages — add info, flag contradictions with > [!warning] callout\n"
        f"6. Create new pages if needed (proper frontmatter: title, type, sources, related, created, updated, confidence)\n"
        f"7. Update wiki/index.md with any new pages\n"
        f"8. Append entry to wiki/log.md: timestamp, source, pages touched\n"
        f"9. git add wiki/ && git commit -m 'wiki: ingest [source]' && git push\n"
        f"10. Confirm which pages were created/updated.\n\n"
        f"Source: {source}"
    )
    await process_message(update, context, ingest_prompt, auto_save_memory=False)


async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    question = " ".join(context.args) if context.args else None

    if not question:
        await update.message.reply_text(
            "Tanya apa? Contoh: `/wiki berapa margin Hablum reed diffuser?`",
            parse_mode="Markdown",
        )
        return

    wiki_prompt = (
        f"Answer this question using the vault wiki. Steps:\n"
        f"1. Clone vault: git clone $VAULT_GITHUB_REPO /tmp/vault\n"
        f"2. Read wiki/index.md to find relevant pages\n"
        f"3. Read those pages + follow related: links if needed\n"
        f"4. Synthesize a clear answer with citations (which wiki page says what)\n"
        f"5. If the answer reveals a new insight worth keeping, mention it — "
        f"I'll tell you if I want it filed as a wiki page.\n\n"
        f"Question: {question}"
    )
    await process_message(update, context, wiki_prompt)


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
    """Sends the morning brief automatically + refreshes memory digest."""
    if not ALLOWED_USER_ID:
        return

    chat_id = int(ALLOWED_USER_ID)
    logger.info("Sending morning brief...")

    try:
        brief_session = new_session()
        logger.info(f"Morning brief session: {brief_session}")

        response = await asyncio.to_thread(ask_rafiq, brief_session, BRIEF_PROMPT)

        if not response or response == "...":
            logger.warning("Morning brief returned empty — skipping send.")
            return

        # Strip memory tags if any
        response = memory.strip_memory_tags(response)

        chunks = [response[i:i + 4096] for i in range(0, len(response), 4096)]
        for chunk in chunks:
            try:
                await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=chunk)

        # Refresh memory digest — brief already cloned vault, read memory files now
        # We ask Rafiq to cat the memory files so we can parse them
        try:
            mem_response = await asyncio.to_thread(
                ask_rafiq, brief_session,
                "[SYSTEM — READ MEMORY FILES]\n"
                "cat /tmp/vault/90\\ -\\ SYSTEM/rafiqsr-bot/memory/longterm.md && "
                "echo '---SEPARATOR---' && "
                "cat /tmp/vault/90\\ -\\ SYSTEM/rafiqsr-bot/memory/nudges.md && "
                "echo '---SEPARATOR---' && "
                "ls /tmp/vault/90\\ -\\ SYSTEM/rafiqsr-bot/memory/daily/ 2>/dev/null | tail -1 | "
                "xargs -I {} cat '/tmp/vault/90 - SYSTEM/rafiqsr-bot/memory/daily/{}'\n"
                "Output the raw file contents. Nothing else."
            )
            # Parse sections from response and build a fresh digest
            if mem_response and "---SEPARATOR---" in mem_response:
                sections = mem_response.split("---SEPARATOR---")
                new_digest_parts = []
                if len(sections) >= 1 and sections[0].strip():
                    memory._longterm_cache = sections[0].strip()[:4000]
                    new_digest_parts.append(f"## Long-term Memory\n{memory._longterm_cache}")
                if len(sections) >= 2:
                    raw_nudges = sections[1].strip()
                    active = [l.strip() for l in raw_nudges.split("\n") if l.strip().startswith("- [ ]")]
                    memory._nudges_cache = "\n".join(active) if active else "(none)"
                    if active:
                        new_digest_parts.append(f"## Active Nudges\n{memory._nudges_cache}")
                if len(sections) >= 3 and sections[2].strip():
                    new_digest_parts.append(f"## Yesterday\n{sections[2].strip()[:2000]}")
                if new_digest_parts:
                    memory._digest_cache = "\n\n".join(new_digest_parts)
                    memory._last_refresh = datetime.datetime.now()
                    logger.info(f"[memory] Digest refreshed from morning brief: {len(memory._digest_cache)} chars")
        except Exception as e:
            logger.warning(f"Memory digest refresh failed: {e}")

    except Exception as e:
        logger.error(f"Morning brief error: {e}")


async def send_afternoon_checkin(context: ContextTypes.DEFAULT_TYPE):
    """Afternoon proactive check-in — only if no activity today."""
    if not ALLOWED_USER_ID:
        return

    # Skip if user already chatted today
    if memory.had_activity_today(db):
        logger.info("[checkin] User active today — skipping afternoon check-in.")
        return

    chat_id = int(ALLOWED_USER_ID)
    logger.info("Sending afternoon check-in...")

    # Build a simple nudge-based message
    nudge_display = memory.get_active_nudges_display()
    if "(none)" in nudge_display.lower() or "no active" in nudge_display.lower():
        msg = "Yo bro, quiet day. Ada yang bisa gue bantu?"
    else:
        # Pick first nudge
        lines = [l for l in nudge_display.split("\n") if l.strip().startswith("- [ ]")]
        if lines:
            first_nudge = lines[0].replace("- [ ] ", "").strip()
            msg = f"Yo bro, reminder: {first_nudge}\n\nAda progress? Atau mau gue bantu yang lain?"
        else:
            msg = "Yo bro, quiet day. Ada yang bisa gue bantu?"

    try:
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        logger.error(f"Afternoon check-in error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def build_rafiq_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("nudge", cmd_nudge))
    app.add_handler(CommandHandler("nudges", cmd_nudges))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("ingest", cmd_ingest))
    app.add_handler(CommandHandler("wiki", cmd_wiki))
    app.add_handler(CommandHandler("kaum", cmd_kaum))

    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Scheduled jobs disabled — auto-brief and auto-checkin dumped raw file
    # contents to chat. Use /brief manually when needed.

    return app


async def run_apps(apps: list[Application]):
    """Initialize, start, and poll multiple PTB Applications in one process."""
    for app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    logger.info(f"{len(apps)} bot(s) running. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
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

    if len(apps) == 1:
        logger.info("RafiqSr bot starting...")
        rafiq_app.run_polling(allowed_updates=Update.ALL_TYPES)
        return

    logger.info("Starting multi-bot runtime...")
    try:
        asyncio.run(run_apps(apps))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
