import asyncio
import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.database import get_db, log_event
from app.notifier import send_telegram_notification

scheduler = AsyncIOScheduler(timezone="UTC")

def parse_to_utc(dt_str: str) -> datetime.datetime:
    if not dt_str:
        return None
    dt_str = dt_str.strip()
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(dt_str)
    except Exception:
        dt = datetime.datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
    
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt

async def trigger_scheduled_recording(schedule_id: int, stream_id: int, duration_minutes: int, description: str):
    from app.recorder import StreamRecorder, active_recordings
    print(f"[+] TRIGGERING SCHEDULE #{schedule_id} for stream {stream_id} ({duration_minutes} min)", flush=True)
    
    try:
        with get_db() as conn:
            stream = conn.execute("SELECT id, stream_url, label FROM streams WHERE id = ?", (stream_id,)).fetchone()
            if not stream:
                log_event(f"Schedule #{schedule_id} failed: Stream ID {stream_id} not found.")
                return

            stream_url = stream["stream_url"] if hasattr(stream, "keys") and "stream_url" in stream.keys() else stream[1]
            stream_label = stream["label"] if hasattr(stream, "keys") and "label" in stream.keys() else stream[2]

            # Fetch recordings directory to compute full filepath
            row = conn.execute("SELECT value FROM settings WHERE key = 'recordings_dir'").fetchone()
            rec_dir = (row[0] if row else "/recordings") or "/recordings"
            
            cursor = conn.cursor()
            now_dt = datetime.datetime.now()
            timestamp = now_dt.strftime("%Y-%m-%d_%H-%M-%S")
            safe_label = "".join(c for c in str(stream_label) if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_") or f"stream_{stream_id}"
            safe_desc = "".join(c for c in str(description or "") if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
            if safe_desc:
                initial_filename = f"{safe_label}_{safe_desc}_{timestamp}.mp3"
            else:
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

        desc_text = f" ({description})" if description else ""
        start_str = datetime.datetime.now().strftime("%I:%M %p")
        end_time_dt = datetime.datetime.now() + datetime.timedelta(minutes=duration_minutes)
        end_str = end_time_dt.strftime("%I:%M %p")

        try:
            send_telegram_notification(
                "notif_sched_start",
                stream_label=stream_label,
                desc_text=desc_text,
                start_str=start_str,
                end_str=end_str
            )
        except Exception as err:
            log_event(f"Start notif error: {err}")

        log_event(f"Started scheduled recording #{recording_id} for '{stream_label}' ({duration_minutes}m)")
        await recorder.start()

    except Exception as e:
        print(f"[!] Error in trigger_scheduled_recording: {e}", flush=True)
        log_event(f"Schedule #{schedule_id} error: {e}")

def schedule_job(schedule_id: int, stream_id: int, start_time_iso: str, end_time_iso: str, description: str):
    job_id = f"sched_{schedule_id}"
    remove_scheduled_job(schedule_id)

    try:
        start_dt = parse_to_utc(start_time_iso)
        end_dt = parse_to_utc(end_time_iso)
        if not start_dt or not end_dt:
            return

        diff_seconds = (end_dt - start_dt).total_seconds()
        duration_minutes = max(1, int(diff_seconds / 60)) if diff_seconds > 0 else 60
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        if start_dt > now_utc:
            scheduler.add_job(
                trigger_scheduled_recording,
                'date',
                run_date=start_dt,
                args=[schedule_id, stream_id, duration_minutes, description],
                id=job_id,
                replace_existing=True
            )
            print(f"[+] Enqueued schedule #{schedule_id} for {start_dt.isoformat()} UTC (duration: {duration_minutes}m)", flush=True)
            log_event(f"Enqueued schedule #{schedule_id} for {start_dt.isoformat()} UTC")
        else:
            print(f"[-] Skipped schedule #{schedule_id}: Start time {start_dt.isoformat()} is in the past (Current UTC: {now_utc.isoformat()}).", flush=True)

    except Exception as e:
        print(f"[!] schedule_job error: {e}", flush=True)
        log_event(f"Failed to register job for schedule #{schedule_id}: {e}")

def remove_scheduled_job(schedule_id: int):
    job_id = f"sched_{schedule_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

def load_schedules():
    with get_db() as conn:
        try:
            rows = conn.execute("SELECT id, stream_id, start_time, end_time, description FROM schedules").fetchall()
            for r in rows:
                sid = r["id"] if hasattr(r, "keys") and "id" in r.keys() else r[0]
                stream_id = r["stream_id"] if hasattr(r, "keys") and "stream_id" in r.keys() else r[1]
                start_time = r["start_time"] if hasattr(r, "keys") and "start_time" in r.keys() else r[2]
                end_time = r["end_time"] if hasattr(r, "keys") and "end_time" in r.keys() else r[3]
                desc = r["description"] if hasattr(r, "keys") and "description" in r.keys() else (r[4] if len(r) > 4 else "")
                schedule_job(sid, stream_id, start_time, end_time, desc or "")
        except Exception as e:
            print(f"[!] load_schedules error: {e}", flush=True)

def init_scheduler():
    if not scheduler.running:
        scheduler.start()
    load_schedules()
