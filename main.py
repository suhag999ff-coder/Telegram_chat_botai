import asyncio
import getpass
import json
import logging
import os
import re
import threading
from collections import deque
from urllib import error, request

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, MessageHandler, filters

SYSTEM_PROMPT = """You are W8 NOYON SIR AI, a helpful professional AI assistant.
If the user speaks Bangla, reply in Bangla.
Never reveal API keys, tokens, or server secrets.
Give concise, useful answers."""

MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
API_URL = "https://openrouter.ai/api/v1/chat/completions"

MAX_HISTORY = 24
HISTORY_LOCK = threading.Lock()
CHAT_HISTORY = {}

def ask_secret(env_name, prompt):
    value = os.getenv(env_name)
    if value:
        return value

    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, OSError):
        return input(prompt).strip()

TG_BOT_TOKEN = ask_secret("TG_BOT_TOKEN", "Telegram Bot Token: ")

OPENROUTER_KEY_1 = ask_secret("OPENROUTER_KEY_1", "OpenRouter API Key: ")
raw_key2 = os.getenv("OPENROUTER_KEY_2") or ask_secret(
    "OPENROUTER_KEY_2",
    "OpenRouter API Key 2, Enter to skip: "
).strip()

AI_KEYS = [key for key in (OPENROUTER_KEY_1, raw_key2) if key]

if not TG_BOT_TOKEN or not AI_KEYS:
    raise SystemExit("Telegram Bot Token and at least one OpenRouter API Key are required.")

def call_openrouter(token, messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.7
    }

    req = request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/",
            "X-Title": "W8 NOYON SIR AI"
        }
    )

    try:
        with request.urlopen(req, timeout=120) as response:
            obj = json.loads(response.read().decode("utf-8", "replace"))

        choices = obj.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned no choices.")

        choice = choices[0] if isinstance(choices[0], dict) else {}
        content = (choice.get("message") or {}).get("content") or ""

        if isinstance(content, list):
            content = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict)
            )

        if not content:
            raise RuntimeError("OpenRouter returned an empty response.")

        return content.strip()

    except error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")

        try:
            obj = json.loads(raw)
            err = obj.get("error", {})
            message = err.get("message") or raw
            code = err.get("code") or e.code
        except Exception:
            message = raw or e.reason
            code = e.code

        raise RuntimeError(f"OpenRouter HTTP {code}: {message}") from None

    except error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from None

    except Exception as e:
        raise RuntimeError(f"Request error: {e}") from None

def build_messages(chat_id, user_text):
    with HISTORY_LOCK:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        for role, content in CHAT_HISTORY.get(chat_id, []):
            messages.append({
                "role": role,
                "content": content
            })

        messages.append({
            "role": "user",
            "content": user_text
        })

    return messages

def save_history(chat_id, user_text, reply):
    with HISTORY_LOCK:
        CHAT_HISTORY.setdefault(
            chat_id,
            deque(maxlen=MAX_HISTORY)
        ).extend([
            ("user", user_text),
            ("assistant", reply)
        ])

def clear_history(chat_id):
    with HISTORY_LOCK:
        CHAT_HISTORY.pop(chat_id, None)

def get_ai_reply(chat_id, user_text):
    messages = build_messages(chat_id, user_text)
    errors = []

    for key in AI_KEYS:
        try:
            reply = call_openrouter(key, messages)
            save_history(chat_id, user_text, reply)
            return reply
        except Exception as e:
            errors.append(str(e))

    raise RuntimeError(
        "Both OpenRouter keys failed: " + " | ".join(errors[-3:])
    )

def remove_bot_mention(text, username):
    if username:
        text = re.sub(
            rf"@{re.escape(username)}\b",
            "",
            text,
            flags=re.I
        ).strip()
    return text

async def start_cmd(update, context):
    msg = update.effective_message
    if not msg:
        return

    await msg.reply_text("""✅ W8 NOYON SIR AI ready.

Commands:
/start - start
/help - help
/newchat - clear history
/status - status
/stop - clear current chat

In groups, mention the bot or reply to it.""")

async def help_cmd(update, context):
    msg = update.effective_message
    if not msg:
        return

    await msg.reply_text("""Available commands:

/start
/help
/newchat
/status
/stop""")

async def status_cmd(update, context):
    msg = update.effective_message
    if not msg:
        return

    await msg.reply_text(f"""✅ Model: {MODEL}
🔑 AI Provider: OpenRouter
🔑 API Key: configured""")

async def reset_chat(update, context):
    msg = update.effective_message
    chat_id = update.effective_chat.id

    clear_history(chat_id)

    if msg:
        await msg.reply_text("✅ History cleared.")

async def stop_chat(update, context):
    msg = update.effective_message
    chat_id = update.effective_chat.id

    clear_history(chat_id)

    if msg:
        await msg.reply_text("✅ Current chat stopped and history cleared.")

async def handle_message(update, context):
    msg = update.effective_message
    chat = update.effective_chat

    if not msg or not chat or not msg.text:
        return

    text = msg.text.strip()
    if not text:
        return

    username = context.bot.username

    if chat.type != ChatType.PRIVATE:
        if username and f"@{username}".lower() not in text.lower():
            return

        if not msg.reply_to_message or msg.reply_to_message.from_user.is_bot:
            return

        text = remove_bot_mention(text, username)

    if not text:
        return

    chat_id = chat.id

    await context.bot.send_chat_action(chat_id, "typing")

    try:
        reply = await asyncio.to_thread(get_ai_reply, chat_id, text)
    except Exception as e:
        reply = f"❌ AI service error:\n{e}"

    await context.bot.send_message(
        chat_id=chat_id,
        text=reply,
        disable_web_page_preview=True
    )

async def on_error(update, context):
    exc = context.error
    if exc:
        logging.error(
            "Telegram update error: %r",
            exc,
            exc_info=(type(exc), exc, exc.__traceback__)
        )

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    app = Application.builder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(["newchat", "reset"], reset_chat))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop", stop_chat))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)

    app.run_polling()

if __name__ == "__main__":
    main()
