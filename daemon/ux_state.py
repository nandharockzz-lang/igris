"""Runtime UI / arm state published under $XDG_RUNTIME_DIR/jarvis/."""

from __future__ import annotations

import os
import time

import numpy as np

import safefile

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".local", "share", "jarvis")


def _state_root():
    """Where the pipeline-state file lives.

    XDG_RUNTIME_DIR is per-user and mode 0700, so it is the right home. Fall
    back to a directory only this user can write instead of world-writable /tmp.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return runtime
    return os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state")


STATE_DIR = os.path.join(_state_root(), "jarvis")
STATE_FILE = os.path.join(STATE_DIR, "state")
MODE_FILE = os.path.join(STATE_DIR, "mode")
ARM_FILE = os.path.join(STATE_DIR, "armed_until")
WAKE_FILE = os.path.join(STATE_DIR, "wake")
LEVEL_FILE = os.path.join(STATE_DIR, "level")
MIC_FILE = os.path.join(STATE_DIR, "mic")
TOOL_FILE = os.path.join(STATE_DIR, "tool")
REQUEST_FILE = os.path.join(STATE_DIR, "request")
RESPONSE_FILE = os.path.join(STATE_DIR, "response")
UI_TEXT_LIMIT = 1200
STARTUP_ERROR_FILE = os.path.join(STATE_DIR, "startup_error")
CUSTOM_WAKE_DIR = os.path.join(JARVIS_DIR, "wake-models")
JARVIS_VOX_CONFIG = os.path.join(STATE_DIR, "voxtype.toml")
AGENT_CWD = os.path.join(STATE_DIR, "agent-cwd")

MODES = ("safe", "workspace", "privileged")

# Default arm windows (mirrored from listen DEFAULTS.workspace).
_DEFAULT_AUTO_DISARM = 1800.0
_DEFAULT_FULL_DISARM = 300.0

MIC_BUCKETS = 12


def note_startup_error(msg):
    """Record why the listener refused to start (panel reads this)."""
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        safefile.write_atomic(STARTUP_ERROR_FILE, str(msg)[:500])
    except OSError:
        pass


def clear_startup_error():
    try:
        os.unlink(STARTUP_ERROR_FILE)
    except OSError:
        pass

def current_mode(cfg):
    mode = cfg.get("mode", "safe")
    return mode if mode in MODES else "safe"


def workspace_timeout(cfg, mode="workspace"):
    """Arm window for a mode. Privileged ("Full") keeps the short window;
    workspace ("Basic") gets the longer one -- broker verbs only."""
    key = "full_disarm_seconds" if mode == "privileged" else \
        "auto_disarm_seconds"
    try:
        secs = float(cfg.get("workspace", {}).get(
            key, _DEFAULT_AUTO_DISARM if key == "auto_disarm_seconds" else _DEFAULT_FULL_DISARM))
    except (TypeError, ValueError):
        return 300.0 if mode == "privileged" else 1800.0
    return min(max(secs, 60.0), 3600.0)


def read_arm_deadline(arm_file=ARM_FILE):
    """The workspace/privileged window's end (epoch seconds), or None."""
    try:
        return float(safefile.read_text(arm_file, 64).strip())
    except (OSError, ValueError):
        return None


def arm_active(mode, now=None, arm_file=ARM_FILE):
    """Authoritative arm state. Safe is always-on by definition; every other
    mode is armed only inside a live window published at startup. The panel
    reflects this file -- it never defines it: flipping config behind a
    running daemon cannot arm anything until the daemon itself restarts and
    republishes the deadline."""
    if mode == "safe":
        return True
    deadline = read_arm_deadline(arm_file)
    if deadline is None:
        return False
    return (now if now is not None else time.time()) < deadline

def set_state(state):
    """Publish pipeline state for the bar widget (idle/listening/thinking/speaking).

    safefile.write_atomic writes an unpredictably named 0600 temp file inside
    the 0700 state dir and renames it over the target, so there is no
    guessable `state.tmp` to pre-plant and the widget's once-a-second reader
    only ever sees a complete value.
    """
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        safefile.write_atomic(STATE_FILE, state)
    except OSError:
        pass


def publish_ui_text(path, text):
    """Publish bounded current-turn text to the local runtime UI only."""
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        safefile.write_atomic(path, str(text or "")[:UI_TEXT_LIMIT])
    except OSError:
        pass


def clear_ui_text():
    publish_ui_text(REQUEST_FILE, "")
    publish_ui_text(RESPONSE_FILE, "")


def on_signal(_signum, _frame):
    global _running
    _running = False


# --------------------------------------------------------------------------
# UI telemetry: numbers only, never audio, transcripts or arguments.
#
# The avatar/EQ widgets read these; every file holds bounded numeric tokens.
# Transient by design: levels decay to 0, mic buckets go stale (>1.5s) the
# moment listening ends, and nothing here is ever logged or persisted beyond
# the runtime dir (tmpfs, per-user 0700).
# --------------------------------------------------------------------------

def _publish(path, text):
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        safefile.write_atomic(path, text)
    except OSError:
        pass


def note_wake():
    """Mark a wake-word detection for the 2s avatar flash."""
    _publish(WAKE_FILE, f"{time.time():.0f}")


def set_level(value):
    """Playback amplitude 0-100 + epoch for the speaking avatar. Only ever
    written while Piper audio is actually playing; otherwise 0 (never faked).
    The epoch lets the widget ignore a stale file from a crashed run."""
    _publish(LEVEL_FILE,
             f"{max(0, min(100, int(value)))} {time.time():.0f}")


def publish_tool(category):
    """Last tool-activity category + epoch (e.g. "broker:volume"). Category
    only -- never arguments, outputs or transcripts."""
    _publish(TOOL_FILE, f"{category[:32]} {time.time():.0f}")


MIC_BUCKETS = 12


def rms_to_100(value, gate):
    """Gate + scale a raw RMS value to a 0-100 EQ bucket. Below the noise
    gate: 0, so idle room noise never jitters the visualization."""
    if value <= gate:
        return 0
    return max(0, min(100, int((value - gate) / max(gate, 1.0) * 60)))


def publish_mic(history):
    """12 gated amplitude buckets + epoch. Raw mic audio is never stored --
    only these transient buckets, which the widget dims once stale."""
    buckets = ([0] * MIC_BUCKETS + list(history))[-MIC_BUCKETS:]
    _publish(MIC_FILE, " ".join(str(max(0, min(100, int(v)))) for v in buckets)
             + f" {time.time():.0f}")


def _wav_envelope(path, bucket_ms=100):
    """Per-bucket RMS 0-100 of a 16-bit mono wav, for amplitude-synced
    speaking animation. Pure function of the file bytes (unit-tested)."""
    import wave
    try:
        with wave.open(path, "rb") as fh:
            rate = fh.getframerate() or 22050
            frames = fh.readframes(fh.getnframes())
    except (OSError, wave.Error):
        return []
    if not frames:
        return []
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
    per = max(1, int(rate * bucket_ms / 1000))
    peak = float(np.max(np.abs(samples))) or 1.0
    out = []
    for i in range(0, len(samples), per):
        chunk = samples[i:i + per]
        out.append(int(float(np.sqrt(np.mean(chunk ** 2))) / peak * 100))
    return out

