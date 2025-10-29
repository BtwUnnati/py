#!/usr/bin/env python3
import asyncio
import html
import logging
from typing import Optional, Tuple

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)

# ====================
# CONFIG
# ====================
BOT_TOKEN = "8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w"
OWNER_USERNAME = "@CodeSynDev"
CHANNEL_LINK = "http://lofiBots.t.me"
PISTON_API = "https://emkc.org/api/v2/piston/execute"
MAX_CONCURRENT_RUNS = 3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("EvalBot")
sem = asyncio.Semaphore(MAX_CONCURRENT_RUNS)

LANG_MAP = {
    "py": "python",
    "python": "python",
    "js": "javascript",
    "javascript": "javascript",
    "c": "c",
    "cpp": "c++",
    "c++": "c++"
}

def normalize(url):
    return f"https://t.me/{url.lstrip('@')}"

def detect_lang(code):
    s = code.lower()
    if "#include" in s or "std::" in s:
        return "c++"
    if "console.log" in s:
        return "javascript"
    return "python"

async def exec_code(code, lang):
    payload = {
        "language": lang,
        "version": "*",
        "files": [{"content": code}]
    }
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post(PISTON_API, json=payload)
        r.raise_for_status()
        data = r.json()
        run = data.get("run", {})
        stdout = run.get("stdout") or ""
        stderr = run.get("stderr") or ""
        out = stdout + stderr
        return out if out.strip() else "No Output"


# ====================
# HANDLERS
# ====================

async def start(update: Update, ctx):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK)],
        [InlineKeyboardButton("👤 Owner", url=normalize(OWNER_USERNAME))]
    ])
    await update.message.reply_text(
        "✅ Use `/eval yourcode`\nExample:\n/eval print('hi')",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def eval_cmd(update: Update, ctx):
    msg = update.message
    raw = msg.text.replace("/eval", "", 1).strip()
    if not raw:
        return await msg.reply_text("⚠ Give some code!")

    # detect language prefix
    p = raw.split(maxsplit=1)
    if p[0].lower() in LANG_MAP:
        lang = LANG_MAP[p[0].lower()]
        code = p[1] if len(p) == 2 else ""
    else:
        code = raw
        lang = detect_lang(code)

    await msg.reply_text("⏳ Running code...")

    async with sem:
        out = await exec_code(code, lang)

    safe = html.escape(out)

    # ✅ EXACT UI LIKE screenshot
    result = (
        f"⇒ <b>RESULT :</b>\n\n"
        f"<b>{lang.title()}</b>\n"
        f"<pre>{safe}</pre>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 COPY CODE", copy_text=code)]
    ])

    await msg.reply_text(result, parse_mode="HTML", reply_markup=kb)

async def fallback(update, ctx):
    await start(update, ctx)


# ====================
# RUN
# ====================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("eval", eval_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))
    print("✅ Eval Bot Running…")
    app.run_polling()

if __name__ == "__main__":
    main()
