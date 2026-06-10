import json
import os
import queue
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from scanner import (
    Command,
    COMMAND_LOCK,
    COMMAND_SCAN,
    COMMAND_SET_CONFIG,
    COMMAND_SET_MODE,
    ScannerState,
    scanner_thread,
    load_channels,
)

app = FastAPI(title="WalkieTalkie FRS Scanner")

state = ScannerState()
cmd_queue = queue.Queue()

channels_path = Path(__file__).parent / "channels.json"
if channels_path.exists():
    state.channels = load_channels(str(channels_path))


@app.get("/api/status")
def get_status():
    return {
        "mode": state.mode,
        "channel": state.channel,
        "frequency": state.frequency,
        "signal_level": round(state.signal_level, 5),
        "signal_db": round(state.signal_db, 1),
        "recording": state.recording,
        "recording_duration": round(state.recording_duration, 1),
        "last_transcript": state.last_transcript,
        "last_transcript_time": state.last_transcript_time,
        "last_transcript_channel": state.last_transcript_channel,
        "squelch": state.squelch,
        "vox_threshold": state.vox_threshold,
        "gain": state.gain,
        "error": state.error,
        "uptime": round(state.uptime, 1),
    }


@app.get("/api/config")
def get_config():
    return {
        "squelch": state.squelch,
        "vox_threshold": state.vox_threshold,
        "gain": state.gain,
        "channels": len(state.channels),
        "whisper_model": os.environ.get("WHISPER_MODEL", "base.en"),
    }


@app.put("/api/config")
def update_config(body: dict):
    allowed = {"squelch", "vox_threshold", "gain"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if updates:
        for k, v in updates.items():
            setattr(state, k, v)
        cmd_queue.put(Command(COMMAND_SET_CONFIG, updates))
    return {k: getattr(state, k, None) for k in allowed}


@app.get("/api/channels")
def get_channels():
    result = []
    for i, ch in enumerate(state.channels):
        result.append(
            {
                "channel": ch["channel"],
                "frequency": ch["frequency"],
                "power": ch["power"],
                "band": ch.get("band", ""),
                "rssi": round(state.channels_rssi[i], 2),
                "db": round(state.channels_db[i], 1),
                "active": state.channel == ch["channel"],
            }
        )
    return {"channels": result}


@app.post("/api/scan")
def trigger_scan():
    cmd_queue.put(Command(COMMAND_SCAN))
    return {"status": "scan_started"}


@app.post("/api/lock/{channel_id:int}")
def lock_channel(channel_id: int):
    if channel_id < 1 or channel_id > len(state.channels):
        raise HTTPException(404, f"Channel {channel_id} not found")
    cmd_queue.put(Command(COMMAND_LOCK, {"channel": channel_id}))
    return {"status": "locked", "channel": channel_id}


@app.post("/api/mode")
def set_mode(body: dict):
    mode = body.get("mode", "auto")
    if mode not in ("auto", "monitor", "idle"):
        raise HTTPException(400, f"Invalid mode: {mode}")
    cmd_queue.put(Command(COMMAND_SET_MODE, {"mode": mode}))
    return {"status": f"mode_set_to_{mode}"}


@app.get("/api/transcripts")
def get_transcripts(limit: int = 20):
    transcripts_dir = Path("/transcripts")
    if not transcripts_dir.exists():
        return {"transcripts": []}
    files = sorted(transcripts_dir.glob("*.json"), reverse=True)[:limit]
    entries = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            entries.append(data)
        except Exception:
            pass
    return {"transcripts": entries}


@app.get("/api/recordings")
def get_recordings(limit: int = 20):
    recordings_dir = Path("/recordings")
    if not recordings_dir.exists():
        return {"recordings": []}
    files = sorted(recordings_dir.glob("*.wav"), reverse=True)[:limit]
    return {"recordings": [f.name for f in files]}


try:
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
except Exception:
    pass


if __name__ == "__main__":
    t = threading.Thread(
        target=scanner_thread, args=(state, cmd_queue), daemon=True
    )
    t.start()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
