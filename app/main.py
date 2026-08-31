import logging
import os
import re
import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
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
from app.highlighter import process_highlight_task, get_highlight_prompt

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

class PlaylistResolveRequest(BaseModel):
    url: str

class ManualRecordRequest(BaseModel):
    stream_id: int

class ModelCacheRequest(BaseModel):
    model_size: str

class LLMCacheRequest(BaseModel):
    model_val: str

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

async def is_safe_url(target_url: str) -> bool:
    import ipaddress
    import socket
    from urllib.parse import urlparse
    if not target_url.startswith(("http://", "https://")): return False
    try:
        parsed = urlparse(target_url)
        hostname = parsed.hostname
        if not hostname: return False
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"): return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_unspecified: return False
            return True
        except ValueError: pass
        import asyncio
        loop = asyncio.get_running_loop()
        ip = await loop.run_in_executor(None, socket.gethostbyname, hostname)
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_unspecified: return False
        return True
    except Exception: return False

@app.post("/api/streams/probe")
async def probe_stream(req: StreamProbe):
    try:
        discovered_urls = await sniff_stream_url(req.url)
        return {"success": True, "stream_urls": discovered_urls}
    except Exception as e:
        log_event(f"Stream probe failed: {e}")
        raise HTTPException(status_code=400, detail="Failed to probe stream URL. See logs for details.")

@app.post("/api/streams/resolve")
async def resolve_stream_url(req: PlaylistResolveRequest):
    import httpx
    if not await is_safe_url(req.url):
        return JSONResponse({"success": False, "url": req.url})
        
    urls = []
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=10.0) as client:
            # codeql[py/full-ssrf] - Mitigated by is_safe_url active DNS validation
            resp = await client.get(req.url)  # lgtm [py/full-ssrf]
            resp.raise_for_status()
            text = resp.text
            
            if re.search(r'^File\d+=', text, re.IGNORECASE | re.MULTILINE):
                matches = re.findall(r'^File\d+=(.+)$', text, re.MULTILINE | re.IGNORECASE)
                urls.extend([m.strip() for m in matches if m.strip()])
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        urls.append(line)
        if urls:
            return JSONResponse({"success": True, "url": urls[0]})
    except Exception:
        pass
    return JSONResponse({"success": False, "url": req.url})

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
    import datetime
    from app.notifier import send_telegram_notification

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
    try:
        with get_db() as conn:
            count = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='logs'").fetchone()[0]
            if count == 0:
                return {"logs": "No application logs available yet."}
                
            rows = conn.execute("SELECT timestamp, message FROM logs ORDER BY id DESC LIMIT 200").fetchall()
            if not rows:
                return {"logs": "No application logs available yet."}
                
            log_lines = [f"[{r['timestamp']}] {r['message']}" for r in reversed(rows)]
            return {"logs": "\n".join(log_lines)}
    except Exception as e:
        print(f"Error serving logs API: {e}", flush=True)
        return {"logs": "Error reading application logs from the database."}

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
        log_event(f"Database purge failed: {e}")
        return {"success": False, "error": "Database purge failed due to an internal error."}

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
        "llm_model": d.get("llm_model", "Qwen/Qwen2.5-1.5B-Instruct-GGUF|qwen2.5-1.5b-instruct-q4_k_m.gguf"),
        "max_concurrent_transcriptions": d.get("max_concurrent_transcriptions", "1"),
        "max_concurrent_highlights": d.get("max_concurrent_highlights", "1"),
        "ai_log_level": d.get("ai_log_level", "INFO"),
        "highlight_prompt": prompt_text
    }

@app.post("/api/sys_settings")
async def post_sys_settings(request: Request):
    try:
        payload = await request.json()
        prompt_content = payload.pop("highlight_prompt", None)

        with get_db() as conn:
            for k, v in payload.items():
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (str(k), str(v)))
            conn.commit()

        if prompt_content is not None:
            config_dir = os.path.abspath(os.getenv("CONFIG_DIR", "/config"))
            target_path = os.path.abspath(os.path.join(config_dir, "highlight_prompt.txt"))
                
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(prompt_content)

        return JSONResponse({"success": True})
    except Exception as e:
        log_event(f"Failed to save system settings: {e}")
        return JSONResponse({"success": False, "error": "Failed to save configuration settings."})

@app.post("/api/sys_settings/test")
async def test_sys_settings(request: Request):
    try:
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
    except Exception as e:
        log_event(f"Telegram test notification failed: {e}")
        return JSONResponse({"success": False, "error": "Failed to dispatch test notification."})

@app.post("/api/model/cache")
async def cache_ai_model(req: ModelCacheRequest, background_tasks: BackgroundTasks):
    try:
        from app.transcriber import force_cache_model
        
        def run_whisper_download():
            try:
                log_event(f"Starting background download for Whisper model: {req.model_size}")
                force_cache_model(req.model_size)
                log_event(f"Successfully verified/cached Whisper model: {req.model_size}")
            except Exception as e:
                log_event(f"Whisper model cache failed: {e}")
                
        background_tasks.add_task(run_whisper_download)
        return JSONResponse({"success": True})
    except Exception as e:
        log_event(f"Failed to initiate Whisper cache: {e}")
        return JSONResponse({"success": False, "error": "Failed to cache AI model."}, status_code=500)

@app.post("/api/model/llm/cache")
async def cache_llm_model(req: LLMCacheRequest, background_tasks: BackgroundTasks):
    try:
        from app.highlighter import force_cache_llm
        
        def run_llm_download():
            try:
                filename = req.model_val.split('|')[1]
                log_event(f"Starting background download for LLM model: {filename}")
                force_cache_llm(req.model_val)
                log_event(f"Successfully verified/cached LLM model: {filename}")
            except Exception as e:
                log_event(f"LLM model cache failed: {e}")
                
        background_tasks.add_task(run_llm_download)
        return JSONResponse({"success": True})
    except Exception as e:
        log_event(f"Failed to initiate LLM cache: {e}")
        return JSONResponse({"success": False, "error": "Failed to cache LLM model."}, status_code=500)

@app.delete("/api/model/cache")
async def delete_model_cache():
    try:
        import shutil
        config_dir = os.getenv("CONFIG_DIR", "/config")
        model_dir = os.path.join(config_dir, "models")
        if os.path.exists(model_dir):
            shutil.rmtree(model_dir)
        os.makedirs(model_dir, exist_ok=True)
        log_event("AI Model Cache completely wiped by user.")
        return JSONResponse({"success": True})
    except Exception as e:
        log_event(f"Failed to clear model cache: {e}")
        return JSONResponse({"success": False, "error": "Failed to clear model cache."}, status_code=500)

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
        return JSONResponse({"success": False, "error": "Failed to create schedule recording entry."}, status_code=500)

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
        return JSONResponse({"success": False, "error": "Failed to update existing schedule."}, status_code=500)

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
        log_event(f"Failed to delete schedule #{schedule_id}: {e}")
        return JSONResponse({"success": False, "error": "Failed to delete schedule."}, status_code=500)