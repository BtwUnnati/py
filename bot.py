#!/usr/bin/env python3
"""
Code Execution Bot (Telegram)
- Shows output with a language header and preformatted output (like the image you sent)
- Supports: Python, JavaScript, C, C++
- Uses Piston execution API
- Safe HTML escaping so Telegram parse won't fail
- Robust error handling and concurrency limit
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
    MessageHandler,
    filters,
)

# -------------------------
# CONFIG (replace if you rotate token)
# -------------------------
BOT_TOKEN = "8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w"  # temporary token (rotate after tests)
OWNER_USERNAME = "@CodeSynDev"
CHANNEL_LINK = "http://lofiBots.t.me"

# Piston endpoint
PISTON_API = "https://emkc.org/api/v2/piston/execute"

# Concurrency limit
MAX_CONCURRENT_RUNS = 5

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("codebot")

run_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


# -------------------------
# Helpers
# -------------------------
def normalize_owner_url(owner: str) -> str:
    return f"https://t.me/{owner.lstrip('@')}"


def parse_eval_input(text: str) -> Tuple[Optional[str], str]:
    """
    Parse the message text after /eval:
      - inline: /eval python print("hi")
      - fenced:
        /eval
        ```\nprint("hi")
        ```
    Returns (language or None, code string)
    """
    t = text.strip()
    if not t:
        return None, ""

    # Inline: "python print('hi')"
    parts = t.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("python", "py", "javascript", "js", "c", "cpp", "c++"):
        lang_raw = parts[0].lower()
        lang_map = {"py": "python", "python": "python", "javascript": "javascript", "js": "javascript", "c": "c", "cpp": "c++", "c++": "c++"}
        return lang_map.get(lang_raw, lang_raw), parts[1]

    # Code fence detection
    if t.startswith("```") and "```" in t[3:]:
        rest = t[3:]
        # first_line could be language
        if "\n" in rest:
            first_line, body = rest.split("\n", 1)
            # body until closing ```
            if "```" in body:
                body_content = body.rsplit("```", 1)[0]
            else:
                body_content = body
            first_line = first_line.strip()
            if first_line:
                lang_map = {"py": "python", "python": "python", "javascript": "javascript", "js": "javascript", "c": "c", "cpp": "c++", "c++": "c++"}
                return lang_map.get(first_line.lower(), first_line.lower()), body_content
            else:
                return None, body_content

    # No fence, no explicit lang -> return None and whole thing
    return None, t


def rudimentary_detect_language(code: str) -> str:
    s = code.strip()
    if s.startswith("#include") or "std::" in s or "printf(" in s:
        return "c++"
    if "console.log" in s or "console." in s or ("function " in s and "{" in s):
        return "javascript"
    return "python"


async def run_code_piston(code: str, language: str, version: str = "*", timeout_seconds: int = 20) -> str:
    payload = {
        "language": language,
        "version": version,
        "files": [{"content": code}],
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(PISTON_API, json=payload)
        resp.raise_for_status()
        data = resp.json()
        run = data.get("run") or {}
        stdout = run.get("stdout") or ""
        stderr = run.get("stderr") or ""
        if not run and isinstance(data, dict):
            stdout = data.get("output", "") or data.get("stdout", "") or ""
            stderr = data.get("stderr", "") or ""
        output = (stdout or "") + (stderr or "")
        if not output:
            exit_code = (run.get("code") if run else data.get("code"))
            output = f"Process exited with code: {exit_code}" if exit_code is not None else "Finished (no output)"
        return output


# -------------------------
# Handlers
# -------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Devloper", url=normalize_owner_url(OWNER_USERNAME))],
        [InlineKeyboardButton("Channel", url=CHANNEL_LINK)],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    text = (
        "Hello — I'm a Code Execution Bot By @esxnz \n\n"
        "Send examples :\n"
        "<code>/ev print('hello_World')</code>\n"
        "Supported Lang : Python, JavaScript, C, C++"
    )
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Usage examples:\n"
        "<code>/ev print('hello')</code>\n\n",
        parse_mode="HTML",
    )


async def eval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    raw = message.text or ""

    # Remove the command portion robustly (handles /eval@BotName)
    without_cmd = raw
    if raw.lower().startswith("/ev"):
        # Remove the first token (/eval or /eval@Bot)
        without_cmd = raw.partition(" ")[2] if " " in raw else raw.replace("/eval", "", 1).strip()
    # Also remove /eval@BotName cases
    if without_cmd.startswith(f"@{(context.bot.username or '')}"):
        without_cmd = without_cmd.replace(f"@{(context.bot.username or '')}", "", 1).strip()

    lang, code = parse_eval_input(without_cmd)
    if not code:
        await message.reply_text(
            "Please provide code. Examples:\n"
            "<code>/ev print('hello World')</code>\n",
            parse_mode="HTML",
        )
        return

    if not lang:
        lang = rudimentary_detect_language(code)

    # normalize
    lang_map = {"py": "python", "js": "javascript", "c++": "c++", "cpp": "c++", "c": "c"}
    lang = lang_map.get(lang, lang)

    # run with semaphore
    async with run_semaphore:
        try:
            output = await run_code_piston(code, language=lang)
        except httpx.RequestError as e:
            logger.exception("HTTP error calling Piston")
            await message.reply_text(f"Execution service HTTP error:\n<pre>{html.escape(str(e))}</pre>", parse_mode="HTML")
            return
        except httpx.HTTPStatusError as e:
            logger.exception("Piston returned non-2xx")
            await message.reply_text(f"Executor returned error:\n<pre>{html.escape(str(e))}</pre>", parse_mode="HTML")
            return
        except Exception as e:
            logger.exception("Unexpected execution error")
            await message.reply_text(f"Unexpected error:\n<pre>{html.escape(str(e))}</pre>", parse_mode="HTML")
            return

    # Prepare the final message in the desired visual format:
    # - Big header with language name
    # - Preformatted output block (HTML <pre>)
    safe_output = html.escape(output)
    MAX_CHARS = 4000
    if len(safe_output) > MAX_CHARS:
        safe_output = safe_output[: MAX_CHARS - 200] + "\n\n...output truncated..."

    # Example visual:
    # <b>RESULT — Python</b>
    # <pre>hi</pre>
    try:
        reply_text = f"<b>Out Put — {html.escape(lang.title())}</b>\n<pre>{safe_output}</pre>"
        await message.reply_text(reply_text, parse_mode="HTML")
    except Exception:
        # Fallback: if HTML sending fails for any reason, send a plain-escaped message
        await message.reply_text(f"Out Put — {lang.title()}\n\n{safe_output}")


async def text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_handler(update, context)


# -------------------------
# Entrypoint
# -------------------------
def main():
    logger.info("Starting Code Execution Bot")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("ev", eval_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback))

    app.run_polling()


if __name__ == "__main__":
    main()
