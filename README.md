# Stream Recorder

A lightweight, automated stream recording and scheduling platform packaged as a multi-architecture Docker container. Built with FastAPI, SQLite, APScheduler, and FFmpeg, Stream Recorder delivers real-time capture, live telemetry, browser-based management, and instant HTML Telegram notifications.

---

## Features

* **Automated Scheduling:** AsyncIO APScheduler job engine with strict UTC normalization to ensure accurate execution across time zones.
* **Manual & Scheduled Capture:** Records live internet radio streams, sports broadcasts, and direct audio feeds to high-quality MP3s via FFmpeg.
* **Live Telemetry Dashboard:** Computes active recording duration and disk file size growth on the fly every second via an Alpine.js and Tailwind CSS frontend.
* **Structured Filename Convention:** Standardizes recorded files according to the format:
  `{StreamName}_{Description}_{YYYY-MM-DD_HH-MM-SS}.mp3`
* **Instant Telegram Delivery Alerts:** Dispatches clean, high-contrast HTML notification cards with custom icons for recording start, recording stop, and stream connection changes.
* **Dependency-Free Notification Engine:** Utilizes Python standard library HTTP handlers for Telegram dispatch without third-party wrapper overhead.
* **Multi-Architecture Support:** Built for both `linux/amd64` and `linux/arm64` architectures.

---

## Repository Structure

```text
├── .github/
│   └── workflows/
│       └── docker-publish.yml # Multi-arch build, push, and Docker Hub sync
├── app/
│   ├── main.py                # FastAPI endpoints and Uvicorn log filters
│   ├── database.py            # SQLite schema management and event logging
│   ├── recorder.py            # FFmpeg process execution and metadata tagging
│   ├── scheduler.py           # APScheduler job definitions and UTC handlers
│   ├── notifier.py            # HTML Telegram alert delivery service
│   ├── sniffer.py             # Stream health verification and probing
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

## Telegram Notifications

Configure Telegram alerts directly from the **Settings** view in the web UI or inject credentials into `/config/stream_recorder.db`.

### Supported Settings

| Setting Key | Description | Default |
|---|---|---|
| `telegram_token` | Bot token provided by `@BotFather` | `""` |
| `telegram_chat_id` | Target Channel, Group, or Direct Chat ID | `""` |
| `notif_sched_start` | Alert when a scheduled recording begins | `true` |
| `notif_sched_stop` | Alert when a scheduled recording finishes | `true` |
| `notif_manual_start` | Alert when a manual recording begins | `true` |
| `notif_manual_stop` | Alert when a manual recording finishes | `true` |
| `notif_stream_connected` | Alert when a stream source connects | `true` |
| `notif_stream_disconnected` | Alert when a stream source drops | `true` |

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
| `GET` | `/api/recordings` | List completed and active recordings with live duration and byte counts |
| `POST` | `/api/recordings/start` | Start an immediate recording session |
| `POST` | `/api/recordings/stop` | Terminate an active recording process |
| `GET` | `/api/schedules` | List upcoming and completed schedule entries |
| `POST` | `/api/schedules` | Create a new scheduled capture window |
| `DELETE` | `/api/schedules/{id}` | Delete a scheduled recording job |
| `GET` | `/api/settings` | Retrieve system preferences and Telegram credentials |
| `POST` | `/api/settings` | Save application preferences and alert toggles |

---

## Persistent Storage & Database Schema

Application state is preserved inside `/config/stream_recorder.db` across container restarts:

* **`streams`**: Stream identifiers, feed URLs, and friendly names.
* **`schedules`**: Scheduled start/end windows stored in UTC, stream IDs, and descriptions.
* **`recordings`**: Filepaths, exit statuses, duration metrics, and disk sizes.
* **`settings`**: Key-value pairs for system options and notification tokens.
* **`logs`**: Centralized event and execution history.

---

## License

This project is licensed under the [MIT License](LICENSE).
