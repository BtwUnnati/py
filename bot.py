#!/usr/bin/env python3
"""
Final Eval Bot
- Uses python-telegram-bot (async)
- Executes code via Piston API
- Shows result in styled message (language header + pre block)
- Adds COPY CODE button (uses switch_inline_query_current_chat)
- No intermediate "running" message
"""

import asyncio
import html
import logging
from typing import Optional, Tuple

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# -------------------------
# CONFIG
# -------------------------
BOT_TOKEN = "8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w"  # your temporary token
OWNER_USERNAME = "@CodeSynDev"
CHANNEL_LINK = "http://lofiBots.t.me"

PISTON_API = "https://emkc.org/api/v2/piston/execute"
MAX_CONCURRENT_RUNS = 6

# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("evalbot")

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


# -------------------------
# Helpers: parse input + language detection
# -------------------------
def clean_code(text: str) -> str:
    """
    Remove command prefix, triple backticks and language fences,
    and left/trailing blank lines. Keep the user's exact code otherwise.
    """
    t = text.strip()

    # remove leading /eval and optional botname (handle '/eval@BotName')
    if t.lower().startswith("/eval"):
        t = t.partition(" ")[2] if " " in t else t.replace("/eval", "", 1).strip()

    # remove triple backticks and surrounding language marker if present
    if t.startswith("```") and "```" in t[3:]:
        inner = t[3:]
        # if first line is like "python\n"
        if "\n" in inner:
            first_line, rest = inner.split("\n", 1)
            # body until closing ```
            body = rest.rsplit("```", 1)[0]
            # if first_line is just a language token, return body
            return body.strip()
        else:
            # fallback: strip all backticks
            return inner.replace("```", "").strip()

    # remove inline code fences like `code`
    t = t.replace("`", "")

    return t.strip()


def detect_language(code: str) -> str:
    s = code.strip().lower()
    # quick heuristics
    if s.startswith(("js ", "javascript ")) or "console.log(" in s or s.startswith("function "):
        return "javascript"
    if s.startswith(("c++", "cpp")) or "#include" in s or "std::" in s:
        return "c++"
    if s.startswith("<?php") or s.startswith("php "):
        return "php"
    if "public static void main" in s or "class " in s and "System.out" in s:
        return "java"
    # default python
    return "python"


# -------------------------
# Execute via Piston (async)
# -------------------------
async def run_piston(code: str, language: str, timeout: int = 20) -> str:
    payload = {"language": language, "version": "*", "files": [{"content": code}]}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(PISTON_API, json=payload)
        resp.raise_for_status()
        data = resp.json()
        # Piston often returns run.stdout and run.stderr
        run = data.get("run") or {}
        stdout = run.get("stdout") or ""
        stderr = run.get("stderr") or ""
        # Some installations use 'output'
        if not (stdout or stderr):
            stdout = data.get("output", "") or stdout
            stderr = data.get("stderr", "") or stderr
        output = (stdout or "") + (stderr or "")
        if not output:
            return "✅ Finished (no output)"
        return output


# -------------------------
# Telegram Handlers
# -------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Owner 👑", url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("Channel 📢", url=CHANNEL_LINK)]
    ])
    await update.message.reply_text(
        "Hello — send `/eval <code>` to run code.\nExamples:\n"
        "`/eval print('hi')`\nOr send a fenced block:\n"
        "```/eval\n```python\nprint('hi')\n```",
        parse_mode="Markdown",
        reply_markup=kb
    )


async def eval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clean and parse code
    raw_text = update.message.text or ""
    code = clean_code(raw_text)

    if not code:
        # keep this short — user asked to not show extra chatter; but still give feedback
        return await update.message.reply_text("⚠️ Provide some code after /eval", parse_mode="Markdown")

    # If first token is an explicit language prefix like "python print('hi')", handle it
    parts = code.split(None, 1)
    user_lang = None
    if len(parts) >= 2 and parts[0].lower() in ("python", "py", "javascript", "js", "c", "cpp", "c++", "java", "php"):
        token = parts[0].lower()
        lang_map = {"py": "python", "python": "python", "js": "javascript", "javascript": "javascript",
                    "c": "c", "cpp": "c++", "c++": "c++", "java": "java", "php": "php"}
        user_lang = lang_map.get(token, token)
        code = parts[1]  # remaining as actual code

    # detect if not provided
    language = user_lang or detect_language(code)

    # Run code (no intermediate "Running..." message)
    try:
        async with _semaphore:
            output = await run_piston(code, language)
    except httpx.RequestError as e:
        logger.exception("Piston HTTP error")
        return await update.message.reply_text(f"⚠ Executor unreachable: {html.escape(str(e))}", parse_mode="HTML")
    except Exception as e:
        logger.exception("Unexpected execution error")
        return await update.message.reply_text(f"⚠ Execution error: {html.escape(str(e))}", parse_mode="HTML")

    # Prepare styled reply (close to your screenshot)
    safe_output = html.escape(output)
    # Truncate very long outputs to avoid Telegram limits
    MAX = 3500
    if len(safe_output) > MAX:
        safe_output = safe_output[: MAX - 200] + "\n\n...output truncated..."

    # Build the display message:
    # "⇒ RESULT :" and then a language header + grey pre block with output
    reply_html = (
        "⇒ <b>RESULT :</b>\n\n"
        f"<b>{html.escape(language.title())}</b>\n"
        f"<pre>{safe_output}</pre>"
    )

    # Copy button — use switch_inline_query_current_chat so user can paste code into input
    # (this avoids the invalid 'copy_text' field error)
    # Use original code (not HTML-escaped) so it inserts valid code into input
    try:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK),
                InlineKeyboardButton("👑 Owner", url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}")
            ],
            [InlineKeyboardButton("📋 COPY CODE", switch_inline_query_current_chat=code)]
        ])
    except Exception:
        # Fallback simple keyboard if something unexpected
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK)]
        ])

    # Send the result (single reply)
    await update.message.reply_text(reply_html, parse_mode="HTML", reply_markup=kb)


async def echo_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # When people send plain text (not a command) we keep it simple
    await update.message.reply_text("Send `/eval <code>` to run code.", parse_mode="Markdown")


# -------------------------
# Entrypoint
# -------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("eval", eval_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_help))

    logger.info("Starting Eval Bot")
    app.run_polling()


if __name__ == "__main__":
    main()
