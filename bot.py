#!/usr/bin/env python3
"""
Telegram multi-language runner (Option A runtimes inside container).
Supports: python, javascript (node), cpp, java, php.
Auto-detects language heuristically.

ENV required:
 - TELEGRAM_TOKEN (string)
 - OWNER_ID (int)
 - CHANNEL_LINK (string, optional)
 - PUBLIC_MODE ("on" or "off") default "on"

Warning: running public code execution is risky. Monitor usage.
"""

import os
import logging
import sqlite3
import time
import html
import tempfile
import subprocess
import shlex
import pathlib
import sys
import urllib.parse
import re
from typing import Optional, Tuple, List

# resource for setting rlimits (Unix)
import resource

import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, constants
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Config from env
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w")
OWNER_ID = int(os.environ.get("OWNER_ID", "8470120636") or 8470120636)
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "http://lofiBots.t.me")
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "on").lower()  # "on" or "off"

if not TELEGRAM_TOKEN or OWNER_ID == 0:
    print("ERROR: Set TELEGRAM_TOKEN and OWNER_ID environment variables.", file=sys.stderr)
    # Exit so Docker shows failure early
    raise SystemExit("Missing TELEGRAM_TOKEN or OWNER_ID")

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# DB
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
        deps TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    # set public_mode according to env if not set
    cur = c.execute("SELECT value FROM settings WHERE key = 'public_mode'").fetchone()
    if not cur:
        c.execute("INSERT INTO settings (key, value) VALUES ('public_mode', ?)", (PUBLIC_MODE,))
    conn.commit()
    conn.close()

init_db()

def get_setting(key: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def is_public_mode() -> bool:
    v = get_setting("public_mode")
    return (v == "on")

def save_run(user_id: int, username: str, lang: str, code: str, stdout: str, stderr: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO runs (user_id, username, lang, code, stdout, stderr, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (user_id, username or "", lang, code, stdout or "", stderr or "", int(time.time())))
    conn.commit()
    conn.close()

def get_user_history(user_id: int, limit: int = 50) -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, lang, code, stdout, stderr, created_at FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    conn.close()
    return rows

def add_user_deps(user_id: int, deps: List[str]):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT deps FROM user_deps WHERE user_id = ?", (user_id,)).fetchone()
    if row and row[0]:
        existing = row[0].split()
        merged = sorted(set(existing + deps))
        deps_str = " ".join(merged)
        conn.execute("UPDATE user_deps SET deps = ? WHERE user_id = ?", (deps_str, user_id))
    else:
        deps_str = " ".join(deps)
        conn.execute("INSERT OR REPLACE INTO user_deps (user_id, deps) VALUES (?, ?)", (user_id, deps_str))
    conn.commit()
    conn.close()
    return deps_str

def get_user_deps(user_id: int) -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT deps FROM user_deps WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row and row[0]:
        return row[0].split()
    return []

def format_history_rows(rows: List[Tuple]) -> str:
    lines = []
    for r in rows:
        rid, lang, code, stdout, stderr, created_at = r
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
        lines.append(f"#{rid} [{t}] lang={lang}\nCode:\n```\n{code[:1000]}\n```\nOutput:\n```\n{(stdout or '')[:1000]}\n```\n")
    return "\n".join(lines) if lines else "No history found."

# Simple heuristic language detector for Option A languages
def detect_language(code: str) -> str:
    # If user provided explicit ```lang block, we parse that before calling detector (done by caller).
    s = code.strip()
    # quick token checks (order matters)
    if s.startswith("#!") and "python" in s.splitlines()[0]:
        return "python"
    if re.search(r"<\?php", s) or s.strip().startswith("<?php"):
        return "php"
    if re.search(r"\bconsole\.log\b|\bprocess\.exit\b|\bmodule\.exports\b", s):
        return "javascript"
    if re.search(r"\bimport\s+sys\b|\bdef\s+\w+\(|\bprint\(|\bif __name__ == ['\"]__main__", s):
        return "python"
    if re.search(r"#include\s+<.*?>", s) or re.search(r"\bstd::\w+|\bcout\s*<<", s):
        return "cpp"
    if re.search(r"\bpublic\s+static\s+void\s+main\b|\bSystem\.out\.println\b|\bclass\s+\w+\s*{", s):
        return "java"
    if re.search(r"\bconsole\.log\b", s):
        return "javascript"
    # fallback by file-like markers
    if "{" in s and ";" in s and "int main" in s:
        return "cpp"
    # default fallback: python
    return "python"

# Execution helper with resource limits
def set_limits():
    # CPU seconds limit (soft, hard)
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))  # 5 seconds CPU
    except Exception:
        pass
    # Address space limit (virtual memory): 256MB
    try:
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    except Exception:
        pass
    # no core dumps
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass

def run_with_limits(cmd, cwd, timeout=8):
    """
    Run command (list) in cwd with resource limits set in child.
    Returns (returncode, stdout, stderr)
    """
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, preexec_fn=set_limits, text=True)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as te:
        return -1, te.stdout or "", (str(te) or "Timed out")
    except Exception as e:
        return -2, "", str(e)

# Map language to filename and commands
def prepare_and_run(language: str, code: str, user_id: int) -> Tuple[str, str]:
    """
    Prepares files, compiles if needed, runs, returns (stdout, stderr)
    """
    # prepare temp dir
    with tempfile.TemporaryDirectory() as td:
        td_path = pathlib.Path(td)
        # create file and determine steps
        lang = language.lower()
        if lang in ("py", "python"):
            filename = td_path / "main.py"
            filename.write_text(code, encoding="utf-8")
            # if user has deps, install them first (best-effort)
            deps = get_user_deps(user_id)
            if deps:
                # install via pip in same environment
                install_cmd = [sys.executable, "-m", "pip", "install"] + deps
                rc, out, err = run_with_limits(install_cmd, cwd=td_path, timeout=60)
                # ignore install errors, include in stderr later
                install_info = f"pip_install_rc={rc}\n{out}\n{err}\n"
            else:
                install_info = ""
            cmd = [sys.executable, str(filename)]
            rc, out, err = run_with_limits(cmd, cwd=td_path, timeout=6)
            full_stdout = (install_info + out)[:10000]
            full_stderr = err[:8000]
            return full_stdout, full_stderr

        elif lang in ("js", "javascript", "node"):
            filename = td_path / "main.js"
            filename.write_text(code, encoding="utf-8")
            cmd = ["node", str(filename)]
            rc, out, err = run_with_limits(cmd, cwd=td_path, timeout=6)
            return out[:10000], err[:8000]

        elif lang in ("cpp", "c++"):
            filename = td_path / "main.cpp"
            filename.write_text(code, encoding="utf-8")
            exe_path = td_path / "a.out"
            compile_cmd = ["g++", str(filename), "-O2", "-std=c++17", "-o", str(exe_path)]
            rc_c, out_c, err_c = run_with_limits(compile_cmd, cwd=td_path, timeout=10)
            if rc_c != 0:
                return "", f"Compile error:\n{err_c}\n{out_c}"
            # run
            cmd = [str(exe_path)]
            rc, out, err = run_with_limits(cmd, cwd=td_path, timeout=6)
            return out[:10000], err[:8000]

        elif lang == "java":
            # try to ensure class name is Main if possible; if not, user must provide class Main
            filename = td_path / "Main.java"
            filename.write_text(code, encoding="utf-8")
            compile_cmd = ["javac", str(filename)]
            rc_c, out_c, err_c = run_with_limits(compile_cmd, cwd=td_path, timeout=12)
            if rc_c != 0:
                return "", f"Java compile error:\n{err_c}\n{out_c}"
            run_cmd = ["java", "-cp", str(td_path), "Main"]
            rc, out, err = run_with_limits(run_cmd, cwd=td_path, timeout=8)
            return out[:10000], err[:8000]

        elif lang == "php":
            filename = td_path / "main.php"
            filename.write_text(code, encoding="utf-8")
            cmd = ["php", str(filename)]
            rc, out, err = run_with_limits(cmd, cwd=td_path, timeout=6)
            return out[:10000], err[:8000]

        else:
            return "", f"Unsupported language: {language}"

# Telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Join Channel", url=CHANNEL_LINK) if CHANNEL_LINK else InlineKeyboardButton("No channel set", callback_data="no_channel"),
        InlineKeyboardButton("Owner ID", callback_data="owner_id")
    ]])
    text = (
        f"Hi {html.escape(user.first_name or 'User')} 👋\n\n"
        "I run code for: python, javascript (node), c++ (g++), java, php.\n"
        "Send `/eval` with a code block (triple backticks) or inline: `/eval print(\"hi\")`.\n"
        "Auto-detection will try to pick the language.\n\n"
        "Use /history to see your runs. Owner can reply to a user's message and run /his to inspect history.\n"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=kb)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "owner_id":
        await query.edit_message_text(f"Owner ID: <code>{OWNER_ID}</code>", parse_mode=constants.ParseMode.HTML)
    else:
        await query.edit_message_text("No channel configured.")


def extract_code_from_message(msg_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    If message contains a triple-backtick codeblock with optional lang, extract it.
    Returns (language_hint, code)
    """
    if not msg_text:
        return None, None
    txt = msg_text.strip()
    # triple backticks block
    m = re.search(r"```(?:([^\n]+)\n)?(.*?)```", txt, flags=re.DOTALL)
    if m:
        lang_hint = m.group(1).strip() if m.group(1) else None
        code = m.group(2)
        return (lang_hint, code)
    # else inline: /eval python print('hi') handled elsewhere
    return None, None

async def eval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or str(user_id)

    # permission check
    if not is_public_mode() and user_id != OWNER_ID:
        await update.message.reply_text("Bot is currently private. Only the owner can run code.")
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=2)

    lang = None
    inline_code = None
    if len(parts) >= 2:
        # if user wrote: /eval python code
        if parts[1].lower() in ("python","py","javascript","js","cpp","c++","java","php"):
            lang = parts[1].lower()
            if len(parts) == 3:
                inline_code = parts[2]
    # Extract code block if present
    cb_lang, cb_code = extract_code_from_message(text)
    if cb_code:
        if cb_lang:
            lang = cb_lang.strip()
        code = cb_code
    else:
        code = inline_code

    if not code:
        await update.message.reply_text("Please provide code. Example:\n```\n/eval\n```python\nprint('hi')\n```\nOr inline: /eval python print('hi')", parse_mode=constants.ParseMode.MARKDOWN)
        return

    # If lang not explicitly given, auto-detect
    if not lang:
        detected = detect_language(code)
        lang = detected
    # normalize language keys
    mapping = {"py":"python","js":"javascript","c++":"cpp"}
    lang_norm = mapping.get(lang.lower(), lang.lower())

    # notify running
    running_msg = await update.message.reply_text(f"Detected language: {lang_norm}. Running...", parse_mode=constants.ParseMode.HTML)

    try:
        stdout, stderr = prepare_and_run(lang_norm, code, user_id)
    except Exception as e:
        log.exception("Execution failure")
        await running_msg.edit_text(f"Execution failed: {html.escape(str(e))}")
        return

    # save run
    save_run(user_id, username, lang_norm, code, stdout or "", stderr or "")

    # format output
    out_text = f"<b>Language:</b> {html.escape(lang_norm)}\n<b>User:</b> {html.escape(username)} (<code>{user_id}</code>)\n\n"
    out_text += "<b>Code:</b>\n<pre>" + html.escape(code[:1200]) + "</pre>\n\n"
    if stdout:
        out_text += "<b>Stdout:</b>\n<pre>" + html.escape(stdout[:4000]) + "</pre>\n\n"
    if stderr:
        out_text += "<b>Stderr:</b>\n<pre>" + html.escape(stderr[:4000]) + "</pre>\n\n"

    # share/copy link
    share_text = f"```{code}```"
    share_url = "https://t.me/share/url?text=" + urllib.parse.quote_plus(share_text)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Copy code to chat", url=share_url)]])
    await running_msg.edit_text(out_text, parse_mode=constants.ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = get_user_history(user.id, limit=50)
    if not rows:
        await update.message.reply_text("No runs recorded yet for you.")
        return
    text = format_history_rows(rows)
    if len(text) > 3000:
        # send as file
        import io
        bio = io.BytesIO(text.encode())
        bio.name = "history.txt"
        await update.message.reply_document(bio, filename=bio.name, caption="Your run history")
    else:
        await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)

async def his_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("Owner-only command.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message and run /his to see that user's history.")
        return
    target = update.message.reply_to_message.from_user
    rows = get_user_history(target.id, limit=1000)
    if not rows:
        await update.message.reply_text("No history for that user.")
        return
    text = format_history_rows(rows)
    if len(text) > 3000:
        import io
        bio = io.BytesIO(text.encode())
        bio.name = f"history_{target.id}.txt"
        await update.message.reply_document(bio, filename=bio.name, caption=f"History for {target.id}")
    else:
        await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)

async def in_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /in pip install pkg1 pkg2
    user = update.effective_user
    text = update.message.text or ""
    parts = text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /in pip install <pkg1> <pkg2> ... (Python-only)")
        return
    if parts[1].lower() != "pip" or parts[2].lower() != "install":
        await update.message.reply_text("Only `pip install ...` allowed via /in (Python dependencies).")
        return
    deps = parts[3:]
    if not deps:
        await update.message.reply_text("No packages specified.")
        return
    deps_str = add_user_deps(user.id, deps)
    await update.message.reply_text(f"Saved Python dependencies: `{deps_str}`", parse_mode=constants.ParseMode.MARKDOWN)

async def enable_public(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("Owner only.")
        return
    set_setting("public_mode", "on")
    await update.message.reply_text("Public mode enabled. Bot accepts /eval from anyone.")

async def disable_public(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("Owner only.")
        return
    set_setting("public_mode", "off")
    await update.message.reply_text("Public mode disabled. Only owner may run commands.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pm = "ON" if is_public_mode() else "OFF"
    await update.message.reply_text(f"Public mode: {pm}\nOwner ID: {OWNER_ID}")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /eval, /history, /in, /status.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("eval", eval_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("his", his_command))
    app.add_handler(CommandHandler("in", in_command))
    app.add_handler(CommandHandler("enable_public_mode", enable_public))
    app.add_handler(CommandHandler("disable_public_mode", disable_public))
    app.add_handler(CommandHandler("status", status_cmd))

    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_handler(MessageHandler(filters.Regex("^owner_id$"), handle_callback))

    log.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
