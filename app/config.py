import os
from pathlib import Path

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
DB_PATH = CONFIG_DIR / "stream_recorder.db"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
