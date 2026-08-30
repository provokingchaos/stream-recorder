
import logging

class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/recordings" not in record.getMessage()

# Filter polling endpoint from Uvicorn access logs
logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
import os
import re
from pathlib import Path
from app.database import init_db, recover_interrupted_recordings, get_db, get_setting, set_setting
from app.scheduler import init_scheduler, schedule_job, remove_scheduled_job
from app.sniffer import sniff_stream_url
from app.recorder import StreamRecorder, active_recordings

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recover_interrupted_recordings()
    init_scheduler()
    yield

app = FastAPI(title="Stream Recorder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Models







from pydantic import BaseModel
from typing import Optional

class StreamCreate(BaseModel):
    label: str
    stream_url: str

class StreamUpdate(BaseModel):
    label: str

class StreamProbe(BaseModel):
    url: str

class ManualRecordRequest(BaseModel):
    stream_id: int

class ScheduleCreateDate(BaseModel):
    stream_id: int
    start_time: str
    end_time: str
    description: str = ""

class SettingsUpdate(BaseModel):
    recordings_dir: str
    telegram_token: str
    telegram_chat_id: str

@app.post("/api/streams/probe")
async def probe_stream(req: StreamProbe):
    try:
        discovered_urls = await sniff_stream_url(req.url)
        return {"success": True, "stream_urls": discovered_urls}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/streams")
def list_streams():
    with get_db() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM streams ORDER BY id DESC").fetchall()]

@app.patch("/api/streams/{stream_id}")
def update_stream(stream_id: int, payload: StreamUpdate):
    from app.database import get_db, log_event
    with get_db() as conn:
        conn.execute("UPDATE streams SET label = ? WHERE id = ?", (payload.label, stream_id))
        conn.commit()
        log_event(f"Stream label updated: {payload.label}")
    return {"success": True}

@app.post("/api/streams")
def create_stream(payload: StreamCreate):
    from app.database import get_db, log_event
    # Accept either payload key from the frontend
    url_to_save = payload.stream_url if payload.stream_url else payload.url
    with get_db() as conn:
        conn.execute("INSERT INTO streams (label, stream_url) VALUES (?, ?)", (payload.label, url_to_save))
        conn.commit()
    log_event(f"Added new stream: {payload.label}")
    return {"success": True}
@app.delete("/api/streams/{stream_id}")
def delete_stream(stream_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM streams WHERE id = ?", (stream_id,))
        conn.commit()
    
    return {"success": True}

# Schedules API
@app.post("/api/record/start")
async def start_recording(payload: ManualRecordRequest):
    with get_db() as conn:
        stream = conn.execute("SELECT * FROM streams WHERE id = ?", (payload.stream_id,)).fetchone()
        if not stream:
            raise HTTPException(status_code=404, detail="Stream not found")
    
    recorder = StreamRecorder(stream["id"], stream["label"], stream["stream_url"], None)
    await recorder.start()
    return {"success": True, "recording_id": recorder.db_id}

@app.post("/api/record/stop/{recording_id}")
async def stop_recording(recording_id: int):
    if recording_id not in active_recordings:
        raise HTTPException(status_code=404, detail="Active recording not found")
    await active_recordings[recording_id].stop()
    return {"success": True}

@app.get("/api/recordings")
def get_recordings_api():
    import os, datetime
    from app.database import get_db
    results = []
    try:
        with get_db() as conn:
            row_set = conn.execute("SELECT value FROM settings WHERE key = 'recordings_dir'").fetchone()
            rec_dir = (row_set[0] if row_set else "/recordings") or "/recordings"

            rows = conn.execute("""
                SELECT 
                    r.id, 
                    r.stream_id, 
                    r.filename, 
                    r.filepath,
                    COALESCE(r.start_time, r.started_at, '') AS start_time, 
                    COALESCE(r.end_time, r.ended_at, '') AS end_time, 
                    COALESCE(r.duration_seconds, 0) AS duration_seconds, 
                    COALESCE(r.file_size_bytes, 0) AS file_size_bytes, 
                    r.status, 
                    COALESCE(s.label, 'Unknown Stream') AS stream_label
                FROM recordings r
                LEFT JOIN streams s ON r.stream_id = s.id
                ORDER BY r.id DESC
            """).fetchall()

            now_dt = datetime.datetime.now()
            
            for r in rows:
                rec_id = r[0]
                stream_id = r[1]
                filename = r[2] or "Recording in progress..."
                filepath = r[3] or os.path.join(rec_dir, filename)
                start_time_str = r[4]
                end_time_str = r[5]
                duration_sec = r[6]
                file_size = r[7]
                status = r[8]
                stream_label = r[9]

                # If actively recording, compute live duration and check disk file size
                if status == "recording":
                    if start_time_str:
                        try:
                            st_dt = datetime.datetime.fromisoformat(start_time_str.replace("Z", ""))
                            duration_sec = max(0, int((now_dt - st_dt).total_seconds()))
                        except Exception:
                            pass
                    
                    # Check disk file size live
                    if filepath and os.path.exists(filepath):
                        try:
                            file_size = os.path.getsize(filepath)
                        except Exception:
                            pass

                results.append({
                    "id": rec_id,
                    "stream_id": stream_id,
                    "filename": filename,
                    "filepath": filepath,
                    "start_time": start_time_str,
                    "end_time": end_time_str,
                    "duration_seconds": duration_sec,
                    "file_size_bytes": file_size,
                    "status": status,
                    "stream_label": stream_label
                })
    except Exception as e:
        print(f"Error fetching recordings: {e}", flush=True)
    return results

@app.delete("/api/recordings/{rec_id}")
async def delete_recording(rec_id: int):
    if rec_id in active_recordings:
        await active_recordings[rec_id].stop()

    with get_db() as conn:
        rec = conn.execute("SELECT filepath FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        if rec and Path(rec["filepath"]).exists():
            try:
                os.remove(rec["filepath"])
            except OSError:
                pass
        conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
        conn.commit()
    return {"success": True}

@app.get("/api/stream/audio/{filename}")
def stream_audio(filename: str, request: Request):
    safe_filename = os.path.basename(filename)
    rec_dir = Path(get_setting("recordings_dir", "/recordings"))
    file_path = rec_dir / safe_filename
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        byte_start, byte_end = 0, None
        match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            groups = match.groups()
            byte_start = int(groups[0])
            if groups[1]:
                byte_end = int(groups[1])

        if byte_end is None:
            byte_end = file_size - 1
        length = byte_end - byte_start + 1

        def iterfile():
            with open(file_path, "rb") as f:
                f.seek(byte_start)
                remaining = length
                while remaining > 0:
                    chunk_size = min(64 * 1024, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            "Content-Range": f"bytes {byte_start}-{byte_end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": "audio/mpeg",
        }
        return StreamingResponse(iterfile(), status_code=206, headers=headers)

    return StreamingResponse(open(file_path, "rb"), media_type="audio/mpeg")

# Settings API
@app.put("/api/settings")
def update_settings(settings: SettingsUpdate):
    set_setting("recordings_dir", settings.recordings_dir)
    set_setting("telegram_bot_token", settings.telegram_bot_token)
    set_setting("telegram_chat_id", settings.telegram_chat_id)
    return {"success": True}

@app.get("/", response_class=FileResponse)
def index():
    return FileResponse("app/static/index.html")



@app.get("/api/logs")
def get_logs():
    import os
    if not os.path.exists("/config/app.log"): return {"logs": "No application logs available yet."}
    with open("/config/app.log", "r") as f:
        return {"logs": "".join(f.readlines()[-200:])}


@app.get("/api/recordings/{rec_id}/play")
def play_recording(rec_id: int):
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    from app.database import get_db
    import os
    
    with get_db() as conn:
        rec = conn.execute("SELECT filepath FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        
    if rec and rec["filepath"] and os.path.exists(rec["filepath"]):
        return FileResponse(rec["filepath"], media_type="audio/mpeg")
        
    raise HTTPException(status_code=404, detail="Recording file not found on disk.")



@app.on_event("startup")
def rebuild_schedules_schema():
    from app.database import get_db
    with get_db() as conn:
        try:
            # Check if legacy cron_expression column exists; if so, nuke the table
            conn.execute("SELECT cron_expression FROM schedules LIMIT 1")
            conn.execute("DROP TABLE schedules")
        except Exception:
            pass
        
        # Build the exact modern schema
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        ''')
        conn.commit()

@app.get("/api/schedules")
def get_schedules():
    from app.database import get_db
    with get_db() as conn:
        try:
            # Safely JOIN to the streams table to extract the stream_label
            schedules = conn.execute('''
                SELECT s.id, s.stream_id, s.start_time, s.end_time, s.status, s.description, st.label as stream_label 
                FROM schedules s 
                LEFT JOIN streams st ON s.stream_id = st.id 
                ORDER BY s.id DESC
            ''').fetchall()
            return [dict(r) for r in schedules]
        except Exception as e:
            return []

@app.get("/api/recordings/{rec_id}/download")
def download_recording(rec_id: int):
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    from app.database import get_db
    import os
    
    with get_db() as conn:
        rec = conn.execute("SELECT filepath, filename FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        
    if rec and rec["filepath"] and os.path.exists(rec["filepath"]):
        # Passing 'filename' forces the browser to download rather than stream
        return FileResponse(path=rec["filepath"], media_type="audio/mpeg", filename=rec["filename"])
        
    raise HTTPException(status_code=404, detail="Recording file not found on disk.")

@app.post("/api/purge")
def purge_database():
    from app.database import get_db, log_event
    try:
        with get_db() as conn:
            # Wipe all application data from the SQLite tables
            conn.execute("DELETE FROM streams")
            conn.execute("DELETE FROM recordings")
            conn.execute("DELETE FROM schedules")
            conn.execute("DELETE FROM settings")
            conn.commit()
            
        try:
            from app.scheduler import scheduler
            scheduler.remove_all_jobs()
        except: pass
        
        log_event("SYSTEM PURGE: Database factory reset executed by user.")
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def start_recording_job(stream_id: int, duration_minutes: int = None, description: str = ""):
    from app.recorder import StreamRecorder, active_recordings
    from app.database import get_db, log_event
    from app.notifier import send_telegram_notification
    import datetime

    try:
        with get_db() as conn:
            stream = conn.execute("SELECT id, stream_url, label FROM streams WHERE id = ?", (stream_id,)).fetchone()
            if not stream:
                log_event(f"Scheduled recording failed: Stream {stream_id} not found.")
                return

            stream_url = stream["stream_url"] if hasattr(stream, "keys") and "stream_url" in stream.keys() else stream[1]
            stream_label = stream["label"] if hasattr(stream, "keys") and "label" in stream.keys() else stream[2]

            row = conn.execute("SELECT value FROM settings WHERE key = 'recordings_dir'").fetchone()
            rec_dir = (row[0] if row else "/recordings") or "/recordings"

            cursor = conn.cursor()
            now_dt = datetime.datetime.now()
            timestamp = now_dt.strftime("%Y-%m-%d_%H-%M-%S")
            safe_label = "".join(c for c in str(stream_label) if c.isalnum() or c in (" ", "_", "-")).rstrip().replace(" ", "_") or f"stream_{stream_id}"
            initial_filename = f"{safe_label}_{timestamp}.mp3"
            initial_filepath = f"{rec_dir.rstrip('/')}/{initial_filename}"

            cursor.execute(
                "INSERT INTO recordings (stream_id, filename, filepath, status, start_time) VALUES (?, ?, ?, 'recording', ?)",
                (stream_id, initial_filename, initial_filepath, now_dt.isoformat())
            )
            recording_id = cursor.lastrowid
            conn.commit()

        recorder = StreamRecorder(
            recording_id=recording_id,
            stream_id=stream_id,
            stream_url=stream_url,
            label=stream_label,
            duration_minutes=duration_minutes,
            description=description
        )
        active_recordings[recording_id] = recorder

        try:
            desc_text = f" ({description})" if description else ""
            start_str = datetime.datetime.now().strftime("%I:%M %p")
            end_time_dt = datetime.datetime.now() + datetime.timedelta(minutes=(duration_minutes or 60))
            end_str = end_time_dt.strftime("%I:%M %p")
            send_telegram_notification(
                "notif_sched_start",
                stream_label=stream_label,
                desc_text=desc_text,
                start_str=start_str,
                end_str=end_str
            )
        except Exception:
            pass

        await recorder.start()
    except Exception as e:
        log_event(f"Error in start_recording_job: {e}")


@app.get("/api/settings")
def get_settings_api():
    from app.database import get_db
    settings_dict = {}
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            for r in rows:
                settings_dict[r[0]] = r[1]
    except Exception as e:
        print(f"Error reading settings: {e}", flush=True)
    
    defaults = {
        "recordings_dir": "/recordings",
        "telegram_token": "",
        "telegram_chat_id": "",
        "notif_manual_start": "false",
        "notif_manual_stop": "false",
        "notif_sched_start": "false",
        "notif_sched_stop": "false",
        "notif_stream_connected": "false",
        "notif_stream_disconnected": "false"
    }
    defaults.update(settings_dict)
    return defaults

@app.post("/api/settings")
async def save_settings_api(request: Request):
    try:
        payload = await request.json()
        from app.database import set_setting, log_event
        for k, v in payload.items():
            set_setting(k, str(v) if v is not None else "")
        log_event("Global settings updated.")
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/settings/test-telegram")
async def test_telegram_api(request: Request):
    try:
        payload = await request.json()
        from app.notifier import send_telegram_notification
        send_telegram_notification(
            payload.get("notif_type", ""), 
            payload.get("title", "Test"), 
            payload.get("description", "Test"), 
            force=True
        )
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

from fastapi import Request
from fastapi.responses import JSONResponse

@app.get("/api/sys_settings")
def get_sys_settings():
    from app.database import get_db
    d = {}
    try:
        with get_db() as conn:
            for row in conn.execute("SELECT key, value FROM settings").fetchall():
                d[row[0]] = row[1]
    except Exception: pass
    
    return {
        "recordings_dir": d.get("recordings_dir", "/recordings"),
        "telegram_token": d.get("telegram_token", ""),
        "telegram_chat_id": d.get("telegram_chat_id", ""),
        "notif_manual_start": d.get("notif_manual_start", "false"),
        "notif_manual_stop": d.get("notif_manual_stop", "false"),
        "notif_sched_start": d.get("notif_sched_start", "false"),
        "notif_sched_stop": d.get("notif_sched_stop", "false"),
        "notif_stream_connected": d.get("notif_stream_connected", "false"),
        "notif_stream_disconnected": d.get("notif_stream_disconnected", "false")
    }

@app.post("/api/sys_settings")
async def post_sys_settings(request: Request):
    payload = await request.json()
    from app.database import get_db
    try:
        with get_db() as conn:
            for k, v in payload.items():
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (str(k), str(v)))
            conn.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

@app.post("/api/sys_settings/test")
async def test_sys_settings(request: Request):
    payload = await request.json()
    from app.notifier import send_telegram_notification
    mock_data = {
        "stream_label": "Example Station",
        "desc_text": "(Morning Show)",
        "start_str": "07:00 PM",
        "end_str": "10:00 PM",
        "now_str": "10:00 PM"
    }
    send_telegram_notification(payload.get("notif_type", ""), force=True, **mock_data)
    return JSONResponse({"success": True})




@app.post("/api/schedules")
async def create_schedule(request: Request):
    from app.database import get_db, log_event
    from app.scheduler import schedule_job
    try:
        data = await request.json()
        stream_id = data.get("stream_id")
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        description = data.get("description", "")

        if not stream_id or not start_time or not end_time:
            return JSONResponse({"success": False, "error": "Missing required fields"}, status_code=400)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO schedules (stream_id, start_time, end_time, description) VALUES (?, ?, ?, ?)",
                (stream_id, start_time, end_time, description)
            )
            schedule_id = cursor.lastrowid
            conn.commit()

        # Enqueue immediately into the running APScheduler
        schedule_job(schedule_id, stream_id, start_time, end_time, description)
        log_event(f"Created schedule #{schedule_id} for stream {stream_id}")
        return JSONResponse({"success": True, "id": schedule_id})
    except Exception as e:
        log_event(f"Failed to create schedule: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.patch("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, request: Request):
    from app.database import get_db, log_event
    from app.scheduler import schedule_job, remove_scheduled_job
    try:
        data = await request.json()
        stream_id = data.get("stream_id")
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        description = data.get("description", "")

        with get_db() as conn:
            conn.execute(
                "UPDATE schedules SET stream_id = ?, start_time = ?, end_time = ?, description = ? WHERE id = ?",
                (stream_id, start_time, end_time, description, schedule_id)
            )
            conn.commit()

        remove_scheduled_job(schedule_id)
        schedule_job(schedule_id, stream_id, start_time, end_time, description)
        log_event(f"Updated schedule #{schedule_id}")
        return JSONResponse({"success": True})
    except Exception as e:
        log_event(f"Failed to update schedule #{schedule_id}: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.delete("/api/schedules/{schedule_id}")
def delete_schedule_api(schedule_id: int):
    from app.database import get_db, log_event
    from app.scheduler import remove_scheduled_job
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            conn.commit()
        remove_scheduled_job(schedule_id)
        log_event(f"Deleted schedule #{schedule_id}")
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
