"""Local speech-to-text for Telegram voice notes.

Local rather than API-based because voice notes are the most personal thing
that passes through this system.

The model is loaded lazily and released after an idle period: the bot daemon is
long-lived, and holding ~1GB resident for something used a few times a day is
rude on a small box that runs other services too.
"""
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config, vault

MODEL_SIZE = os.environ.get("EA_WHISPER_MODEL", "small")
COMPUTE = os.environ.get("EA_WHISPER_COMPUTE", "int8")
IDLE_UNLOAD_SEC = int(os.environ.get("EA_WHISPER_IDLE", "900"))
LANGUAGE = os.environ.get("EA_WHISPER_LANG", "en")

_lock = threading.Lock()
_model = None
_last_used = 0.0


def available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load():
    global _model, _last_used
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type=COMPUTE)
    _last_used = time.time()
    return _model


def maybe_unload():
    """Release the model after a quiet spell. Safe to call on every poll cycle."""
    global _model
    with _lock:
        if _model is not None and time.time() - _last_used > IDLE_UNLOAD_SEC:
            _model = None
            return True
    return False


def _to_wav(data, suffix=".oga"):
    """Decode Telegram's OGG/Opus into 16kHz mono WAV via ffmpeg."""
    src = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    src.write(data)
    src.close()
    dst = tempfile.mktemp(suffix=".wav")
    res = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src.name,
         "-ac", "1", "-ar", "16000", "-f", "wav", dst],
        capture_output=True, text=True, timeout=180,
    )
    os.unlink(src.name)
    if res.returncode != 0 or not os.path.exists(dst):
        return None, f"ffmpeg failed: {res.stderr.strip()[:160]}"
    return dst, None


def transcribe(data, suffix=".oga"):
    """Transcribe audio bytes. Returns (text, error)."""
    if not available():
        return None, "faster-whisper not installed"
    wav, err = _to_wav(data, suffix)
    if err:
        return None, err
    try:
        with _lock:
            model = _load()
        segments, _info = model.transcribe(
            wav, language=LANGUAGE, beam_size=1, vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        global _last_used
        _last_used = time.time()
    except Exception as exc:                      # model or audio failure
        return None, f"transcribe failed: {type(exc).__name__}: {exc}"
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    if not text:
        return None, "nothing intelligible in the audio"
    return text, None


def park(data, suffix=".oga"):
    """Save un-transcribable audio into the vault so the thought is not lost."""
    config.ATTACHMENTS.mkdir(parents=True, exist_ok=True)
    name = f"voice-{vault.now().strftime('%Y%m%d-%H%M%S')}{suffix}"
    path = config.ATTACHMENTS / name
    path.write_bytes(data)
    return path


def warm():
    """Pre-download and load the model, so the first real note is not slow."""
    with _lock:
        _load()
    return MODEL_SIZE
