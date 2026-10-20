"""Text-to-speech with Piper; output as 8 kHz mono WAV in the sounds folder shared with Asterisk."""
import os
import subprocess
import tempfile
import threading
import time
import wave

_voices = {}
_lock = threading.Lock()


class TtsError(Exception):
    pass


def _voice(cfg):
    name = cfg.get("tts_voice") or "nl_BE-nathalie-medium"
    with _lock:
        if name not in _voices:
            try:
                from piper import PiperVoice
            except ImportError as e:
                raise TtsError("Piper (tekst-naar-spraak) is niet geïnstalleerd") from e
            path = os.path.join(cfg["voices_dir"], f"{name}.onnx")
            if not os.path.exists(path):
                raise TtsError(f"Stem niet gevonden: {path}")
            _voices[name] = PiperVoice.load(path)
        return _voices[name]


def synthesize(cfg, text, name):
    """Writes <sounds_dir>/<name>.wav and returns the Asterisk sound name "alarm/<name>"."""
    voice = _voice(cfg)
    os.makedirs(cfg["sounds_dir"], exist_ok=True)
    target = os.path.join(cfg["sounds_dir"], f"{name}.wav")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        raw = tmp.name
    try:
        with _lock, wave.open(raw, "wb") as wf:
            voice.synthesize_wav(text, wf)
        # Asterisk plays .wav as 8 kHz, 16 bit, mono; "gain -n -1" avoids clipping after resampling.
        subprocess.run(["sox", raw, "-r", "8000", "-c", "1", "-b", "16", target + ".tmp.wav", "gain", "-n", "-1"],
                       check=True, capture_output=True)
        os.chmod(target + ".tmp.wav", 0o644)
        os.replace(target + ".tmp.wav", target)
    except (subprocess.CalledProcessError, OSError) as e:
        raise TtsError(f"Spraak maken mislukt: {e}") from None
    finally:
        os.unlink(raw)
    return f"alarm/{name}"


def cleanup(cfg, max_age=3 * 86400):
    """Removes old call messages (the confirmation sound is kept)."""
    folder = cfg["sounds_dir"]
    if not os.path.isdir(folder):
        return
    for f in os.listdir(folder):
        p = os.path.join(folder, f)
        if f.startswith("call-") and time.time() - os.path.getmtime(p) > max_age:
            os.unlink(p)
