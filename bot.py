#!/usr/bin/env python3
"""
Telegram Code Executor Bot — Linux Terminal Theme Style 💻
By @CodeSynDev
"""

import asyncio
import html
import logging
import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ================= CONFIG =================
BOT_TOKEN = "8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w"
OWNER_USERNAME = "@CodeSynDev"
CHANNEL_LINK = "http://lofiBots.t.me"
PISTON_API = "https://emkc.org/api/v2/piston/execute"
MAX_CONCURRENT_RUNS = 5
# ==========================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LinuxStyleBot")

run_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


def detect_language(code: str) -> str:
    """Auto detect language by code pattern."""
    code = code.strip()
    if code.startswith("#include") or "std::" in code:
        return "c++"
    if "console.log" in code or "function " in code:
        return "javascript"
    return "python"


def copy_button(code: str):
    """Inline Copy button."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 COPY CODE", switch_inline_query_current_chat=code)]]
    )


async def run_code(code: str, lang: str) -> str:
    """Run code through piston API."""
    async with httpx.AsyncClient(timeout=25) as client:
        res = await client.post(
            PISTON_API,
            json={"language": lang, "version": "*", "files": [{"content": code}]}
        )
        data = res.json()
        run = data.get("run", {})
        out = (run.get("stdout") or "") + (run.get("stderr") or "")
        if not out.strip():
            out = "✅ No output"
        return out.strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Owner 👑", url=f"https://t.me/{OWNER_USERNAME[1:]}")],
        [InlineKeyboardButton("Channel 📢", url=CHANNEL_LINK)],
    ]
    msg = (
        "💻 <b>Linux Style Code Executor</b>\n\n"
        "Use:\n<code>/eval print('hi')</code>\n\n"
        "Supported: Python, JS, C, C++"
    )
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def eval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    code = text.replace("/eval", "", 1).strip()
    if not code:
        return await update.message.reply_text(
            "Send code after /eval, e.g.\n<code>/eval print('hi')</code>",
            parse_mode="HTML"
        )

    lang = detect_language(code)

    async with run_semaphore:
        try:
            output = await run_code(code, lang)
        except Exception as e:
            logger.exception("Error while running code")
            output = f"⚠️ {html.escape(str(e))}"

    # Escape safely
    safe_out = html.escape(output)
    safe_lang = html.escape(lang.title())

    # Stylish Linux-like block output
    result = (
        f"<pre>💻 LANGUAGE → {safe_lang}</pre>\n\n"
        f"<pre>{safe_out}</pre>\n\n"
        "📋 <b>Copy Code Below</b>"
    )

    await update.message.reply_text(
        result,
        parse_mode="HTML",
        reply_markup=copy_button(code)
    )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Try /eval print('hi')", parse_mode="HTML")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("eval", eval_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))
    app.run_polling()


if __name__ == "__main__":
    main()
