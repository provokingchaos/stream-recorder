import os
import shutil
import asyncio
import datetime
from pathlib import Path
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from app.database import get_db, get_setting
from app.notifier import send_telegram_alert

active_recordings = {}

class StreamRecorder:
    def __init__(self, stream_id: int, label: str, stream_url: str, duration_minutes: int = None):
        self.stream_id = stream_id
        self.label = "".join(c for c in label if c.isalnum() or c in (" ", "_", "-")).rstrip()
        if not self.label:
            self.label = "Unknown_Stream"
        self.stream_url = stream_url
        self.duration_minutes = duration_minutes
        self.process = None
        self.task = None
        self.db_id = None
        self.part_path = None
        self.final_path = None
        self.start_time = None

    def get_recordings_dir(self) -> Path:
        dir_path = Path(get_setting("recordings_dir", "/recordings"))
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path

    async def start(self):
        rec_dir = self.get_recordings_dir()
        stat = shutil.disk_usage(rec_dir)
        if (stat.free / (1024 * 1024 * 1024)) < 2.0:
            await send_telegram_alert(f"⚠️ *Recording Aborted*: Low disk space (< 2 GB) for `{self.label}`.")
            return

        self.start_time = datetime.datetime.now()
        timestamp_str = self.start_time.strftime("%Y-%m-%d_%H-%M-%S")
        filename_base = f"{self.label}_{timestamp_str}"
        self.part_path = rec_dir / f"{filename_base}.mp3.part"
        self.final_path = rec_dir / f"{filename_base}.mp3"

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO recordings (stream_id, filename, filepath, status) VALUES (?, ?, ?, 'recording')",
                (self.stream_id, self.final_path.name, str(self.final_path))
            )
            self.db_id = cursor.lastrowid
            conn.commit()

        active_recordings[self.db_id] = self
        self.task = asyncio.create_task(self._run_recording())
        await send_telegram_alert(f"🎙️ *Started Recording*: `{self.label}`\nFile: `{self.final_path.name}`")

    async def _run_recording(self):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-reconnect", "1", "-reconnect_at_eof", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
            "-i", self.stream_url,
            "-vn", "-c:a", "libmp3lame", "-b:a", "192k",
            "-id3v2_version", "3",
            "-metadata", f"title={self.label} - {self.start_time.strftime('%Y-%m-%d')}",
            "-metadata", f"artist={self.label}",
            "-metadata", "album=Stream Recorder Captures",
            "-y", str(self.part_path)
        ]

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            if self.duration_minutes:
                await asyncio.sleep(self.duration_minutes * 60)
                await self.stop()
            else:
                await self.process.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._finalize()

    async def stop(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.stdin.write(b"q")
                await self.process.stdin.drain()
                await asyncio.wait_for(self.process.wait(), timeout=10.0)
            except Exception:
                self.process.terminate()
        if self.task and not self.task.done():
            self.task.cancel()

    async def _finalize(self):
        if self.db_id in active_recordings:
            del active_recordings[self.db_id]

        file_size = 0
        duration_seconds = 0

        if self.part_path.exists():
            os.rename(self.part_path, self.final_path)
            file_size = self.final_path.stat().st_size
            try:
                audio = MP3(self.final_path, ID3=EasyID3)
                duration_seconds = int(audio.info.length)
            except Exception:
                pass

        size_mb = round(file_size / (1024 * 1024), 2)
        with get_db() as conn:
            conn.execute(
                """UPDATE recordings 
                   SET status = 'completed', duration_seconds = ?, file_size_bytes = ?, ended_at = CURRENT_TIMESTAMP 
                   WHERE id = ?""",
                (duration_seconds, file_size, self.db_id)
            )
            conn.commit()

        await send_telegram_alert(
            f"⏹️ *Stopped Recording*: `{self.label}`\nSize: `{size_mb} MB`\nDuration: `{datetime.timedelta(seconds=duration_seconds)}`"
        )
