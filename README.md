# Stream Recorder

A lightweight, automated stream recording and scheduling platform packaged as a Docker container. Built with FastAPI, SQLite, APScheduler, and FFmpeg, Stream Recorder provides real-time capture, dynamic telemetry, browser-based management, and instant Telegram delivery notifications.

---

## Key Features

- **Automated Scheduling:** Precision scheduling powered by AsyncIO APScheduler with strict UTC normalization to prevent missed jobs.
- **Manual & Scheduled Capture:** Record live internet radio streams, sports feeds, and HLS/audio links directly to high-quality MP3s via FFmpeg.
- **Live Telemetry Dashboard:** Real-time, per-second calculation of active recording duration and disk file size growth via an Alpine.js/Tailwind UI.
- **Standardized Metadata & Naming:** Clean, structured file output following the pattern:
  `{Stream_Label}_{Description}_{YYYY-MM-DD_HH-MM-SS}.mp3`
- **Instant Telegram Alerts:** High-contrast, scannable HTML notification cards dispatched automatically when recordings begin, conclude, or encounter stream connection state changes.
- **Zero-External-Dependency Notifications:** Built-in standard library HTTP client ensures reliable dispatch without third-party API wrapper fragility.
- **Lightweight & Self-Contained:** Runs in a single container with persistent volume mounts for recordings and database configuration.

---

## Architecture Overview



├── app/
│ ├── main.py # FastAPI application, route handlers, and API endpoints
│ ├── database.py # SQLite connection manager and persistent event logger
│ ├── recorder.py # FFmpeg process management and file metadata writer
│ ├── scheduler.py # APScheduler job engine and UTC lifecycle handlers
│ ├── notifier.py # HTML Telegram notification dispatch service
│ ├── sniffer.py # Stream availability and connection verification
│ ├── messages.json # Customizable Telegram message templates
│ └── static/
│ └── index.html # Single-page dashboard interface (Alpine.js + Tailwind)
├── Dockerfile # Python 3.12 + FFmpeg runtime environment
├── docker-compose.yml # Multi-volume container definition
└── requirements.txt # Python application dependencies



---

## Quick Start

### 1. Prerequisites
- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

### 2. Clone the Repository
```bash
git clone [https://github.com/provokingchaos/stream-recorder.git](https://github.com/provokingchaos/stream-recorder.git)
cd stream-recorder


3. Configure docker-compose.yml
Ensure your volume paths map cleanly to your host storage:



YAML
version: "3.8"

services:
  stream_recorder:
    container_name: stream_recorder
    build: .
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./config:/config
      - ./recordings:/recordings
    environment:
      - TZ=America/Chicago


4. Build and Launch



Bash
docker compose up -d --build


Access the web interface at http://<host-ip>:8000.
Configuration
Telegram Notifications
Configure notification settings directly within the Settings tab on the web dashboard or via SQLite:
Setting Key
Description
Default
telegram_token
Your Telegram Bot Token obtained from @BotFather
""
telegram_chat_id
Target Channel, Group, or User Chat ID
""
notif_sched_start
Notify when a scheduled recording starts
true
notif_sched_stop
Notify when a scheduled recording finishes
true
notif_manual_start
Notify when a manual recording starts
true
notif_manual_stop
Notify when a manual recording finishes
true

Customizing Alert Templates
Notification templates are defined in app/messages.json (and persisted under /config/messages.json). You can customize the HTML structure using the following tokens:
{stream_label}: Name/label of the configured stream.
{desc_text}: Contextual description or matchup title.
{start_str}: Formatted start time.
{end_str}: Formatted completion time.
{now_str}: Exact timestamp of event execution.
Default Template Format:



JSON
{
  "notif_sched_start": "🎙️ <b>RECORDING STARTED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Window:</b> <code>{start_str} - {end_str}</code>",
  "notif_sched_stop": "⏹ <b>RECORDING COMPLETED</b>\n<b>Stream:</b> <code>{stream_label}</code>{desc_text}\n<b>Finished:</b> <code>{now_str}</code>",
  "notif_stream_connected": "🟢 <b>STREAM CONNECTED</b>\n<b>Stream:</b> <code>{stream_label}</code>",
  "notif_stream_disconnected": "⚠️ <b>STREAM DISCONNECTED</b>\n<b>Stream:</b> <code>{stream_label}</code>"
}


API Reference
Method
Endpoint
Description
GET
/api/streams
List all configured audio streams
POST
/api/streams
Add a new audio stream source
GET
/api/recordings
List recording history and live telemetry for active jobs
POST
/api/recordings/start
Trigger an immediate manual recording
POST
/api/recordings/stop
Terminate an active recording process
GET
/api/schedules
Fetch all upcoming and past scheduled jobs
POST
/api/schedules
Create a new scheduled recording job
DELETE
/api/schedules/{id}
Cancel and delete a scheduled recording
GET
/api/settings
Retrieve system and notification settings
POST
/api/settings
Update configuration parameters

Database Management & Persistence
Application state is managed in SQLite at /config/stream_recorder.db. The core database schema includes:
streams: Registered audio endpoints, URLs, and labels.
schedules: Time-slotted jobs with ISO-8601 UTC timestamps and descriptions.
recordings: Complete file paths, execution status, byte sizes, and duration metadata.
settings: Runtime key-value storage for application preferences and credentials.
logs: Persistent operational and error logging table.
License
Distributed under the MIT License.
