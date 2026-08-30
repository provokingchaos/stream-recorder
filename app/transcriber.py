import asyncio
import os
from faster_whisper import WhisperModel
from app.database import get_db, log_event, get_setting

# Global variables for lazy loading and hot-swapping
_model = None
_loaded_model_size = None

def force_cache_model(model_size: str):
    """Forces the huggingface_hub to download and verify the model cache."""
    from faster_whisper.utils import download_model
    config_dir = os.getenv("CONFIG_DIR", "/config")
    model_dir = os.path.join(config_dir, "models")
    os.makedirs(model_dir, exist_ok=True)
    download_model(model_size, cache_dir=model_dir)

def _run_transcription(recording_id: int, filepath: str, transcript_path: str):
    global _model, _loaded_model_size
    try:
        model_size = get_setting("whisper_model", "base.en")
        
        # Load or reload the model if the size setting changed
        if _model is None or _loaded_model_size != model_size:
            config_dir = os.getenv("CONFIG_DIR", "/config")
            model_dir = os.path.join(config_dir, "models")
            os.makedirs(model_dir, exist_ok=True)
            _model = WhisperModel(model_size, device="cpu", compute_type="int8", download_root=model_dir)
            _loaded_model_size = model_size
        
        segments, info = _model.transcribe(filepath, beam_size=5)
        
        with open(transcript_path, "w", encoding="utf-8") as f:
            for segment in segments:
                f.write(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}\n")
        
        with get_db() as conn:
            conn.execute("UPDATE recordings SET transcription_status = 'completed', transcript_path = ? WHERE id = ?", (transcript_path, recording_id))
            conn.commit()
            
        log_event(f"Transcription completed for recording #{recording_id} using {model_size}")
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