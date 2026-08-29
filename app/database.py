import sqlite3
from app.config import DB_PATH

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                page_url TEXT,
                stream_url TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER NOT NULL,
                cron_expression TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (stream_id) REFERENCES streams (id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                duration_seconds INTEGER DEFAULT 0,
                file_size_bytes INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                ended_at DATETIME
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT UNIQUE PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        
        # Insert default settings if they don't exist
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('recordings_dir', '/recordings')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('telegram_bot_token', '')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('telegram_chat_id', '')")
        conn.commit()

def get_setting(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?", (key, value, value))
        conn.commit()

def recover_interrupted_recordings():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, filepath FROM recordings WHERE status = 'recording'")
        interrupted = cursor.fetchall()
        for rec in interrupted:
            cursor.execute(
                "UPDATE recordings SET status = 'interrupted', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (rec["id"],)
            )
        conn.commit()
    return interrupted
