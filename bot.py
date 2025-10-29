import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = "8470120636:AAF4ipUqg8xKqho8WQInf8MuuDpn2749K1w"
CHANNEL_URL = "http://lofiBots.t.me"
OWNER_USERNAME = "@CodeSynDev"

PISTON_URL = "https://emkc.org/api/v2/piston/execute"


def detect_language(code: str) -> str:
    if "console.log" in code:
        return "javascript"
    if "#include" in code:
        return "c"
    if "<?php" in code:
        return "php"
    if "public class" in code:
        return "java"
    return "python"


async def eval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.replace("/eval", "").strip()
    if not code:
        return await update.message.reply_text("⚠ Code likh bhai!")

    lang = detect_language(code)

    payload = {
        "language": lang,
        "version": "*",
        "files": [{"content": code}]
    }

    try:
        r = requests.post(PISTON_URL, json=payload).json()
        stdout = r.get("run", {}).get("stdout", "")
        stderr = r.get("run", {}).get("stderr", "")

        output = stdout + stderr
        if not output:
            output = "✅ DONE (no output)"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📢 Channel", url=CHANNEL_URL),
                InlineKeyboardButton("👨‍💻 Owner", url=f"https://t.me/{OWNER_USERNAME.strip('@')}")
            ],
            [InlineKeyboardButton("📋 COPY CODE", copy_text=output)]
        ])

        result_msg = (
            "<b>⇒ RESULT :</b>\n\n"
            f"<b>{lang.title()}</b>\n"
            f"<pre><code class=\"language-{lang}\">{output}</code></pre>"
        )

        await update.message.reply_html(result_msg, reply_markup=keyboard)

    except Exception as e:
        await update.message.reply_html(
            "<b>Error:</b>\n<pre>" + str(e) + "</pre>"
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>⚙ Cloud Code Runner Activated</b>\n\n"
        "Use:\n<pre>/eval print(\"hi\")</pre>"
    )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("eval", eval_command))
    app.run_polling()


if __name__ == "__main__":
    main()
