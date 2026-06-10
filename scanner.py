import json
import math
import os
import queue
import select
import struct
import subprocess
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


SAMPLE_RATE = 22050
CHUNK_SIZE = 4096
VOX_HOLD_MS = 1500
VOX_DEBOUNCE_MS = 200
MIN_RECORD_SEC = 1.0
MAX_RECORD_SEC = 120.0
SCAN_INTERVAL_SEC = 30.0
SILENCE_TIMEOUT_SEC = 120.0


@dataclass
class ScannerState:
    mode: str = "auto"
    channel: int = 0
    frequency: float = 0.0
    signal_level: float = 0.0
    signal_db: float = -100.0
    recording: bool = False
    recording_duration: float = 0.0
    last_transcript: str = ""
    last_transcript_time: str = ""
    last_transcript_channel: int = 0
    channels_rssi: list = field(default_factory=lambda: [0.0] * 22)
    channels_db: list = field(default_factory=lambda: [-100.0] * 22)
    error: str = ""
    uptime: float = 0.0
    squelch: int = 30
    vox_threshold: float = 0.02
    gain: str = "auto"


COMMAND_SCAN = "SCAN"
COMMAND_LOCK = "LOCK"
COMMAND_SET_MODE = "SET_MODE"
COMMAND_SET_CONFIG = "SET_CONFIG"


@dataclass
class Command:
    type: str
    args: dict = field(default_factory=dict)


def load_channels(path: str) -> list:
    with open(path) as f:
        return json.load(f)


def compute_rms(data: bytes) -> float:
    if len(data) < 2:
        return 0.0
    count = len(data) // 2
    samples = struct.unpack(f"<{count}h", data[: count * 2])
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / count)
    return rms / 32768.0


def db_from_rms(rms: float) -> float:
    if rms < 1e-10:
        return -100.0
    return 20.0 * math.log10(rms)


def kill_proc(proc: subprocess.Popen | None):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def freq_str(freq_mhz: float) -> str:
    return f"{freq_mhz:.6f}M"


def scanner_thread(state: ScannerState, cmd_queue: queue.Queue):
    channels = state.channels
    recordings_dir = Path("/recordings")
    transcripts_dir = Path("/transcripts")
    recordings_dir.mkdir(parents=True, exist_ok=True)
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    model_path = os.environ.get(
        "WHISPER_MODEL_PATH", "/models/ggml-base.en.bin"
    )

    fm_proc = None
    wav_file = None
    recording_path = None
    recording_start = 0.0
    vox_on_time = 0.0
    vox_off_time = 0.0
    last_scan_time = 0.0
    start_time = time.time()
    silence_start = 0.0

    _state = "idle"
    scan_results = []

    def _cleanup_fm():
        nonlocal fm_proc
        if fm_proc:
            kill_proc(fm_proc)
            fm_proc = None

    def _cleanup_recording():
        nonlocal wav_file, recording_path
        if wav_file:
            try:
                wav_file.close()
            except Exception:
                pass
            wav_file = None
        recording_path = None

    def _start_fm(freq_mhz: float):
        _cleanup_fm()
        try:
            proc = subprocess.Popen(
                [
                    "rtl_fm",
                    "-f", freq_str(freq_mhz),
                    "-M", "fm",
                    "-s", str(SAMPLE_RATE),
                    "-W", "narrow",
                    "-E", "deemp",
                    "-l", str(state.squelch),
                    "-A", "std",
                    "-F", "9",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            return proc
        except FileNotFoundError:
            state.error = "rtl_fm not found"
            return None
        except Exception as e:
            state.error = f"rtl_fm error: {e}"
            return None

    def _scan_channels() -> list:
        nonlocal scan_results
        results = []
        min_freq = min(c["frequency"] for c in channels) - 0.001
        max_freq = max(c["frequency"] for c in channels) + 0.001
        try:
            proc = subprocess.Popen(
                [
                    "rtl_power",
                    "-f", f"{freq_str(min_freq)}-{freq_str(max_freq)}:25k",
                    "-i", "1",
                    "-1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            stdout, _ = proc.communicate(timeout=15)
            for line in stdout.strip().split("\n"):
                parts = line.split(",")
                if len(parts) >= 4:
                    try:
                        freq = float(parts[2])
                        power = float(parts[3])
                        results.append((freq, power))
                    except ValueError:
                        pass
        except FileNotFoundError:
            state.error = "rtl_power not found"
        except subprocess.TimeoutExpired:
            kill_proc(proc)
        except Exception as e:
            state.error = f"scan error: {e}"

        for i, ch in enumerate(channels):
            freq = ch["frequency"]
            nearby = [p for f, p in results if abs(f - freq) < 0.0125]
            if nearby:
                avg_power = sum(nearby) / len(nearby)
                state.channels_rssi[i] = avg_power
                state.channels_db[i] = 10.0 * math.log10(avg_power + 1e-12)
            else:
                state.channels_rssi[i] = 0.0
                state.channels_db[i] = -100.0

        return results

    def _pick_best_channel() -> int:
        best_idx = 0
        best_power = -1e9
        for i, ch in enumerate(channels):
            db = state.channels_db[i]
            if db > best_power:
                best_power = db
                best_idx = i
        if state.channels_db[best_idx] > -70:
            return best_idx
        return -1

    def _transcribe(audio_path: str, ch: int):
        try:
            proc = subprocess.Popen(
                [
                    "whisper-cli",
                    "-m", model_path,
                    "-f", audio_path,
                    "-ot",
                    "-nt",
                    "-np",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            stdout, _ = proc.communicate(timeout=300)
            text = stdout.strip()
            if not text:
                text = "(silence)"
        except FileNotFoundError:
            text = "(whisper not available)"
        except subprocess.TimeoutExpired:
            text = "(transcription timed out)"
            kill_proc(proc)
        except Exception as e:
            text = f"(error: {e})"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.last_transcript = text
        state.last_transcript_time = timestamp
        state.last_transcript_channel = ch

        ch_info = channels[ch - 1] if 1 <= ch <= len(channels) else {}
        entry = {
            "time": timestamp,
            "channel": ch,
            "frequency": ch_info.get("frequency"),
            "text": text,
            "file": audio_path,
        }
        tname = datetime.now().strftime("%Y%m%d_%H%M%S")
        tpath = transcripts_dir / f"{tname}_ch{ch:02d}.json"
        try:
            with open(tpath, "w") as f:
                json.dump(entry, f, indent=2)
        except Exception:
            pass

    while True:
        while not cmd_queue.empty():
            cmd = cmd_queue.get()
            if cmd.type == COMMAND_SET_MODE:
                state.mode = cmd.args.get("mode", "auto")
                _cleanup_fm()
                _cleanup_recording()
                _state = "idle"
                state.error = ""
            elif cmd.type == COMMAND_LOCK:
                ch = cmd.args.get("channel", 1)
                if 1 <= ch <= len(channels):
                    state.channel = ch
                    state.frequency = channels[ch - 1]["frequency"]
                    state.mode = "monitor"
                    _cleanup_fm()
                    _cleanup_recording()
                    _state = "idle"
                    state.error = ""
            elif cmd.type == COMMAND_SCAN:
                _state = "scanning"
            elif cmd.type == COMMAND_SET_CONFIG:
                for k, v in cmd.args.items():
                    if hasattr(state, k):
                        setattr(state, k, v)
                if fm_proc:
                    if "squelch" in cmd.args:
                        _cleanup_fm()
                        _state = "idle"

        state.uptime = time.time() - start_time

        if _state == "idle":
            if state.mode == "auto":
                _state = "scanning"
            elif state.mode == "monitor":
                _state = "monitoring"
            else:
                time.sleep(0.1)

        elif _state == "scanning":
            state.error = ""
            scan_results = _scan_channels()
            if state.mode == "auto":
                best = _pick_best_channel()
                if best >= 0:
                    state.channel = best + 1
                    state.frequency = channels[best]["frequency"]
                    _state = "monitoring"
                else:
                    _state = "idle"
                    time.sleep(2)
            else:
                _state = "idle"

        elif _state == "monitoring":
            if fm_proc is None:
                if state.frequency == 0 and state.channel > 0:
                    state.frequency = channels[state.channel - 1]["frequency"]
                if state.frequency == 0:
                    state.frequency = channels[0]["frequency"]
                    state.channel = 1
                fm_proc = _start_fm(state.frequency)
                if fm_proc is None:
                    time.sleep(2)
                    continue
                vox_on_time = 0.0
                vox_off_time = 0.0
                silence_start = time.time()

            r, _, _ = select.select([fm_proc.stdout], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(fm_proc.stdout.fileno(), CHUNK_SIZE)
                except Exception:
                    chunk = b""
                if not chunk:
                    _cleanup_fm()
                    continue

                rms = compute_rms(chunk)
                state.signal_level = rms
                state.signal_db = db_from_rms(rms)

                now = time.time()
                if rms > state.vox_threshold:
                    if vox_on_time == 0:
                        vox_on_time = now
                    vox_off_time = 0
                    silence_start = now

                    if (
                        vox_on_time > 0
                        and (now - vox_on_time) * 1000 > VOX_DEBOUNCE_MS
                    ):
                        _state = "recording"
                        vox_on_time = 0
                else:
                    vox_on_time = 0

                if state.mode == "auto":
                    elapsed = now - silence_start
                    if elapsed > SILENCE_TIMEOUT_SEC and elapsed > 30:
                        _cleanup_fm()
                        _state = "scanning"
            else:
                pass

        elif _state == "recording":
            if fm_proc is None:
                _state = "monitoring"
                continue

            if wav_file is None:
                recording_start = time.time()
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                ch = state.channel
                fname = f"{timestamp}_ch{ch:02d}.wav"
                recording_path = str(recordings_dir / fname)
                try:
                    wav_file = wave.open(recording_path, "wb")
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(SAMPLE_RATE)
                except Exception as e:
                    state.error = f"wav open error: {e}"
                    _cleanup_recording()
                    _state = "monitoring"
                    continue
                state.recording = True

            r, _, _ = select.select([fm_proc.stdout], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(fm_proc.stdout.fileno(), CHUNK_SIZE)
                except Exception:
                    chunk = b""
                if not chunk:
                    _cleanup_fm()
                    _cleanup_recording()
                    state.recording = False
                    _state = "monitoring"
                    continue

                rms = compute_rms(chunk)
                state.signal_level = rms
                state.signal_db = db_from_rms(rms)

                try:
                    wav_file.writeframes(chunk)
                except Exception:
                    pass

                now = time.time()
                state.recording_duration = now - recording_start

                if rms > state.vox_threshold:
                    vox_off_time = 0
                else:
                    if vox_off_time == 0:
                        vox_off_time = now
                    elif (now - vox_off_time) * 1000 > VOX_HOLD_MS:
                        _cleanup_recording()
                        state.recording = False
                        state.recording_duration = 0

                        if recording_path and os.path.getsize(recording_path) > 4000:
                            _state = "transcribing"
                        else:
                            try:
                                os.unlink(recording_path)
                            except Exception:
                                pass
                            recording_path = None
                            if state.mode == "auto":
                                _state = "scanning"
                            else:
                                _state = "monitoring"

                if state.recording_duration > MAX_RECORD_SEC:
                    _cleanup_recording()
                    state.recording = False
                    state.recording_duration = 0
                    if recording_path and os.path.getsize(recording_path) > 4000:
                        _state = "transcribing"
                    else:
                        recording_path = None
                        _state = "monitoring"

        elif _state == "transcribing":
            path = recording_path
            ch = state.channel
            recording_path = None
            if path and os.path.exists(path):
                _transcribe(path, ch)
            _cleanup_fm()
            if state.mode == "auto":
                _state = "scanning"
            else:
                _state = "monitoring"
