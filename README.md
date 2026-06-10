# WalkieTalkie — FRS Scanner, Recorder & Transcriber

Docker container that scans FRS/GMRS walkie-talkie bands, locks onto active channels, records audio, and transcribes speech to text — all controllable via a web UI.

```
┌─────────────────────────────────────────────────────────────┐
│  ┌──────────┐   ┌──────────┐   ┌─────────┐   ┌──────────┐ │
│  │ rtl_power│──▶│ rtl_fm   │──▶│ VOX     │──▶│ whisper  │ │
│  │ scan 22  │   │ demod    │   │ detect  │   │ .cpp     │ │
│  │ channels │   │ 22kHz FM │   │ + WAV   │   │ transcribe│ │
│  └──────────┘   └──────────┘   └────┬────┘   └──────────┘ │
│                                      │                      │
│                               ┌──────┴──────┐               │
│                               │ FastAPI + Web│              │
│                               │ UI (port 8080)│             │
│                               └─────────────┘               │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
docker compose up -d
# Open http://localhost:8080
```

## Features

- **22 FRS channels** — scans 462–467 MHz, narrowband FM
- **Auto mode** — sweeps channels, locks onto the most active one
- **VOX recording** — starts/stops WAV recordings based on audio energy
- **Speech-to-text** — transcribes recordings using `whisper.cpp` (base.en model, ~145 MB)
- **Live web UI** — channel selector, signal meter, recent transcripts, config controls
- **REST API** — control everything programmatically

## Architecture

### Components

| Component | Role |
|-----------|------|
| `rtl_power` | Rapid spectrum sweep across FRS frequencies, reports power levels per channel |
| `rtl_fm` | Narrowband FM demodulation (12.5 kHz bandwidth, de-emphasis) |
| `VOX detector` | RMS energy threshold detection — starts recording on speech, stops after 1.5s silence |
| `whisper.cpp` | Local speech-to-text inference using OpenAI's Whisper model (base.en) |
| `FastAPI` | REST API + static file serving |
| `index.html` | Single-file web UI (zero dependencies) |

### Scanner States

```
idle → scanning → monitoring → recording → transcribing → (loop)
```

- **scanning**: runs `rtl_power` sweep, picks channel with strongest signal above -70 dB
- **monitoring**: runs `rtl_fm` on selected channel, computes RMS energy per chunk
- **recording**: writes raw audio to `/recordings/*.wav`, tracks voice activity
- **transcribing**: calls `whisper-cli`, saves result to `/transcripts/*.json`

## API

All endpoints at `http://localhost:8080/api/`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Current mode, channel, signal level, recording state, last transcript |
| `GET` | `/api/config` | Squelch level, VOX threshold, gain |
| `PUT` | `/api/config` | Update config (squelch, vox_threshold, gain) |
| `GET` | `/api/channels` | RSSI/ dB levels for all 22 channels |
| `POST` | `/api/scan` | Trigger an immediate channel scan |
| `POST` | `/api/lock/{ch}` | Lock to a specific channel (1–22) |
| `POST` | `/api/mode` | Set mode: `auto`, `monitor`, `idle` |
| `GET` | `/api/transcripts` | Recent transcriptions (last 20) |

## FRS Channels

The Family Radio Service uses 22 channels in the 462–467 MHz UHF band:

| Range | Freqs | Power | Notes |
|-------|-------|-------|-------|
| CH 1–7 | 462.5625 – 462.7125 MHz | 2 W | Shared with GMRS |
| CH 8–14 | 467.5625 – 467.7125 MHz | 0.5 W | FRS-only, lower power |
| CH 15–22 | 462.5500 – 462.7250 MHz | 2 W | Shared with GMRS |

All channels use narrowband FM (12.5 kHz bandwidth).

**Privacy codes (CTCSS/DCS):** These are sub-audible tones that filter at the receiver — they do not encrypt or hide audio. Our scanner hears all traffic on a frequency regardless of tone. CTCSS detection can be added as a future enhancement.

## USB Passthrough (Windows)

Docker Desktop on Windows (WSL2 backend) needs the RTL-SDR USB device forwarded into the Linux VM before Docker can see it.

### Option A: usbipd-win (recommended)

```powershell
# On Windows (Admin):
winget install usbipd
usbipd list
usbipd bind --busid <BUSID> --force

# Inside your WSL2 distro:
sudo usbip attach -r <WindowsIP> -b <BUSID>
```

Then Docker uses the device via `--device /dev/bus/usb:/dev/bus/usb` (already in `docker-compose.yml`).

### Option B: rtl_tcp bridge

Run `rtl_tcp` on Windows, then replace the `devices` entry in `docker-compose.yml` with an environment variable and modify the scanner to connect via TCP.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `base.en` | Whisper model name |
| `WHISPER_MODEL_PATH` | `/models/ggml-base.en.bin` | Path to model file |

### Run-time Config (via API / Web UI)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `squelch` | 30 | 0–150 | RF squelch level (passed to `rtl_fm -l`) |
| `vox_threshold` | 0.02 | 0.001–0.5 | RMS energy threshold for voice detection |
| `gain` | auto | auto / numeric | RTL-SDR gain |

## Output

```
recordings/
├── 20260609_143022_ch07.wav      # Raw audio
└── ...

transcripts/
├── 20260609_143022_ch07.json     # Transcription result
└── ...
```

Transcript JSON format:
```json
{
  "time": "2026-06-09 14:30:22",
  "channel": 7,
  "frequency": 462.7125,
  "text": "copy that, heading north now",
  "file": "/recordings/20260609_143022_ch07.wav"
}
```

## Building

```bash
docker compose build
```

The build is multi-stage:
1. **Builder stage** — clones whisper.cpp v1.8.4, compiles it, downloads `base.en` model
2. **Runtime stage** — installs rtl-sdr, Python, FastAPI; copies whisper binary and model

Total build time: ~2–4 minutes (mostly whisper.cpp compilation + model download).

## Development

```bash
# Run without USB device (API works, scanner thread runs but fails gracefully)
docker compose up -d

# Check logs
docker compose logs -f

# Stop
docker compose down
```

To test the API without hardware, any `POST` action (scan, lock, mode) queues the command; the scanner thread will try to run `rtl_power`/`rtl_fm`, log the error, and stay in idle mode.

## Roadmap

- CTCSS tone detection for multi-user differentiation
- rtl_tcp support for network-attached SDR
- Configurable scan intervals and VOX hold times
- WebSocket streaming of live signal levels
- PTT (Push-to-Talk) detection analytics

## License

MIT
