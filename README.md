# RafiqSr Bot

Telegram interface to Claude Managed Agents. Rafiq Sr. runs on Anthropic's infra, reads your vault from GitHub, and is always in your pocket.

---

## Setup (do this once)

### 1. Vault → GitHub

Push your Obsidian Vault to a **private** GitHub repo.

```bash
cd "E:/Documents/Obsidian Vault"
git init
git remote add origin git@github.com:yourusername/obsidian-vault.git
git add wiki/ raw/ CLAUDE.md
git commit -m "init vault"
git push -u origin main
```

Add a `.gitignore` to exclude large/private files:
```
20 - AREAS/
*.mp3
*.wav
*.mp4
node_modules/
```

### 2. Create a GitHub token for the agent

Go to GitHub → Settings → Developer Settings → Personal Access Tokens → Fine-grained token.
Give it **read access** to your vault repo only. Copy the token.

Your vault URL becomes:
```
https://YOUR_USERNAME:YOUR_TOKEN@github.com/YOUR_USERNAME/obsidian-vault.git
```

### 3. Install dependencies

```bash
cd "90 - SYSTEM/rafiqsr-bot"
pip install -r requirements.txt
```

### 4. Set up .env

```bash
cp .env.example .env
# Fill in all values
```

To get your Telegram user ID: message **@userinfobot** on Telegram.

### 5. Create the agent + environment

```bash
python setup.py
```

Copy the printed `AGENT_ID` and `ENVIRONMENT_ID` into your `.env`.

### 6. Run locally (test)

```bash
python bot.py
```

Message your bot on Telegram. It should respond.

---

## Deploy to Railway

1. Push this folder to its own GitHub repo (separate from the vault)
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add all `.env` values as Railway environment variables
4. Railway will detect the `Procfile` and run `python bot.py`

Free tier handles this bot easily.

---

## Commands

| Command | Action |
|---|---|
| `/start` | Wake up Rafiq |
| `/reset` | Clear session (fresh context) |
| `/status` | Show current session ID |

---

## How the vault works

Rafiq's system prompt tells him to clone your vault repo when he needs deep context.
You can also explicitly ask: *"Clone the vault and check wiki/projects/hablum.md"*

For Rafiq to push changes back (e.g. updating wiki), he needs write access on the GitHub token.

---

## Architecture

```
Telegram → bot.py → Managed Agents Session
                         ├── Rafiq Sr. system prompt
                         ├── Tools: bash, file ops, web search
                         └── Vault: git clone from GitHub
```

Sessions persist per Telegram chat. Use `/reset` to start fresh.
