import os
import json
import html
import asyncio
import httpx
from app.database import get_db, log_event

def get_telegram_config():
    with get_db() as conn:
        rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        bot_token = rows.get("telegram_token") or rows.get("telegram_bot_token") or ""
        chat_id = rows.get("telegram_chat_id") or ""
        return bot_token.strip(), chat_id.strip(), rows

def load_message_templates():
    default_templates = {
        "notif_sched_start": "🎙️ <b>RECORDING STARTED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Window:</b> <code>{start_str} - {end_str}</code>",
        "notif_sched_stop": "⏹ <b>RECORDING COMPLETED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Finished:</b> <code>{now_str}</code>",
        "notif_manual_start": "🎙️ <b>MANUAL RECORDING STARTED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Started:</b> <code>{start_str}</code>",
        "notif_manual_stop": "⏹ <b>MANUAL RECORDING COMPLETED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Finished:</b> <code>{now_str}</code>",
        "notif_stream_connected": "🟢 <b>STREAM CONNECTED</b>\n<b>Stream:</b> <code>{stream_label}</code>",
        "notif_stream_disconnected": "⚠️ <b>STREAM DISCONNECTED</b>\n<b>Stream:</b> <code>{stream_label}</code>"
    }
    for path in ["/config/messages.json", "/app/app/messages.json", "app/messages.json"]:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        default_templates.update(data)
                        break
            except Exception as e:
                print(f"[!] Error loading {path}: {e}", flush=True)
    return default_templates

def send_telegram_notification(event_key: str, **kwargs):
    """Fire-and-forget wrapper to prevent blocking the FastAPI event loop."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_telegram_async(event_key, **kwargs))
    except RuntimeError:
        asyncio.run(_send_telegram_async(event_key, **kwargs))

async def _send_telegram_async(event_key: str, **kwargs):
    bot_token, chat_id, settings = get_telegram_config()
    if not bot_token or not chat_id:
        return False

    if event_key in settings:
        val = str(settings[event_key]).lower()
        if val in ("false", "0", "off", "no"):
            return False

    templates = load_message_templates()
    template = templates.get(event_key)
    if not template:
        return False

    safe_kwargs = {}
    for k, v in kwargs.items():
        if k == "desc_text":
            val = str(v).strip()
            if val:
                clean_desc = val.lstrip(" (").rstrip(")")
                safe_kwargs[k] = f"\n<b>Description:</b> <i>{html.escape(clean_desc)}</i>"
            else:
                safe_kwargs[k] = ""
        else:
            safe_kwargs[k] = html.escape(str(v)) if v is not None else ""

    for key in ["stream_label", "desc_text", "start_str", "end_str", "now_str"]:
        if key not in safe_kwargs:
            safe_kwargs[key] = ""

    try:
        formatted_message = template.format(**safe_kwargs)
    except Exception:
        formatted_message = f"<b>{event_key}</b>: " + ", ".join(f"{k}={v}" for k, v in kwargs.items())

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": formatted_message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10.0)
            if resp.status_code != 200:
                log_event(f"Telegram alert error: {resp.text}")
                return False
            return True
    except Exception as e:
        log_event(f"Telegram network error: {e}")
        return False