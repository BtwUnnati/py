#!/usr/bin/env python3
"""
Telegram multi-language runner bot using a Piston-compatible execution API.
Features:
 - /start with inline buttons (channel, owner)
 - /eval <language> [code]  OR send code block after command
 - /history (user) and owner-only /his (reply to a user message)
 - /in pip install ...  (adds per-user python deps to be installed before running)
Uses sqlite for storing history and per-user dependencies.

SET THE FOLLOWING ENV:
 - TELEGRAM_TOKEN
 - OWNER_ID
 - CHANNEL_LINK
 - PISTON_URL (optional, default: https://emkc.org/api/v2/piston/execute)
"""

import os
import logging
import sqlite3
import time
import html
import urllib.parse
from typing import Optional, Tuple, List

import requests
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    constants,
)
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# config
TELEGRAM_TOKEN = os.environ.get("8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w")
OWNER_ID = int(os.environ.get("OWNER_ID", "8233966309"))
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "http://lofiBots.t.me")
PISTON_URL = os.environ.get("PISTON_URL", "https://emkc.org/api/v2/piston/execute")  # replace with your Piston URL if you have one

if not TELEGRAM_TOKEN or OWNER_ID == 0:
    raise RuntimeError("Please set TELEGRAM_TOKEN and OWNER_ID environment variables.")

# logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# sqlite
DB_PATH = "runner_bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        lang TEXT,
        code TEXT,
        stdout TEXT,
        stderr TEXT,
        created_at INTEGER
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS user_deps (
        user_id INTEGER PRIMARY KEY,
        deps TEXT -- space separated pip packages
    )""")
    conn.commit()
    conn.close()

init_db()

# helpers
def save_run(user_id: int, username: str, lang: str, code: str, stdout: str, stderr: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO runs (user_id, username, lang, code, stdout, stderr, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (user_id, username or "", lang, code, stdout or "", stderr or "", int(time.time())))
    conn.commit()
    conn.close()

def get_user_history(user_id: int, limit: int = 20) -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, lang, code, stdout, stderr, created_at FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_history_for_user_id(user_id: int, limit: int = 1000) -> List[Tuple]:
    return get_user_history(user_id, limit)

def add_user_deps(user_id: int, deps: List[str]):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    existing = c.execute("SELECT deps FROM user_deps WHERE user_id = ?", (user_id,)).fetchone()
    if existing and existing[0]:
        merged = existing[0].split() + deps
        merged = sorted(set(merged))
        deps_str = " ".join(merged)
        c.execute("UPDATE user_deps SET deps = ? WHERE user_id = ?", (deps_str, user_id))
    else:
        deps_str = " ".join(deps)
        c.execute("INSERT OR REPLACE INTO user_deps (user_id, deps) VALUES (?, ?)", (user_id, deps_str))
    conn.commit()
    conn.close()
    return deps_str

def get_user_deps(user_id: int) -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute("SELECT deps FROM user_deps WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row and row[0]:
        return row[0].split()
    return []

def format_history_rows(rows: List[Tuple]) -> str:
    out_lines = []
    for r in rows:
        rid, lang, code, stdout, stderr, created_at = r
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
        out_lines.append(f"#{rid} [{t}] lang={lang}\nCode:\n```\n{code[:800]}\n```\nOutput:\n```\n{(stdout or '')[:800]}\n```\n")
    return "\n".join(out_lines) if out_lines else "No history found."

# Piston calling
def call_piston(language: str, code: str, stdin: Optional[str] = None, filename: Optional[str] = None) -> Tuple[str, str]:
    """
    Calls the Piston API. Returns (stdout, stderr).
    Note: The public Piston endpoint and API shape may vary. This function assumes the common
    /execute endpoint with JSON {language, version?, files: [{name, content}], stdin}
    """
    # Prepare payload: try the common Piston format.
    payload = {
        "language": language,
        "source": code
    }
    # some Piston instances expect 'files' instead. We'll try /execute with 'language' and 'source'.
    try:
        resp = requests.post(PISTON_URL, json=payload, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        # Try alternative shape (older Piston clones)
        alt_payload = {
            "language": language,
            "files": [{"name": filename or "main", "content": code}],
            "stdin": stdin or ""
        }
        try:
            resp = requests.post(PISTON_URL, json=alt_payload, timeout=30)
            resp.raise_for_status()
        except Exception as e2:
            log.exception("Piston call failed")
            raise RuntimeError(f"Piston call failed: {e} / {e2}")
    data = resp.json()
    # Piston responses vary. Try to extract common fields.
    stdout = ""
    stderr = ""
    # If engine returns 'run' with 'stdout' etc:
    if isinstance(data, dict):
        if "output" in data:
            # some endpoints return merged output
            stdout = data.get("output") or ""
        elif "run" in data:
            run = data.get("run") or {}
            stdout = run.get("stdout") or run.get("output") or ""
            stderr = run.get("stderr") or ""
        else:
            # older: data might contain stdout/stderr top level
            stdout = data.get("stdout") or data.get("output") or ""
            stderr = data.get("stderr") or ""
    elif isinstance(data, str):
        stdout = data
    else:
        stdout = str(data)
    return stdout, stderr

# telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Join Channel", url=CHANNEL_LINK),
            InlineKeyboardButton("Owner", url=f"https://t.me/{context.bot.get_me().username}?start=owner") if False else InlineKeyboardButton("Owner ID", callback_data="owner_id")
        ]
    ])
    text = (
        f"Hi {html.escape(user.first_name or 'User')} 👋\n\n"
        "Main multi-language code runner bot hoon.\n\n"
        "Use /eval <language> then send code block, ya ek line me: `/eval python print(\"hi\")`\n"
        "Use /history to see your runs. Owner can reply to a user message and send /his to see that user's history.\n\n"
        "Note: For Python-specific packages, use `/in pip install <pkg1> <pkg2>` to add per-user deps.\n"
    )
    # send start message
    sent = await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=kb)

async def handle_owner_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # simple callback to show owner id
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(f"Owner ID: <code>{OWNER_ID}</code>", parse_mode=constants.ParseMode.HTML)

def extract_code_from_message(msg_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    If message contains a triple-backtick codeblock with optional lang, extract it.
    Returns (language_hint, code) where language_hint may be None.
    """
    if not msg_text:
        return None, None
    txt = msg_text.strip()
    # triple backticks
    if txt.startswith("```") and "```" in txt[3:]:
        # format: ```lang\ncode\n```
        try:
            inside = txt[3:txt.rfind("```")]
            # if first line has lang
            lines = inside.splitlines()
            if lines:
                first = lines[0].strip()
                # if first looks like a language (no spaces), treat as lang
                if first and " " not in first and len(first) <= 20 and not first.endswith(";"):
                    lang = first
                    code = "\n".join(lines[1:]) if len(lines) > 1 else ""
                    return lang, code
            # otherwise no language hint
            return None, inside
        except Exception:
            return None, None
    return None, None

async def eval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
    /eval python
    <code block>

    or
    /eval python print('hi')

    If user sends code block with language inside triple-backticks, bot will detect.
    """
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or str(user_id)
    text = update.message.text or ""
    parts = text.split(maxsplit=2)
    lang = None
    inline_code = None

    if len(parts) >= 2:
        lang = parts[1].strip()
        if len(parts) == 3:
            inline_code = parts[2].strip()

    # If message also contains a code block, extract from it
    cb_lang, cb_code = extract_code_from_message(text)
    if cb_code:
        # prefer codeblock content
        if cb_lang:
            lang = cb_lang
        code = cb_code
    else:
        # if inline_code present, use that; else maybe next message has code (we won't wait)
        code = inline_code

    if not lang:
        await update.message.reply_text("Please specify the language. Example: `/eval python` and then send code block. Supported: python, cpp, java, javascript, php, go, rust, etc.", parse_mode=constants.ParseMode.MARKDOWN)
        return

    if not code:
        await update.message.reply_text("Please provide code (either inline after language or as a triple-backtick code block). Example:\n```\n/eval python\nprint('hello')\n```", parse_mode=constants.ParseMode.MARKDOWN)
        return

    # Prepend user deps for python if any
    if lang.lower() in ("python", "py"):
        deps = get_user_deps(user_id)
        if deps:
            # Prepend pip installs to code (this will run inside the execution container). This is slower but isolates host.
            pip_lines = "\n".join([f"import sys, subprocess\nsubprocess.run([sys.executable, '-m', 'pip', 'install', '{d}'], check=False)" for d in deps])
            code = pip_lines + "\n\n" + code

    # Call piston
    msg = await update.message.reply_text(f"Running your {html.escape(lang)} code...", parse_mode=constants.ParseMode.HTML)
    try:
        stdout, stderr = call_piston(lang, code)
    except Exception as e:
        log.exception("Execution failed")
        await msg.edit_text(f"Execution failed: {html.escape(str(e))}")
        return

    # Save in DB (store original code without pip preamble if we added it)
    # If we prepended pip lines above, remove them when saving for clarity
    saved_code = code
    if lang.lower() in ("python", "py"):
        deps = get_user_deps(user_id)
        if deps:
            # try to remove the pip preamble from saved_code: remove everything before original code's first line
            # Best-effort: assume original code present at the end
            if "\n\n" in saved_code:
                saved_code = saved_code.split("\n\n", 1)[1]

    save_run(user_id, username, lang, saved_code, stdout or "", stderr or "")

    # Format output
    out_text = f"<b>Language:</b> {html.escape(lang)}\n"
    out_text += f"<b>User:</b> {html.escape(username)} (<code>{user_id}</code>)\n\n"
    out_text += "<b>Code</b>:\n<pre>" + html.escape(saved_code[:1000]) + "</pre>\n\n"
    if stdout:
        out_text += "<b>Stdout:</b>\n<pre>" + html.escape(stdout[:4000]) + "</pre>\n\n"
    if stderr:
        out_text += "<b>Stderr:</b>\n<pre>" + html.escape(stderr[:4000]) + "</pre>\n\n"

    # Add a button to "copy code" using Telegram share link (pre-fills text) - best-effort
    share_text = f"```{saved_code}```"
    share_url = "https://t.me/share/url?text=" + urllib.parse.quote_plus(share_text)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy code to chat", url=share_url)]
    ])
    await msg.edit_text(out_text, parse_mode=constants.ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    rows = get_user_history(user_id, limit=20)
    if not rows:
        await update.message.reply_text("You have no recorded runs yet.")
        return
    text = format_history_rows(rows)
    # send as file if too long
    if len(text) > 3500:
        # write to file and send
        import io
        bio = io.BytesIO(text.encode())
        bio.name = "history.txt"
        await update.message.reply_document(bio, filename="history.txt", caption="Your history (full).")
    else:
        await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)

async def his_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only. If owner replies to a user's message and sends /his, show that user's history.
    """
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("This command is owner-only.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message and run /his to see their history.")
        return
    target = update.message.reply_to_message.from_user
    rows = get_all_history_for_user_id(target.id, limit=1000)
    if not rows:
        await update.message.reply_text(f"No history found for {target.id}.")
        return
    text = format_history_rows(rows)
    if len(text) > 3500:
        import io
        bio = io.BytesIO(text.encode())
        bio.name = f"history_{target.id}.txt"
        await update.message.reply_document(bio, filename=bio.name, caption=f"Full history for {target.id}")
    else:
        await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)

async def in_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /in pip install package1 package2
    Adds per-user python deps that will be installed automatically before running python code.
    Rejects apt/host installs.
    """
    user = update.effective_user
    user_id = user.id
    text = update.message.text or ""
    parts = text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /in pip install <pkg1> <pkg2> ...\nNote: apt / system package installs are not allowed via bot for safety.")
        return
    if parts[1].lower() != "pip" or parts[2].lower() != "install":
        await update.message.reply_text("Only `pip install ...` is allowed via /in for Python dependencies. System package managers (apt, yum) are not allowed.")
        return
    deps = parts[3:]
    if not deps:
        await update.message.reply_text("No packages specified.")
        return
    deps_str = add_user_deps(user_id, deps)
    await update.message.reply_text(f"Saved Python dependencies for you: `{deps_str}`\nThey will be installed automatically before running your Python code in the execution container.", parse_mode=constants.ParseMode.MARKDOWN)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /eval, /history, /in. See /start for help.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("eval", eval_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("his", his_command))
    app.add_handler(CommandHandler("in", in_command))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    # callback for owner id button (simple)
    app.add_handler(MessageHandler(filters.Regex("^owner_id$"), handle_owner_id_callback))

    log.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
