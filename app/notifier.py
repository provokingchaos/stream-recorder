import httpx
from app.database import get_setting

async def send_telegram_alert(message: str):
    token = get_setting("telegram_bot_token")
    chat_id = get_setting("telegram_chat_id")
    
    if not token or not chat_id:
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload)
    except Exception:
        pass
