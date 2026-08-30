import asyncio
import os
from faster_whisper import WhisperModel
from app.database import get_db, log_event

# Global model instance for lazy loading
_model = None

def _run_transcription(recording_id: int, filepath: str, transcript_path: str):
    global _model
    try:
        # Lazy load the model into memory only when needed using INT8 for CPU speed
        if _model is None:
            config_dir = os.getenv("CONFIG_DIR", "/config")
            model_dir = os.path.join(config_dir, "models")
            os.makedirs(model_dir, exist_ok=True)
            _model = WhisperModel("base.en", device="cpu", compute_type="int8", download_root=model_dir)
        
        # Beam size 5 offers the best balance of speed vs accuracy
        segments, info = _model.transcribe(filepath, beam_size=5)
        
        with open(transcript_path, "w", encoding="utf-8") as f:
            for segment in segments:
                f.write(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}\n")
        
        with get_db() as conn:
            conn.execute("UPDATE recordings SET transcription_status = 'completed', transcript_path = ? WHERE id = ?", (transcript_path, recording_id))
            conn.commit()
            
        log_event(f"Transcription completed for recording #{recording_id}")
    except Exception as e:
        with get_db() as conn:
            conn.execute("UPDATE recordings SET transcription_status = 'failed' WHERE id = ?", (recording_id,))
            conn.commit()
        log_event(f"Transcription failed for recording #{recording_id}: {e}")

async def transcribe_audio(recording_id: int, filepath: str):
    """Dispatches the CPU-heavy transcription to a background thread."""
    with get_db() as conn:
        conn.execute("UPDATE recordings SET transcription_status = 'processing' WHERE id = ?", (recording_id,))
        conn.commit()
    
    transcript_path = filepath.rsplit(".", 1)[0] + ".txt"
    await asyncio.to_thread(_run_transcription, recording_id, filepath, transcript_path)