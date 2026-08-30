import logging
import os
import re
import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/recordings" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

from app.database import init_db, recover_interrupted_recordings, get_db, get_setting, set_setting, log_event
from app.scheduler import init_scheduler, schedule_job, remove_scheduled_job
from app.sniffer import sniff_stream_url
from app.recorder import StreamRecorder, active_recordings
from app.transcriber import transcribe_audio
from app.highlighter import process_highlight_task, get_highlight_prompt, get_prompt_filepath

def rebuild_schedules_schema():
    with get_db() as conn:
        try:
            conn.execute("SELECT cron_expression FROM schedules LIMIT 1")
            conn.execute("DROP TABLE schedules")
        except Exception:
            pass
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                description TEXT DEFAULT "",
                status TEXT DEFAULT 'pending',
                FOREIGN KEY(stream_id) REFERENCES streams(id) ON DELETE CASCADE
            )
        ''')
        conn.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    rebuild_schedules_schema()
    recover_interrupted_recordings()
    init_scheduler()
    yield

app = FastAPI(title="Stream Recorder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

class StreamCreate(BaseModel):
    label: str
    stream_url: str

class StreamUpdate(BaseModel):
    label: str

class StreamProbe(BaseModel):
    url: str

class ManualRecordRequest(BaseModel):
    stream_id: int

class ModelCacheRequest(BaseModel):
    model_size: str

def resolve_recording_path(rec: dict) -> Optional[Path]:
    if rec.get("filepath"):
        candidate = Path(rec["filepath"])
        if candidate.exists() and candidate.is_file():
            return candidate
    if rec.get("filename"):
        rec_dir = Path(get_setting("recordings_dir", "/recordings"))
        candidate = rec_dir / os.path.basename(rec["filename"])
        if candidate.exists() and candidate.is_file():
            return candidate
    return None

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
    with get_db() as conn:
        conn.execute("UPDATE streams SET label = ? WHERE id = ?", (payload.label, stream_id))
        conn.commit()
        log_event(f"Stream label updated: {payload.label}")
    return {"success": True}

@app.post("/api/streams")
def create_stream(payload: StreamCreate):
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

@app.post("/api/record/start")
async def start_recording(payload: ManualRecordRequest):
    with get_db() as conn:
        stream = conn.execute("SELECT * FROM streams WHERE id = ?", (payload.stream_id,)).fetchone()
        if not stream:
            raise HTTPException(status_code=404, detail="Stream not found")
        
        cursor = conn.cursor()
        now_dt = datetime.datetime.now()
        cursor.execute(
            "INSERT INTO recordings (stream_id, filename, filepath, status, start_time) VALUES (?, '', '', 'pending', ?)",
            (payload.stream_id, now_dt.isoformat())
        )
        recording_id = cursor.lastrowid
        conn.commit()
    
    recorder = StreamRecorder(
        recording_id=recording_id,
        stream_id=stream["id"],
        stream_url=stream["stream_url"],
        label=stream["label"]
    )
    
    active_recordings[recording_id] = recorder

    try:
        from app.notifier import send_telegram_notification
        start_str = datetime.datetime.now().strftime("%I:%M %p")
        send_telegram_notification(
            "notif_manual_start",
            stream_label=stream["label"],
            start_str=start_str
        )
    except Exception:
        pass

    await recorder.start()
    return {"success": True, "recording_id": recording_id}

@app.post("/api/record/stop/{recording_id}")
async def stop_recording(recording_id: int):
    if recording_id not in active_recordings:
        raise HTTPException(status_code=404, detail="Active recording not found")
    await active_recordings[recording_id].stop()
    active_recordings.pop(recording_id, None)
    return {"success": True}

@app.post("/api/recordings/{rec_id}/transcribe")
async def trigger_transcription(rec_id: int):
    with get_db() as conn:
        rec = conn.execute("SELECT filepath, filename, status FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    
    if not rec or rec["status"] != "completed":
        raise HTTPException(status_code=400, detail="Recording is not ready for transcription.")
        
    file_path = resolve_recording_path(dict(rec))
    if not file_path:
        raise HTTPException(status_code=404, detail="Recording file not found on disk.")
        
    import asyncio
    asyncio.create_task(transcribe_audio(rec_id, str(file_path)))
    return {"success": True}

@app.post("/api/recordings/{rec_id}/highlight")
async def trigger_highlight(rec_id: int):
    with get_db() as conn:
        rec = conn.execute("SELECT filepath, filename, transcript_path, transcription_status FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    
    if not rec or rec["transcription_status"] != "completed" or not rec["transcript_path"]:
        raise HTTPException(status_code=400, detail="Transcript is required before generating highlights.")
        
    file_path = resolve_recording_path(dict(rec))
    if not file_path:
        raise HTTPException(status_code=404, detail="Recording file not found on disk.")
        
    import asyncio
    asyncio.create_task(process_highlight_task(rec_id, str(file_path), rec["transcript_path"]))
    return {"success": True}

@app.get("/api/recordings")
def get_recordings_api():
    results = []
    try:
        with get_db() as conn:
            row_set = conn.execute("SELECT value FROM settings WHERE key = 'recordings_dir'").fetchone()
            rec_dir = (row_set[0] if row_set else "/recordings") or "/recordings"

            rows = conn.execute("""
                SELECT 
                    r.id, r.stream_id, r.filename, r.filepath,
                    COALESCE(r.start_time, r.started_at, '') AS start_time, 
                    COALESCE(r.end_time, r.ended_at, '') AS end_time, 
                    COALESCE(r.duration_seconds, 0) AS duration_seconds, 
                    COALESCE(r.file_size_bytes, 0) AS file_size_bytes, 
                    r.status, COALESCE(s.label, 'Unknown Stream') AS stream_label,
                    COALESCE(r.transcription_status, 'none') AS transcription_status,
                    r.transcript_path, COALESCE(r.transcription_progress, 0) AS transcription_progress,
                    COALESCE(r.highlight_status, 'none') AS highlight_status,
                    COALESCE(r.highlight_progress, 0) AS highlight_progress,
                    r.highlight_path
                FROM recordings r
                LEFT JOIN streams s ON r.stream_id = s.id
                ORDER BY r.id DESC
            """).fetchall()

            now_dt = datetime.datetime.now()
            deleted_any = False
            
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
                trans_status = r[10]
                trans_path = r[11]
                trans_prog = r[12]
                hl_status = r[13]
                hl_prog = r[14]
                hl_path = r[15]

                if status == "completed":
                    file_exists = False
                    if filepath and os.path.exists(filepath):
                        file_exists = True
                    elif filename:
                        fallback_path = os.path.join(rec_dir, os.path.basename(filename))
                        if os.path.exists(fallback_path):
                            file_exists = True
                    
                    if not file_exists:
                        if trans_path and os.path.exists(trans_path):
                            try: os.remove(trans_path)
                            except: pass
                        if hl_path and os.path.exists(hl_path):
                            try: os.remove(hl_path)
                            except: pass
                            
                        conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
                        deleted_any = True
                        continue

                if status == "recording":
                    if start_time_str:
                        try:
                            st_dt = datetime.datetime.fromisoformat(start_time_str.replace("Z", ""))
                            duration_sec = max(0, int((now_dt - st_dt).total_seconds()))
                        except Exception:
                            pass
                    
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
                    "stream_label": stream_label,
                    "transcription_status": trans_status,
                    "transcript_path": trans_path,
                    "transcription_progress": trans_prog,
                    "highlight_status": hl_status,
                    "highlight_progress": hl_prog,
                    "highlight_path": hl_path
                })
            
            if deleted_any:
                conn.commit()
    except Exception as e:
        print(f"Error fetching recordings: {e}", flush=True)
    return results

@app.delete("/api/recordings/{rec_id}")
async def delete_recording(rec_id: int):
    if rec_id in active_recordings:
        await active_recordings[rec_id].stop()
        active_recordings.pop(rec_id, None)

    with get_db() as conn:
        rec = conn.execute("SELECT filepath, filename, transcript_path, highlight_path FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        if rec:
            file_path = resolve_recording_path(dict(rec))
            if file_path and file_path.exists():
                try: os.remove(file_path)
                except: pass
            if rec["transcript_path"] and os.path.exists(rec["transcript_path"]):
                try: os.remove(rec["transcript_path"])
                except: pass
            if rec["highlight_path"] and os.path.exists(rec["highlight_path"]):
                try: os.remove(rec["highlight_path"])
                except: pass
        conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
        conn.commit()
    return {"success": True}

@app.get("/", response_class=FileResponse)
def index():
    return FileResponse("app/static/index.html")

@app.get("/api/logs")
def get_logs():
    if not os.path.exists("/config/app.log"): 
        return {"logs": "No application logs available yet."}
    with open("/config/app.log", "r") as f:
        return {"logs": "".join(f.readlines()[-200:])}

@app.get("/api/recordings/{rec_id}/play")
def play_recording(rec_id: int):
    with get_db() as conn:
        rec = conn.execute("SELECT filepath, filename FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    if not rec: raise HTTPException(status_code=404)
    file_path = resolve_recording_path(dict(rec))
    if file_path: return FileResponse(path=str(file_path), media_type="audio/mpeg")
    raise HTTPException(status_code=404)

@app.get("/api/recordings/{rec_id}/download")
def download_recording(rec_id: int):
    with get_db() as conn:
        rec = conn.execute("SELECT filepath, filename FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    if not rec: raise HTTPException(status_code=404)
    file_path = resolve_recording_path(dict(rec))
    if file_path:
        download_name = rec["filename"] or file_path.name
        return FileResponse(path=str(file_path), media_type="audio/mpeg", filename=download_name)
    raise HTTPException(status_code=404)

@app.get("/api/recordings/{rec_id}/transcript")
def download_transcript(rec_id: int):
    with get_db() as conn:
        rec = conn.execute("SELECT transcript_path, filename FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    if not rec or not rec["transcript_path"] or not os.path.exists(rec["transcript_path"]): raise HTTPException(status_code=404)
    download_name = (rec["filename"] or "transcript").rsplit(".", 1)[0] + ".txt"
    return FileResponse(path=rec["transcript_path"], media_type="text/plain", filename=download_name)

@app.get("/api/recordings/{rec_id}/highlight/play")
def play_highlight(rec_id: int):
    with get_db() as conn:
        rec = conn.execute("SELECT highlight_path FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    if not rec or not rec["highlight_path"] or not os.path.exists(rec["highlight_path"]): raise HTTPException(status_code=404)
    return FileResponse(path=rec["highlight_path"], media_type="audio/mpeg")

@app.get("/api/recordings/{rec_id}/highlight/download")
def download_highlight(rec_id: int):
    with get_db() as conn:
        rec = conn.execute("SELECT highlight_path, filename FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    if not rec or not rec["highlight_path"] or not os.path.exists(rec["highlight_path"]): raise HTTPException(status_code=404)
    download_name = (rec["filename"] or "audio").rsplit(".", 1)[0] + "_highlight.mp3"
    return FileResponse(path=rec["highlight_path"], media_type="audio/mpeg", filename=download_name)

@app.get("/api/schedules")
def get_schedules():
    with get_db() as conn:
        try:
            schedules = conn.execute('''
                SELECT s.id, s.stream_id, s.start_time, s.end_time, s.status, s.description, st.label as stream_label 
                FROM schedules s 
                LEFT JOIN streams st ON s.stream_id = st.id 
                ORDER BY s.id DESC
            ''').fetchall()
            return [dict(r) for r in schedules]
        except Exception: return []

@app.post("/api/purge")
def purge_database():
    try:
        with get_db() as conn:
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

@app.get("/api/sys_settings")
def get_sys_settings():
    d = {}
    try:
        with get_db() as conn:
            for row in conn.execute("SELECT key, value FROM settings").fetchall():
                d[row[0]] = row[1]
    except Exception: pass
    
    prompt_text = get_highlight_prompt()
            
    return {
        "recordings_dir": d.get("recordings_dir", "/recordings"),
        "telegram_token": d.get("telegram_token", ""),
        "telegram_chat_id": d.get("telegram_chat_id", ""),
        "notif_manual_start": d.get("notif_manual_start", "false"),
        "notif_manual_stop": d.get("notif_manual_stop", "false"),
        "notif_sched_start": d.get("notif_sched_start", "false"),
        "notif_sched_stop": d.get("notif_sched_stop", "false"),
        "notif_stream_connected": d.get("notif_stream_connected", "false"),
        "notif_stream_disconnected": d.get("notif_stream_disconnected", "false"),
        "auto_transcribe": d.get("auto_transcribe", "false"),
        "auto_highlight": d.get("auto_highlight", "false"),
        "whisper_model": d.get("whisper_model", "base.en"),
        "max_concurrent_transcriptions": d.get("max_concurrent_transcriptions", "1"),
        "max_concurrent_highlights": d.get("max_concurrent_highlights", "1"),
        "highlight_prompt_file": d.get("highlight_prompt_file", "highlight_prompt.txt"),
        "highlight_prompt": prompt_text
    }

@app.post("/api/sys_settings")
async def post_sys_settings(request: Request):
    payload = await request.json()
    try:
        prompt_content = payload.pop("highlight_prompt", None)
        prompt_file = payload.get("highlight_prompt_file", None)

        with get_db() as conn:
            for k, v in payload.items():
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (str(k), str(v)))
            conn.commit()

        if prompt_content is not None:
            config_dir = os.getenv("CONFIG_DIR", "/config")
            target_filename = os.path.basename((prompt_file or get_setting("highlight_prompt_file", "highlight_prompt.txt")).strip() or "highlight_prompt.txt")
            target_path = os.path.join(config_dir, target_filename)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(prompt_content)

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

@app.post("/api/model/cache")
async def cache_ai_model(req: ModelCacheRequest):
    try:
        from app.transcriber import force_cache_model
        import asyncio
        await asyncio.to_thread(force_cache_model, req.model_size)
        log_event(f"Successfully verified/cached Whisper model: {req.model_size}")
        return JSONResponse({"success": True})
    except Exception as e:
        log_event(f"Model cache failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.post("/api/schedules")
async def create_schedule(request: Request):
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

        schedule_job(schedule_id, stream_id, start_time, end_time, description)
        log_event(f"Created schedule #{schedule_id} for stream {stream_id}")
        return JSONResponse({"success": True, "id": schedule_id})
    except Exception as e:
        log_event(f"Failed to create schedule: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.patch("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, request: Request):
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
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            conn.commit()
        remove_scheduled_job(schedule_id)
        log_event(f"Deleted schedule #{schedule_id}")
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)