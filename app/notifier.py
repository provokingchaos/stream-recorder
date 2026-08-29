import os
import json
import urllib.request
import urllib.parse
import html
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
    bot_token, chat_id, settings = get_telegram_config()
    if not bot_token or not chat_id:
        print("[!] Telegram skipped: Missing credentials in database settings.", flush=True)
        return False

    # Check notification toggle in settings
    if event_key in settings:
        val = str(settings[event_key]).lower()
        if val in ("false", "0", "off", "no"):
            print(f"[!] Notification for '{event_key}' is disabled in settings.", flush=True)
            return False

    templates = load_message_templates()
    template = templates.get(event_key)
    if not template:
        print(f"[!] Telegram skipped: Template '{event_key}' not found.", flush=True)
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
    except Exception as e:
        print(f"[!] Template format error: {e}", flush=True)
        formatted_message = f"<b>{event_key}</b>: " + ", ".join(f"{k}={v}" for k, v in kwargs.items())

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": formatted_message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8")
            if resp.status != 200:
                print(f"[!] Telegram API error ({resp.status}): {resp_body}", flush=True)
                log_event(f"Telegram alert error: {resp_body}")
                return False
            print(f"[+] Telegram alert delivered: {event_key}", flush=True)
            return True
    except Exception as e:
        print(f"[!] Telegram request failed: {e}", flush=True)
        log_event(f"Telegram network error: {e}")
        return False
