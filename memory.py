"""
Memory system for Rafiq Sr.
Memory lives in vault (GitHub) — browsable in Obsidian.
Bot caches a digest in-memory, refreshed daily during morning brief.
"""
import datetime
import logging
import os
import re
import sqlite3

logger = logging.getLogger(__name__)

# ── Vault paths (relative to cloned vault root) ─────────────────────────────
MEMORY_DIR = "90 - SYSTEM/rafiqsr-bot/memory"
DAILY_DIR = f"{MEMORY_DIR}/daily"
LONGTERM_FILE = f"{MEMORY_DIR}/longterm.md"
NUDGES_FILE = f"{MEMORY_DIR}/nudges.md"

# ── In-memory cache ──────────────────────────────────────────────────────────
_digest_cache: str = ""
_longterm_cache: str = ""
_nudges_cache: str = ""
_last_refresh: datetime.datetime | None = None


# ── Activity tracking (reuses existing sessions.db) ─────────────────────────

def init_activity_table(db: sqlite3.Connection):
    """Add activity tracking to existing sessions DB."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS activity "
        "(chat_id INTEGER PRIMARY KEY, last_ts TEXT)"
    )
    db.commit()


def log_activity(db: sqlite3.Connection, chat_id: int):
    now = datetime.datetime.now().isoformat()
    db.execute(
        "INSERT OR REPLACE INTO activity VALUES (?, ?)", (chat_id, now)
    )
    db.commit()


def get_last_activity(db: sqlite3.Connection, chat_id: int) -> datetime.datetime | None:
    row = db.execute(
        "SELECT last_ts FROM activity WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if row and row[0]:
        try:
            return datetime.datetime.fromisoformat(row[0])
        except ValueError:
            return None
    return None


def had_activity_today(db: sqlite3.Connection) -> bool:
    """Check if ANY user chatted today (for afternoon check-in logic)."""
    today = datetime.date.today().isoformat()
    row = db.execute(
        "SELECT COUNT(*) FROM activity WHERE last_ts >= ?", (today,)
    ).fetchone()
    return row[0] > 0 if row else False


# ── Digest builder ───────────────────────────────────────────────────────────

def build_context_digest() -> str:
    """Return cached digest (~3K tokens). Injected into every new session."""
    if _digest_cache:
        return _digest_cache
    # Fallback: return whatever we have cached individually
    parts = []
    if _longterm_cache:
        parts.append(f"## Long-term Memory\n{_longterm_cache}")
    if _nudges_cache:
        parts.append(f"## Active Nudges\n{_nudges_cache}")
    return "\n\n".join(parts) if parts else "(No memory loaded yet. Use /brief to refresh.)"


def refresh_digest_from_vault(vault_path: str):
    """Called after vault clone (during morning brief). Rebuilds in-memory cache."""
    global _digest_cache, _longterm_cache, _nudges_cache, _last_refresh

    parts = []

    # Read longterm memory
    lt_path = os.path.join(vault_path, LONGTERM_FILE)
    if os.path.exists(lt_path):
        with open(lt_path, "r", encoding="utf-8") as f:
            _longterm_cache = f.read().strip()
        # Truncate to ~4K chars (~1K tokens) to keep digest small
        lt_trimmed = _longterm_cache[:4000]
        parts.append(f"## Long-term Memory\n{lt_trimmed}")
    else:
        logger.info(f"[memory] longterm.md not found at {lt_path}")

    # Read yesterday's daily (for continuity)
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    daily_path = os.path.join(vault_path, DAILY_DIR, f"{yesterday}.md")
    if os.path.exists(daily_path):
        with open(daily_path, "r", encoding="utf-8") as f:
            yesterday_content = f.read().strip()
        # Truncate to ~2K chars
        yd_trimmed = yesterday_content[:2000]
        parts.append(f"## Yesterday ({yesterday})\n{yd_trimmed}")

    # Read nudges
    nudge_path = os.path.join(vault_path, NUDGES_FILE)
    if os.path.exists(nudge_path):
        with open(nudge_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        # Only include unchecked items
        active = [
            line.strip() for line in raw.split("\n")
            if line.strip().startswith("- [ ]")
        ]
        _nudges_cache = "\n".join(active) if active else "(none)"

        # Add overdue warnings
        nudge_lines = []
        for item in active:
            nudge_lines.append(item)
            # Check if created date is 3+ days ago
            date_match = re.search(r"created:\s*(\d{4}-\d{2}-\d{2})", item)
            if date_match:
                created = datetime.datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                days_old = (datetime.date.today() - created).days
                if days_old >= 3:
                    nudge_lines[-1] += f" ⚠️ {days_old} days old"

        parts.append(f"## Active Nudges (mention naturally when relevant)\n" + "\n".join(nudge_lines))
    else:
        _nudges_cache = "(none)"
        logger.info(f"[memory] nudges.md not found at {nudge_path}")

    # Read wiki overview for project snapshot (if exists, keep it brief)
    overview_path = os.path.join(vault_path, "wiki", "overview.md")
    if os.path.exists(overview_path):
        with open(overview_path, "r", encoding="utf-8") as f:
            overview = f.read().strip()
        # Just first 1500 chars
        parts.append(f"## Project Snapshot\n{overview[:1500]}")

    _digest_cache = "\n\n".join(parts)
    _last_refresh = datetime.datetime.now()
    logger.info(f"[memory] Digest refreshed: {len(_digest_cache)} chars, {len(parts)} sections")
    return _digest_cache


# ── [MEMORY] tag parser ──────────────────────────────────────────────────────

MEMORY_TAG_RE = re.compile(r"\[MEMORY:\s*(.+?)\]", re.IGNORECASE)


def parse_memory_tags(response: str) -> list[str]:
    """Extract [MEMORY: ...] tags from Claude's response."""
    return MEMORY_TAG_RE.findall(response)


def strip_memory_tags(response: str) -> str:
    """Remove [MEMORY: ...] tags from response before showing to user."""
    return MEMORY_TAG_RE.sub("", response).strip()


# ── Nudge parser ─────────────────────────────────────────────────────────────

def get_active_nudges_display() -> str:
    """Return nudges from cache for /nudges command (no API call)."""
    if _nudges_cache and _nudges_cache != "(none)":
        return f"📌 Active Nudges:\n\n{_nudges_cache}"
    return "📌 No active nudges. Add one with /nudge [text]"


def get_longterm_display() -> str:
    """Return longterm memory from cache for /memory command (no API call)."""
    if _longterm_cache:
        # Truncate for Telegram display
        display = _longterm_cache[:3500]
        return f"🧠 Long-term Memory:\n\n{display}"
    return "🧠 No memory loaded yet. Chat with me first or use /brief to refresh."


# ── Save commands (build prompts for Rafiq to execute) ───────────────────────

def build_memory_save_prompt(entries: list[str]) -> str:
    """Build a prompt for Rafiq to save memory entries to vault."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    entries_text = "\n".join(f"- {e}" for e in entries)
    return (
        f"[SYSTEM — AUTO-SAVE MEMORY]\n"
        f"Save these to the vault memory. Steps:\n"
        f"1. cd /tmp/vault (or git clone $VAULT_GITHUB_REPO /tmp/vault if needed)\n"
        f"2. Append to {DAILY_DIR}/{today}.md (create if doesn't exist):\n"
        f"   ## Session notes\n"
        f"{entries_text}\n\n"
        f"3. If any entry is a long-term fact/decision/preference, also add to {LONGTERM_FILE}\n"
        f"4. git add {MEMORY_DIR}/ && git commit -m 'rafiq: memory update' && git push\n"
        f"5. Reply ONLY: ✅ (nothing else)"
    )


def build_nudge_add_prompt(text: str) -> str:
    """Build a prompt for Rafiq to add a nudge to vault."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    return (
        f"[SYSTEM — ADD NUDGE]\n"
        f"Add this nudge to the vault. Steps:\n"
        f"1. cd /tmp/vault (or git clone $VAULT_GITHUB_REPO /tmp/vault if needed)\n"
        f"2. Append to {NUDGES_FILE}:\n"
        f"   - [ ] {text} (created: {today})\n"
        f"3. git add {NUDGES_FILE} && git commit -m 'rafiq: nudge added' && git push\n"
        f"4. Reply ONLY: ✅ Nudge added."
    )


def build_nudge_done_prompt(text: str) -> str:
    """Build a prompt for Rafiq to mark a nudge as done."""
    return (
        f"[SYSTEM — RESOLVE NUDGE]\n"
        f"Mark this nudge as done in the vault. Steps:\n"
        f"1. cd /tmp/vault (or git clone $VAULT_GITHUB_REPO /tmp/vault if needed)\n"
        f"2. In {NUDGES_FILE}, find the line matching '{text}' and change '- [ ]' to '- [x]'\n"
        f"3. git add {NUDGES_FILE} && git commit -m 'rafiq: nudge resolved' && git push\n"
        f"4. Reply ONLY: ✅ Done."
    )


def build_session_summary_prompt() -> str:
    """Build a prompt for Rafiq to summarize and save the session."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    return (
        f"Summarize this session in 3-5 bullet points. Focus on:\n"
        f"- Decisions made\n"
        f"- New information learned\n"
        f"- Commitments / next steps\n"
        f"Tag each important item with [MEMORY: ...]\n\n"
        f"Then save to vault:\n"
        f"1. cd /tmp/vault (or git clone $VAULT_GITHUB_REPO /tmp/vault)\n"
        f"2. Append summary to {DAILY_DIR}/{today}.md\n"
        f"3. Update {LONGTERM_FILE} with any new long-term facts\n"
        f"4. git add {MEMORY_DIR}/ && git commit -m 'rafiq: session summary' && git push\n"
        f"5. Confirm briefly what was saved."
    )
