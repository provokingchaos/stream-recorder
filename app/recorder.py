import asyncio
import os
import datetime
import urllib.request
import re
from typing import Dict, Optional
from app.database import get_db, log_event, get_setting
from app.notifier import send_telegram_notification
from app.transcriber import transcribe_audio

active_recordings: Dict[int, "StreamRecorder"] = {}

class StreamRecorder:
    def __init__(self, recording_id: int, stream_id: int, stream_url: str, label: str, duration_minutes: Optional[int] = None, description: Optional[str] = None):
        self.recording_id = recording_id
        self.stream_id = stream_id
        self.stream_url = stream_url
        self.label = str(label) if label is not None else 'Stream'
        self.duration_minutes = duration_minutes
        self.description = description or ""
        self.process: Optional[asyncio.subprocess.Process] = None
        self.output_path: Optional[str] = None
        self.start_time: Optional[datetime.datetime] = None
        self.is_stopping = False

    async def start(self):
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'recordings_dir'").fetchone()
            rec_dir = row[0] if row else "/recordings"
        
        os.makedirs(rec_dir, exist_ok=True)
        self.start_time = datetime.datetime.now()
        timestamp = self.start_time.strftime("%Y-%m-%d_%H-%M-%S")
        safe_label = "".join(c for c in str(self.label) if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_") or f"stream_{self.stream_id}"
        safe_desc = "".join(c for c in str(self.description or "") if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
        if safe_desc:
            filename = f"{safe_label}_{safe_desc}_{timestamp}.mp3"
        else:
            filename = f"{safe_label}_{timestamp}.mp3"
        self.output_path = os.path.join(rec_dir, filename)

        with get_db() as conn:
            row = conn.execute("SELECT filename FROM recordings WHERE id = ?", (self.recording_id,)).fetchone()
            if row and row[0]:
                filename = row[0]
                self.output_path = os.path.join(rec_dir, filename)
            conn.execute(
                "UPDATE recordings SET filename = ?, start_time = ?, status = 'recording' WHERE id = ?",
                (filename, self.start_time.isoformat(), self.recording_id)
            )
            conn.commit()

        actual_url = self.stream_url
        clean_url = actual_url.lower().split('?')[0]
        
        if clean_url.endswith('.pls') or clean_url.endswith('.m3u'):
            try:
                req = urllib.request.Request(actual_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    content = response.read().decode('utf-8', errors='ignore')
                    if clean_url.endswith('.pls'):
                        match = re.search(r'^File\d*=(http[^\s]+)', content, re.IGNORECASE | re.MULTILINE)
                        if match:
                            actual_url = match.group(1).strip()
                    else:
                        for line in content.splitlines():
                            line = line.strip()
                            if line.startswith('http'):
                                actual_url = line
                                break
            except Exception as e:
                log_event(f"Warning: Could not parse playlist file {actual_url} - {e}")

        cmd = [
            "ffmpeg",
            "-y",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", actual_url,
            "-c:a", "libmp3lame",
            "-b:a", "128k"
        ]

        if self.duration_minutes:
            cmd.extend(["-t", str(self.duration_minutes * 60)])

        cmd.append(self.output_path)

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            
            try:
                send_telegram_notification("notif_stream_connected", stream_label=self.label)
            except Exception:
                pass
                
            log_event(f"Started recording '{self.label}' to {filename}")
        except Exception as e:
            log_event(f"Failed to start FFmpeg for '{self.label}': {e}")
            with get_db() as conn:
                conn.execute("UPDATE recordings SET status = 'failed' WHERE id = ?", (self.recording_id,))
                conn.commit()
            
            active_recordings.pop(self.recording_id, None)
            return

        asyncio.create_task(self._monitor_process())

    async def _monitor_process(self):
        if self.process:
            await self.process.wait()
            
            try:
                send_telegram_notification("notif_stream_disconnected", stream_label=self.label)
            except Exception:
                pass

            if self.duration_minutes and not self.is_stopping:
                try:
                    desc_text = f" ({self.description})" if self.description else ""
                    now_str = datetime.datetime.now().strftime("%I:%M %p")
                    send_telegram_notification(
                        "notif_sched_stop",
                        stream_label=self.label,
                        desc_text=desc_text,
                        now_str=now_str
                    )
                except Exception:
                    pass

            await self._finalize_recording()

    async def _finalize_recording(self):
        end_time = datetime.datetime.now()
        file_size = 0
        duration_seconds = 0

        if self.output_path and os.path.exists(self.output_path):
            file_size = os.path.getsize(self.output_path)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "error", "-show_entries",
                    "format=duration", "-of",
                    "default=noprint_wrappers=1:nokey=1", self.output_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await proc.communicate()
                if stdout:
                    duration_seconds = int(float(stdout.decode().strip()))
            except Exception:
                pass

        if duration_seconds <= 0:
            duration_seconds = int((end_time - self.start_time).total_seconds()) if self.start_time else 0

        with get_db() as conn:
            conn.execute(
                "UPDATE recordings SET end_time = ?, duration_seconds = ?, file_size_bytes = ?, status = 'completed' WHERE id = ?",
                (end_time.isoformat(), duration_seconds, file_size, self.recording_id)
            )
            conn.commit()

        log_event(f"Finished recording '{self.label}' (Duration: {duration_seconds}s, Size: {file_size} bytes)")
        active_recordings.pop(self.recording_id, None)

        # Trigger automatic transcription if enabled
        if get_setting("auto_transcribe", "false") == "true" and file_size > 0:
            asyncio.create_task(transcribe_audio(self.recording_id, self.output_path))

    async def stop(self):
        self.is_stopping = True
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await self.process.wait()
            except Exception:
                pass
        await self._finalize_recording()