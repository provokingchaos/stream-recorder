import os
import re
import json
import asyncio
import subprocess
from huggingface_hub import hf_hub_download
from app.database import get_db, log_event, get_setting

_highlight_sem = None
_current_highlight_limit = 1

def get_highlight_semaphore():
    global _highlight_sem, _current_highlight_limit
    try:
        limit = int(get_setting("max_concurrent_highlights", "1"))
    except ValueError:
        limit = 1
    limit = max(1, limit)
    if _highlight_sem is None or limit != _current_highlight_limit:
        _current_highlight_limit = limit
        _highlight_sem = asyncio.Semaphore(_current_highlight_limit)
    return _highlight_sem

def get_prompt_filepath():
    config_dir = os.getenv("CONFIG_DIR", "/config")
    filename = get_setting("highlight_prompt_file", "highlight_prompt.txt").strip()
    if not filename:
        filename = "highlight_prompt.txt"
    filename = os.path.basename(filename)
    return os.path.join(config_dir, filename)

def get_highlight_prompt():
    path = get_prompt_filepath()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    
    default_prompt = "You are listening to a high school football game broadcast. Please make a .mp3 highlight reel of all highlights of the game including when a team scores, when a team turns over the ball, when a team gets an injury. You do not need to remove any advertisements or promotional announcements if that is part of the highlight reel."
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(default_prompt)
    except Exception:
        pass
    return default_prompt

def parse_transcript(filepath):
    lines = []
    pattern = re.compile(r"\[([\d\.]+)s -> ([\d\.]+)s\] (.*)")
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            match = pattern.search(line)
            if match:
                lines.append({
                    "start": float(match.group(1)),
                    "end": float(match.group(2)),
                    "text": match.group(3).strip()
                })
    return lines

def _run_highlight(recording_id, audio_path, transcript_path, output_path):
    from llama_cpp import Llama
    try:
        user_prompt = get_highlight_prompt()
        lines = parse_transcript(transcript_path)
        if not lines:
            raise ValueError("Transcript is empty or unparsable.")

        # Provide immediate UI and Log feedback before the massive download blocks execution
        log_event(f"Highlight engine starting for #{recording_id}. Verifying/Downloading Qwen AI model (1.1GB)...")
        with get_db() as conn:
            conn.execute("UPDATE recordings SET highlight_progress = 5 WHERE id = ?", (recording_id,))
            conn.commit()

        config_dir = os.getenv("CONFIG_DIR", "/config")
        model_path = hf_hub_download(
            repo_id="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
            filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
            cache_dir=os.path.join(config_dir, "models")
        )
        
        log_event(f"Qwen AI model loaded for #{recording_id}. Beginning transcript analysis...")
        llm = Llama(model_path=model_path, n_ctx=4096, verbose=False)

        chunks = []
        current_chunk = []
        chunk_start = lines[0]["start"]

        for line in lines:
            current_chunk.append(line)
            if line["end"] - chunk_start >= 180:
                chunks.append(current_chunk)
                current_chunk = []
                chunk_start = line["end"]
        if current_chunk:
            chunks.append(current_chunk)

        all_highlights = []
        total_chunks = len(chunks)

        for i, chunk in enumerate(chunks):
            pct = 10 + int((i / total_chunks) * 80)
            with get_db() as conn:
                conn.execute("UPDATE recordings SET highlight_progress = ? WHERE id = ?", (pct, recording_id))
                conn.commit()

            chunk_text = "\n".join([f"[{l['start']:.2f}s -> {l['end']:.2f}s] {l['text']}" for l in chunk])

            system_msg = "You are a JSON audio highlight extraction AI. Extract timestamp segments that match the user's criteria. Output ONLY a valid JSON array of objects with 'start' and 'end' float keys. Example: [{\"start\": 12.0, \"end\": 45.5}]. If no matches, output []."
            user_msg = f"Criteria: {user_prompt}\n\nTranscript:\n{chunk_text}"

            response = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg}
                ],
                temperature=0.1
            )
            
            try:
                result = response["choices"][0]["message"]["content"]
                result = re.sub(r'```json\s*', '', result)
                result = re.sub(r'```\s*', '', result).strip()
                
                parsed = json.loads(result)
                if isinstance(parsed, list):
                    all_highlights.extend(parsed)
            except Exception as e:
                log_event(f"Highlight JSON parse error on chunk {i}: {e}")

        with get_db() as conn:
            conn.execute("UPDATE recordings SET highlight_progress = 90 WHERE id = ?", (recording_id,))
            conn.commit()

        if not all_highlights:
            with get_db() as conn:
                conn.execute("UPDATE recordings SET highlight_status = 'completed', highlight_progress = 100 WHERE id = ?", (recording_id,))
                conn.commit()
            log_event(f"Highlight generation completed for #{recording_id}: No highlights matched criteria.")
            return

        padded = [{"start": max(0, h["start"] - 5), "end": h["end"] + 5} for h in all_highlights]
        padded.sort(key=lambda x: x["start"])
        merged = []
        for h in padded:
            if not merged:
                merged.append(h)
            else:
                if h["start"] <= merged[-1]["end"]:
                    merged[-1]["end"] = max(merged[-1]["end"], h["end"])
                else:
                    merged.append(h)

        rec_dir = os.path.dirname(output_path)
        temp_files = []
        for i, h in enumerate(merged):
            temp_file = os.path.join(rec_dir, f"temp_hl_{recording_id}_{i}.mp3")
            subprocess.run([
                "ffmpeg", "-y", "-i", audio_path,
                "-ss", str(h["start"]), "-to", str(h["end"]),
                "-c", "copy", temp_file
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(temp_file):
                temp_files.append(temp_file)

        if temp_files:
            list_file = os.path.join(rec_dir, f"list_{recording_id}.txt")
            with open(list_file, "w") as f:
                for tf in temp_files:
                    f.write(f"file '{os.path.basename(tf)}'\n")

            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", list_file, "-c", "copy", output_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            try:
                os.remove(list_file)
            except OSError:
                pass
            for tf in temp_files:
                try:
                    os.remove(tf)
                except OSError:
                    pass

            with get_db() as conn:
                conn.execute("UPDATE recordings SET highlight_status = 'completed', highlight_progress = 100, highlight_path = ? WHERE id = ?", (output_path, recording_id))
                conn.commit()
            log_event(f"Highlight reel generated for #{recording_id}")

    except Exception as e:
        with get_db() as conn:
            conn.execute("UPDATE recordings SET highlight_status = 'failed', highlight_progress = 0 WHERE id = ?", (recording_id,))
            conn.commit()
        log_event(f"Highlight generation failed for #{recording_id}: {e}")

async def process_highlight_task(recording_id: int, audio_path: str, transcript_path: str):
    with get_db() as conn:
        conn.execute("UPDATE recordings SET highlight_status = 'processing', highlight_progress = 0 WHERE id = ?", (recording_id,))
        conn.commit()

    output_path = audio_path.rsplit(".", 1)[0] + "_highlight.mp3"
    sem = get_highlight_semaphore()
    async with sem:
        await asyncio.to_thread(_run_highlight, recording_id, audio_path, transcript_path, output_path)