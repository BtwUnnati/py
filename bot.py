#!/usr/bin/env python3
"""
Code Execution Bot (Telegram)
- Respond ONLY to /ev command ✅
- Shows output with a language header and preformatted output
- Supports: Python, JavaScript, C, C++
"""

import asyncio
import html
import logging
from typing import Optional, Tuple

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# -------------------------
# CONFIG
# -------------------------
BOT_TOKEN = "8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w"
OWNER_USERNAME = "@CodeSynDev"
CHANNEL_LINK = "http://lofiBots.t.me"
PISTON_API = "https://emkc.org/api/v2/piston/execute"
MAX_CONCURRENT_RUNS = 5

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("codebot")
run_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


def normalize_owner_url(owner: str) -> str:
    return f"https://t.me/{owner.lstrip('@')}"


def parse_eval_input(text: str) -> Tuple[Optional[str], str]:
    t = text.strip()
    if not t:
        return None, ""

    parts = t.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in (
        "python", "py", "javascript", "js", "c", "cpp", "c++"
    ):
        lang = parts[0].lower()
        lang_map = {
            "py": "python",
            "python": "python",
            "js": "javascript",
            "javascript": "javascript",
            "c": "c",
            "cpp": "c++",
            "c++": "c++",
        }
        return lang_map.get(lang, lang), parts[1]
    return None, t


def detect_lang(code: str) -> str:
    s = code.strip()
    if s.startswith("#include") or "std::" in s or "printf(" in s:
        return "c++"
    if "console.log" in s:
        return "javascript"
    return "python"


async def run_code_piston(code: str, language: str) -> str:
    payload = {"language": language, "version": "*", "files": [{"content": code}]}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(PISTON_API, json=payload)
    data = resp.json()
    run = data.get("run", {})
    stdout = run.get("stdout", "")
    stderr = run.get("stderr", "")
    output = (stdout + stderr) or "✅ Finished (no output)"
    return output


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Devloper", url=normalize_owner_url(OWNER_USERNAME))],
        [InlineKeyboardButton("Channel", url=CHANNEL_LINK)],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Hello — I'm a Code Execution Bot ✅\n\nUse:\n"
        "<code>/ev python print('hi')</code>\n\n"
        "Supported: Python, JavaScript, C, C++",
        parse_mode="HTML",
        reply_markup=markup,
    )


async def ev_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    raw = message.text or ""

    # Remove /ev or /ev@Bot
    without_cmd = raw.split(" ", 1)[1] if " " in raw else ""

    lang, code = parse_eval_input(without_cmd)
    if not code:
        await message.reply_text(
            "Example:\n<code>/ev python print('hi')</code>",
            parse_mode="HTML",
        )
        return

    if not lang:
        lang = detect_lang(code)

    async with run_semaphore:
        try:
            output = await run_code_piston(code, lang)
        except Exception as e:
            output = f"Error: {e}"

    safe_output = html.escape(output)
    reply_text = f"<b>Out Put — {lang.title()}</b>\n<pre>{safe_output}</pre>"
    await message.reply_text(reply_text, parse_mode="HTML")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("ev", ev_handler))  # ✅ Only /ev now

    app.run_polling()


if __name__ == "__main__":
    main()
