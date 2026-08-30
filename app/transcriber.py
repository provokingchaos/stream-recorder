import asyncio
import os
from faster_whisper import WhisperModel
from app.database import get_db, log_event, get_setting

_model = None
_loaded_model_size = None
_transcription_sem = None
_current_transcription_limit = 1

def get_transcription_semaphore():
    global _transcription_sem, _current_transcription_limit
    try:
        limit = int(get_setting("max_concurrent_transcriptions", "1"))
    except ValueError:
        limit = 1
    limit = max(1, limit)
    if _transcription_sem is None or limit != _current_transcription_limit:
        _current_transcription_limit = limit
        _transcription_sem = asyncio.Semaphore(_current_transcription_limit)
    return _transcription_sem

def force_cache_model(model_size: str):
    from faster_whisper.utils import download_model
    config_dir = os.getenv("CONFIG_DIR", "/config")
    model_dir = os.path.join(config_dir, "models")
    os.makedirs(model_dir, exist_ok=True)
    download_model(model_size, cache_dir=model_dir)

def _run_transcription(recording_id: int, filepath: str, transcript_path: str):
    global _model, _loaded_model_size
    try:
        model_size = get_setting("whisper_model", "base.en")
        
        if _model is None or _loaded_model_size != model_size:
            config_dir = os.getenv("CONFIG_DIR", "/config")
            model_dir = os.path.join(config_dir, "models")
            os.makedirs(model_dir, exist_ok=True)
            _model = WhisperModel(model_size, device="cpu", compute_type="int8", download_root=model_dir)
            _loaded_model_size = model_size
        
        segments, info = _model.transcribe(filepath, beam_size=5)
        total_duration = info.duration if info.duration and info.duration > 0 else 1.0
        last_pct = 0
        
        with open(transcript_path, "w", encoding="utf-8") as f:
            for segment in segments:
                f.write(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}\n")
                
                pct = min(99, max(0, int((segment.end / total_duration) * 100)))
                if pct > last_pct:
                    last_pct = pct
                    with get_db() as conn:
                        conn.execute("UPDATE recordings SET transcription_progress = ? WHERE id = ?", (pct, recording_id))
                        conn.commit()
        
        with get_db() as conn:
            conn.execute(
                "UPDATE recordings SET transcription_status = 'completed', transcription_progress = 100, transcript_path = ? WHERE id = ?",
                (transcript_path, recording_id)
            )
            conn.commit()
            
        log_event(f"Transcription completed for recording #{recording_id} using {model_size}")
    except Exception as e:
        with get_db() as conn:
            conn.execute("UPDATE recordings SET transcription_status = 'failed', transcription_progress = 0 WHERE id = ?", (recording_id,))
            conn.commit()
        log_event(f"Transcription failed for recording #{recording_id}: {e}")

async def transcribe_audio(recording_id: int, filepath: str):
    with get_db() as conn:
        conn.execute("UPDATE recordings SET transcription_status = 'processing', transcription_progress = 0 WHERE id = ?", (recording_id,))
        conn.commit()
    
    transcript_path = filepath.rsplit(".", 1)[0] + ".txt"
    sem = get_transcription_semaphore()
    async with sem:
        await asyncio.to_thread(_run_transcription, recording_id, filepath, transcript_path)
    
    if get_setting("auto_highlight", "false") == "true":
        from app.highlighter import process_highlight_task
        asyncio.create_task(process_highlight_task(recording_id, filepath, transcript_path))