from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.database import get_db
from app.recorder import StreamRecorder

scheduler = AsyncIOScheduler()

def start_scheduler():
    if not scheduler.running:
        scheduler.start()
    sync_scheduled_jobs()

def sync_scheduled_jobs():
    scheduler.remove_all_jobs()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.cron_expression, s.duration_minutes, st.id AS stream_id, st.label, st.stream_url 
            FROM schedules s 
            JOIN streams st ON s.stream_id = st.id 
            WHERE s.is_active = 1
        """)
        for job in cursor.fetchall():
            if job["cron_expression"]:
                trigger = CronTrigger.from_crontab(job["cron_expression"])
                scheduler.add_job(
                    _execute_scheduled_capture,
                    trigger=trigger,
                    id=f"schedule_{job['id']}",
                    args=[job["stream_id"], job["label"], job["stream_url"], job["duration_minutes"]],
                    replace_existing=True
                )

async def _execute_scheduled_capture(stream_id: int, label: str, stream_url: str, duration: int):
    recorder = StreamRecorder(stream_id, label, stream_url, duration_minutes=duration)
    await recorder.start()
