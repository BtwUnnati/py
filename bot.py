#!/usr/bin/env python3
"""
Code Execution Bot (Telegram)
- Supports: Python, JavaScript, C, C++
- Uses Piston execution API
- Safe HTML escaping of outputs to avoid Telegram parse issues
- Robust error handling so it won't crash/restart on bad input
- Owner & Channel buttons
- No auto-delete of user messages (per request)
"""

import asyncio
import html
import logging
from typing import Optional

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
# Config: change if needed
# -------------------------
BOT_TOKEN = "8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w"  # YOUR temporary token (rotate after testing!)
OWNER_USERNAME = "@CodeSynDev"
CHANNEL_LINK = "http://lofiBots.t.me"

# Piston endpoint used previously in your logs — works for many installs.
PISTON_API = "https://emkc.org/api/v2/piston/execute"

# Optional execution concurrency limit (per whole bot)
MAX_CONCURRENT_RUNS = 5

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("codebot")

# Semaphore to limit concurrent runs
run_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


# -------------------------
# Helpers
# -------------------------
def normalize_owner_url(owner: str) -> str:
    """Return a t.me link from username like @Name or Name"""
    owner = owner.lstrip("@")
    return f"https://t.me/{owner}"


def parse_eval_input(text: str) -> (Optional[str], str):
    """
    Parse the message text after /eval:
    - Accepts: `/eval python print("hi")`
    - Accepts code fences:
      /eval
      ```python
      print("hi")
      ```
    - If language not provided, attempt to detect rudimentarily.
    Returns (language, code)
    """
    # strip leading/trailing whitespace
    t = text.strip()

    # If empty
    if not t:
        return None, ""

    # If user provided inline like: "python print('hi')"
    parts = t.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("python", "py", "javascript", "js", "c", "cpp", "c++"):
        lang_raw = parts[0].lower()
        lang_map = {"py": "python", "python": "python", "javascript": "javascript", "js": "javascript", "c": "c", "cpp": "c++", "c++": "c++"}
        return lang_map.get(lang_raw, lang_raw), parts[1]

    # Check for code fences ```lang ... ```
    if t.startswith("```") and "```" in t[3:]:
        # remove first ```
        rest = t[3:]
        # if it starts with language like ```python\n...
        if "\n" in rest:
            first_line, body = rest.split("\n", 1)
            # find closing ```
            if "```" in body:
                body_content = body.rsplit("```", 1)[0]
            else:
                body_content = body
            first_line = first_line.strip()
            if first_line:  # language specified
                lang = first_line.split()[0].lower()
                lang_map = {"py": "python", "python": "python", "javascript": "javascript", "js": "javascript", "c": "c", "cpp": "c++", "c++": "c++"}
                return lang_map.get(lang, lang), body_content
            else:
                return None, body_content

    # If no fences and no explicit language, return None and whole text (use detection later)
    return None, t


def rudimentary_detect_language(code: str) -> str:
    """Simple heuristics to guess language when user didn't specify."""
    s = code.strip()
    if s.startswith("#include") or "std::" in s or "printf(" in s:
        return "c++"
    if "console.log" in s or "console." in s or "function " in s and "{" in s:
        return "javascript"
    # default to python
    return "python"


async def run_code_piston(code: str, language: str, version: str = "*", timeout_seconds: int = 20) -> str:
    """
    Run code using Piston API. Returns combined stdout+stderr or raises on HTTP/execution issues.
    """
    payload = {
        "language": language,
        "version": version,
        "files": [{"content": code}],
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(PISTON_API, json=payload)
        resp.raise_for_status()
        data = resp.json()
        # historic Piston format puts run output in ['run'] subobject
        run = data.get("run") or {}
        stdout = run.get("stdout") or ""
        stderr = run.get("stderr") or ""
        # If Piston uses a different shape:
        if not run and isinstance(data, dict):
            # try to combine common fields
            stdout = data.get("output", "") or data.get("stdout", "") or ""
            stderr = data.get("stderr", "") or ""
        output = (stdout or "") + (stderr or "")
        if not output:
            # Some Piston installations return exits and signals
            exit_code = (run.get("code") if run else data.get("code"))
            if exit_code is not None:
                output = f"Process exited with code: {exit_code}"
            else:
                output = "✅ Finished (no output)"
        return output


# -------------------------
# Handlers
# -------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Owner 👑", url=normalize_owner_url(OWNER_USERNAME))],
        [InlineKeyboardButton("Channel 📢", url=CHANNEL_LINK)],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    text = (
        "Hello — I'm a Code Execution Bot ✅\n\n"
        "Examples:\n"
        "<code>/eval print('hi')</code>\n"
        "<code>/eval python print('hello')</code>\n"
        "Or send a fenced code block:\n"
        "<pre>```python\nprint('hi')\n```</pre>\n\n"
        "Supported: Python, JavaScript, C, C++ (via remote executor)\n"
    )
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Use:\n"
        "<code>/eval python print('hello')</code>\n"
        "Or:\n"
        "<pre>/eval\n```python\nprint('hello')\n```</pre>\n\n"
        "Supports: Python, JavaScript, C, C++",
        parse_mode="HTML",
    )


async def eval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    raw = message.text or ""
    # Remove command prefix (works for '/eval' or '/eval@BotName')
    without_cmd = raw.partition(" ")[2] if " " in raw else raw.replace("/eval", "").replace(f"/eval@{(context.bot.username or '')}", "").strip()

    lang, code = parse_eval_input(without_cmd)
    if not code:
        await message.reply_text(
            "Please provide code. Examples:\n"
            "<code>/eval python print('hi')</code>\n"
            "Or:\n"
            "<pre>/eval\n```python\nprint('hi')\n```</pre>",
            parse_mode="HTML",
        )
        return

    if not lang:
        lang = rudimentary_detect_language(code)

    # Normalize some language keys for Piston
    lang_map = {"py": "python", "js": "javascript", "c++": "c++", "cpp": "c++", "c": "c"}
    lang = lang_map.get(lang, lang)

    # Run with concurrency limit
    async with run_semaphore:
        try:
            # Timeout is controlled by Piston client; keep reasonably small to avoid queued hangs.
            output = await run_code_piston(code, language=lang)
        except httpx.RequestError as e:
            logger.exception("HTTP error when calling Piston")
            await message.reply_text(
                f"Execution service HTTP error:\n<pre>{html.escape(str(e))}</pre>",
                parse_mode="HTML",
            )
            return
        except httpx.HTTPStatusError as e:
            logger.exception("Piston returned non-2xx")
            await message.reply_text(
                f"Executor returned error:\n<pre>{html.escape(str(e))}</pre>",
                parse_mode="HTML",
            )
            return
        except Exception as e:
            logger.exception("Unexpected error during execution")
            await message.reply_text(
                f"Unexpected error:\n<pre>{html.escape(str(e))}</pre>",
                parse_mode="HTML",
            )
            return

    # Escape output for safe HTML
    safe_output = html.escape(output)
    # Truncate if extremely long to avoid Telegram limits (with note)
    MAX_CHARS = 4000
    if len(safe_output) > MAX_CHARS:
        safe_output = safe_output[: MAX_CHARS - 200] + "\n\n...output truncated..."
    await message.reply_text(f"<pre>{safe_output}</pre>", parse_mode="HTML")


async def text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # When user sends plain text (not a command), show help
    await help_handler(update, context)


# -------------------------
# Entry point
# -------------------------
def main():
    logger.info("Starting Code Execution Bot")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("eval", eval_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback))

    # Run polling
    app.run_polling()


if __name__ == "__main__":
    main()
