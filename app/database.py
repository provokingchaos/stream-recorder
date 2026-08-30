import sqlite3
import datetime
from contextlib import contextmanager

DB_PATH = "/config/stream_recorder.db"
LOG_PATH = "/config/app.log"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                stream_url TEXT NOT NULL
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                duration_seconds INTEGER DEFAULT 0,
                file_size_bytes INTEGER DEFAULT 0,
                start_time TEXT,
                end_time TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                FOREIGN KEY(stream_id) REFERENCES streams(id) ON DELETE CASCADE
            )
        """)

        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(recordings)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if "start_time" not in columns:
            cursor.execute("ALTER TABLE recordings ADD COLUMN start_time TEXT")
        if "end_time" not in columns:
            cursor.execute("ALTER TABLE recordings ADD COLUMN end_time TEXT")
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                description TEXT DEFAULT "",
                status TEXT DEFAULT 'pending',
                FOREIGN KEY(stream_id) REFERENCES streams(id) ON DELETE CASCADE
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()

def get_setting(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

def log_event(message: str):
    try:
        with get_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("INSERT INTO logs (message) VALUES (?)", (str(message),))
            conn.commit()
    except Exception as e:
        print(f"[LOG ERROR] {e}: {message}", flush=True)

def recover_interrupted_recordings():
    try:
        with get_db() as conn:
            conn.execute("UPDATE recordings SET status = 'failed' WHERE status = 'recording'")
            conn.commit()
            log_event("System startup: Cleaned up any interrupted recording states.")
    except Exception as e:
        pass
