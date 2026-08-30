<p align="center">
  <img src="https://raw.githubusercontent.com/provokingchaos/stream-recorder/main/assets/stream-recorder-logo.png" alt="Stream Recorder Logo" width="128" height="128" />
</p>

# Stream Recorder

A lightweight, automated stream recording and scheduling platform packaged as a multi-architecture Docker container. Built with FastAPI, SQLite, APScheduler, FFmpeg, and Faster-Whisper, Stream Recorder delivers real-time capture, live telemetry, browser-based management, AI audio transcription, and instant HTML Telegram notifications.

---

## Features

* **AI Audio Transcription:** CPU-optimized, multi-threaded INT8 transcription via `faster-whisper`. Generate transcripts automatically upon stream completion or trigger them manually from the dashboard.
* **Advanced Stream Discovery:** Built-in Playwright and yt-dlp sniffer extracts hidden audio URLs (including direct resolution of `.pls` and `.m3u` playlists) directly from station websites.
* **Automated Scheduling:** AsyncIO APScheduler job engine with strict UTC normalization to ensure accurate execution across time zones.
* **Accurate Media Telemetry:** Utilizes `ffprobe` to extract true media duration—compensating for HLS rolling buffers—while calculating disk file size growth on the fly.
* **Responsive Web Dashboard:** A mobile-friendly Alpine.js and Tailwind CSS frontend featuring system theme auto-detection, in-browser playback, and fluid data cards.
* **Structured Filename Convention:** Standardizes recorded files according to the format: `{StreamName}_{Description}_{YYYY-MM-DD_HH-MM-SS}.mp3`.
* **Asynchronous Telegram Alerts:** Dispatches clean, high-contrast HTML notification cards for recording events and stream health changes without blocking the application event loop.
* **Self-Healing Library:** Automatically prunes database records if physical audio or transcript files are manually removed from the storage drive.
* **Multi-Architecture Support:** Built for both `linux/amd64` and `linux/arm64` architectures.

---

## Repository Structure

```text
├── .github/
│   └── workflows/
│       └── docker-publish.yml # Multi-arch build, push, and cache layer sync
├── app/
│   ├── main.py                # FastAPI endpoints and API routing
│   ├── database.py            # SQLite schema management and initialization
│   ├── recorder.py            # FFmpeg process execution and ffprobe telemetry
│   ├── scheduler.py           # APScheduler job definitions and UTC handlers
│   ├── transcriber.py         # Faster-Whisper background AI transcription worker
│   ├── notifier.py            # Asynchronous HTML Telegram delivery service
│   ├── sniffer.py             # Playwright and yt-dlp URL extraction engine
│   ├── messages.json          # Default Telegram message templates
│   └── static/
│       └── index.html         # Single-page web dashboard
├── Dockerfile                 # Multi-stage Python 3.12 + FFmpeg runtime
├── docker-compose.yml         # Container and volume orchestration
└── requirements.txt           # Python application dependencies
```

---

## Quick Start

### 1. Using Docker Compose (Recommended)

Create a `docker-compose.yml` file:

```yaml
services:
  stream_recorder:
    image: provokingchaos/stream-recorder:latest
    container_name: stream_recorder
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./config:/config
      - ./recordings:/recordings
    environment:
      - TZ=America/Chicago
```

Start the container:

```bash
docker compose up -d
```

Access the dashboard at `http://<host-ip>:8000`.

### 2. Using Docker CLI

```bash
docker run -d \
  --name stream_recorder \
  --restart unless-stopped \
  -p 8000:8000 \
  -v $(pwd)/config:/config \
  -v $(pwd)/recordings:/recordings \
  -e TZ=America/Chicago \
  provokingchaos/stream-recorder:latest
```

---

## Telegram Notifications & Settings

Configure application behavior and Telegram alerts directly from the **Settings** view in the web UI. 

### Supported Settings

| Setting Key | Description | Default |
|---|---|---|
| `recordings_dir` | Internal container path where media and transcripts are saved | `/recordings` |
| `auto_transcribe` | Generate a text transcript immediately after a recording finishes | `false` |
| `telegram_token` | Bot token provided by `@BotFather` | `""` |
| `telegram_chat_id` | Target Channel, Group, or Direct Chat ID | `""` |
| `notif_sched_start` | Alert when a scheduled recording begins | `false` |
| `notif_sched_stop` | Alert when a scheduled recording finishes | `false` |
| `notif_manual_start` | Alert when a manual recording begins | `false` |
| `notif_manual_stop` | Alert when a manual recording finishes | `false` |
| `notif_stream_connected` | Alert when a stream source connects | `false` |
| `notif_stream_disconnected` | Alert when a stream source drops | `false` |

### Alert Formatting & Template Tokens

Message templates reside in `app/messages.json` and are persisted to `/config/messages.json`. The following tokens are dynamically interpolated:

* `{stream_label}`: Configured name of the stream source.
* `{desc_text}`: Event description or matchup title.
* `{start_str}`: Formatted start time.
* `{end_str}`: Formatted finish time.
* `{now_str}`: Timestamp of event execution.

```json
{
  "notif_sched_start": "🎙️ <b>RECORDING STARTED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Window:</b> <code>{start_str} - {end_str}</code>",
  "notif_sched_stop": "⏹ <b>RECORDING COMPLETED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Finished:</b> <code>{now_str}</code>",
  "notif_manual_start": "🎙️ <b>MANUAL RECORDING STARTED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Started:</b> <code>{start_str}</code>",
  "notif_manual_stop": "⏹ <b>MANUAL RECORDING COMPLETED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Finished:</b> <code>{now_str}</code>",
  "notif_stream_connected": "🟢 <b>STREAM CONNECTED</b>\n<b>Stream:</b> <code>{stream_label}</code>",
  "notif_stream_disconnected": "⚠️ <b>STREAM DISCONNECTED</b>\n<b>Stream:</b> <code>{stream_label}</code>"
}
```

---

## API Reference

| HTTP Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/streams` | List registered stream sources |
| `POST` | `/api/streams` | Add a new stream URL and label |
| `POST` | `/api/streams/probe` | Extract audio stream URLs from a webpage |
| `GET` | `/api/recordings` | List completed and active recordings |
| `POST` | `/api/record/start` | Start an immediate recording session |
| `POST` | `/api/record/stop/{id}` | Terminate an active recording process |
| `POST` | `/api/recordings/{id}/transcribe` | Trigger a manual AI transcription for a completed recording |
| `GET` | `/api/recordings/{id}/play` | Stream a completed recording directly in the browser |
| `GET` | `/api/recordings/{id}/download` | Download a completed MP3 file |
| `GET` | `/api/recordings/{id}/transcript` | Download the generated transcription text file |
| `GET` | `/api/schedules` | List upcoming and completed schedule entries |
| `POST` | `/api/schedules` | Create a new scheduled capture window |
| `PATCH` | `/api/schedules/{id}` | Modify an existing schedule entry |
| `DELETE` | `/api/schedules/{id}` | Delete a scheduled recording job |
| `GET` | `/api/sys_settings` | Retrieve system preferences and Telegram credentials |
| `POST` | `/api/sys_settings` | Save application preferences and alert toggles |
| `POST` | `/api/purge` | Execute a complete database factory reset |

---

## Persistent Storage & Database Schema

Application state is preserved inside `/config/stream_recorder.db` across container restarts:

* **`streams`**: Stream identifiers, feed URLs, and friendly names.
* **`schedules`**: Scheduled start/end windows stored in UTC, stream IDs, and descriptions.
* **`recordings`**: Filepaths, exit statuses, true media duration, disk sizes, and transcription states.
* **`settings`**: Key-value pairs for system options, automation toggles, and notification tokens.
* **`logs`**: Centralized event and execution history.

---

## License

This project is licensed under the [MIT License](LICENSE).