#!/usr/bin/env python3
"""Wake-word voice assistant: say the wake word -> ask an agent -> speak the answer.

Runs as a systemd user service, toggled from the Omarchy bar widget
(dorian.voice). Everything except the agent call is local: openWakeWord
listens on a continuous 16kHz mic stream (~3% of one core), voxtype's whisper
model transcribes, piper speaks the reply.

Which agent answers is configuration, not code -- see config.toml.example.
Point it at Claude Code, a local ollama model, or anything else with a
non-interactive CLI, bearing in mind that only an invocation which actually
denies the CLI its tools is answer-only; that file explains how to check.

Pipeline state is written to $XDG_RUNTIME_DIR/jarvis/state (falling back to
$XDG_STATE_HOME, never to a world-writable /tmp) so the bar widget can show
what it is doing without talking to this process.
"""

import argparse
import collections
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
import wave

import numpy as np

# Repo flat layout or install-time lib/: shared modules live next to this
# file during development, and under $JARVIS_DIR/lib after install.sh.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _c in (_HERE, os.path.join(_HERE, "lib"),
           os.path.join(os.path.dirname(_HERE), "lib")):
    _abs = os.path.abspath(_c)
    if os.path.isfile(os.path.join(_abs, "safefile.py")):
        if _abs not in sys.path:
            sys.path.insert(0, _abs)
        break
else:
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
try:
    import jarvis_libpath
    jarvis_libpath.ensure()
except ImportError:
    pass
import safefile
import actions  # versioned local-action contract (grammar, confirmation)
import agent_posture
import routines
import ux_state

# Re-export extracted modules so tests and jarvis-config keep using jl.*.
from agent_posture import (  # noqa: E402
    ACTIONS_PROMPT, Agent, DEFAULTS, GROK_DEFAULT_MODEL, GROK_DISALLOWED_TOOLS,
    GROK_DISALLOWED_TOOLS_ARG, KNOWN_CLIS, NO_TOOLS_PROMPT, OPENCODE_DENY_KEYS,
    STYLE_PROMPT, TOOLS_DENIED, TOOLS_GRANTED, TOOLS_UNKNOWN, WEB_PROMPT,
    WEB_TURN_PROMPT, agent_cwd, capability_label, check_mode_invariants,
    grants_tools, grok_tools_denied, inject_model_flag, jarvis_agent_run_path,
    merge, opencode_agent_name, opencode_frontmatter_denies, select_agent,
    tool_posture,
)
from ux_state import (  # noqa: E402
    AGENT_CWD, ARM_FILE, CUSTOM_WAKE_DIR, JARVIS_VOX_CONFIG, LEVEL_FILE,
    MIC_BUCKETS, MIC_FILE, MODE_FILE, MODES, REQUEST_FILE, RESPONSE_FILE,
    STARTUP_ERROR_FILE, STATE_DIR, STATE_FILE, TOOL_FILE, UI_TEXT_LIMIT,
    WAKE_FILE, arm_active, clear_startup_error, clear_ui_text, current_mode,
    note_startup_error, note_wake, publish_mic, publish_tool, publish_ui_text,
    read_arm_deadline, rms_to_100, set_level, set_state, workspace_timeout,
    _wav_envelope,
)
from routines import (  # noqa: E402
    MUSIC_QUERY_RE, PENDING_CONFIRM, ROUTINE_CONFIRM, match_routine_intent,
    match_routine_intents, _command_words,
)

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".local", "share", "jarvis")
VOICES_DIR = os.path.join(JARVIS_DIR, "voices")
VENV_PY = os.path.join(JARVIS_DIR, "venv", "bin", "python")
JARVIS_BIN = os.path.join(JARVIS_DIR, "bin")

CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
CONFIG_PATH = os.path.join(CONFIG_HOME, "jarvis", "config.toml")

RATE = 16000
CHUNK_SAMPLES = 1280           # openWakeWord wants 80ms frames
CHUNK_BYTES = CHUNK_SAMPLES * 2

# openWakeWord ships these wake-word models. The other .onnx files in
# its resources dir are feature extractors and intent classifiers, not wake
# words -- naming one still works, but these are the supported set.
WAKE_WORDS = ("hey_jarvis", "alexa", "hey_mycroft", "hey_marvin", "igris")

# Speech engines Jarvis can drive through `voxtype transcribe --engine`.
# Only whisper is verified against the current voxtype binary. Parakeet
# model names stay known for tools/stt-bench.py and for opt-in via
# JARVIS_EXPERIMENTAL_STT=1; the panel and default config path refuse it.
STT_ENGINES = ("whisper",)
STT_ENGINES_EXPERIMENTAL = ("parakeet",)
WHISPER_MODELS = ("tiny", "tiny.en", "base", "base.en", "small", "small.en",
                  "medium", "medium.en", "large-v3", "large-v3-turbo")
PARAKEET_MODELS = ("parakeet-tdt-0.6b-v2", "parakeet-tdt-0.6b-v2-int8",
                   "parakeet-tdt-0.6b-v3", "parakeet-tdt-0.6b-v3-int8",
                   "parakeet-unified-en-0.6b")
VOXTYPE_MODELS_DIR = os.path.join(os.path.expanduser("~"), ".local", "share",
                                  "voxtype", "models")

# Redacted audit log. Deliberately NOT the journal and NOT under
# XDG_RUNTIME_DIR (tmpfs, cleared on reboot): it must survive to prove
# containment later. One JSON object per line, redacted by construction --
# sizes and kinds, never transcripts, secrets or full URLs.
def _audit_path():
    base = (os.environ.get("XDG_STATE_HOME")
            or os.path.join(HOME, ".local", "state"))
    return os.path.join(base, "jarvis", "audit.log")

AUDIT_FILE = _audit_path()

_running = True


def log(msg):
    # stderr, not stdout: jarvis-config prints JSON on stdout and the bar
    # widget parses it. journald captures both streams either way.
    print(f"[jarvis] {msg}", file=sys.stderr, flush=True)


def audit(event):
    """Append one redacted audit record (JSONL). Never raises.

    Schema: {time, mode, transcript_len, tool_name, args_summary_sanitized,
    approval_source, result_code}. No transcript text, no secrets, no full
    URLs, no arbitrary tool arguments, no raw model output -- callers pass
    only lengths, categories and allowlisted enum values (origin-only for
    URLs). The file is append-only via a no-follow, owner-checked open, and
    rotated past a bound so it cannot grow without limit.
    """
    import datetime
    import json
    record = {"time": datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")}
    record.update(event)
    try:
        line = json.dumps(record, separators=(",", ":"))[:2048]
        os.makedirs(os.path.dirname(AUDIT_FILE), mode=0o700, exist_ok=True)
        _rotate_audit()
        with safefile.open_append_nofollow(AUDIT_FILE) as fh:
            fh.write(line + "\n")
    except OSError as exc:
        log(f"audit log unwritable ({exc})")


# Bound on the audit file itself; older records spill to audit.log.prev
# (single generation -- forensics, not archiving).
MAX_AUDIT_BYTES = 4 << 20


def _rotate_audit():
    try:
        if os.path.getsize(AUDIT_FILE) <= MAX_AUDIT_BYTES:
            return
    except OSError:
        return
    try:
        # Same-directory atomic rename: replaces the name itself and follows
        # nothing, so a symlink planted at either name is moved, not written
        # through. Non-fatal either way -- audit() still appends below.
        os.replace(AUDIT_FILE, AUDIT_FILE + ".prev")
    except OSError as exc:
        log(f"audit rotation failed ({exc})")


def load_config(path=CONFIG_PATH):
    """DEFAULTS, with ~/.config/jarvis/config.toml layered on top if present."""
    cfg = DEFAULTS
    try:
        # Descriptor-first and bounded: a symlink, FIFO or oversized file at
        # this predictable path is refused, not followed, waited on, or slurped.
        raw = safefile.read_bytes(path, safefile.MAX_CONFIG_BYTES)
    except FileNotFoundError:
        log("no config file, using defaults")
        return cfg
    except OSError as exc:
        log(f"config unreadable ({exc}), using defaults")
        return cfg
    try:
        cfg = merge(DEFAULTS, tomllib.loads(raw.decode("utf-8")))
        log(f"config: {path}")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        log(f"config unreadable ({exc}), using defaults")
    return cfg


def resolve_voice(cfg):
    voice = cfg.get("voice", DEFAULTS["voice"])
    return voice if os.path.isabs(voice) else os.path.join(VOICES_DIR, voice)


def wake_models():
    """Wake models actually on disk: {name: (path, score_stem)}.

    Custom ~/.local/share/jarvis/wake-models first (survives venv
    rebuilds; home of a trained/downloaded igris model), then the files
    openWakeWord ships. Feature extractors (melspectrogram, embedding,
    VAD) are skipped -- they are not wake words. The panel offers exactly
    these keys; anything else is refused before it can kill the listener.
    """
    import openwakeword

    found = {}
    pkg = os.path.join(os.path.dirname(openwakeword.__file__),
                       "resources", "models")
    for models_dir in (CUSTOM_WAKE_DIR, pkg):
        try:
            files = sorted(os.listdir(models_dir))
        except OSError:
            continue
        for f in files:
            if not f.endswith(".onnx"):
                continue
            stem = os.path.splitext(f)[0]
            if stem in ("melspectrogram", "embedding_model", "silero_vad"):
                continue
            name = re.sub(r"_v\d.*$", "", stem)
            found.setdefault(name, (os.path.join(models_dir, f), stem))
    return found


def resolve_wake_model(cfg):
    """Map a wake-word name to an installed onnx file.

    Only models wake_models() actually finds are accepted: a name with no
    file (a not-yet-trained "igris", a typo) refuses startup with a clear
    message instead of dying inside openWakeWord under systemd.
    Returns (path, score_key). The score key is the file stem, which is
    what Model.predict() uses to label its scores.
    """
    name = cfg.get("wake_word", DEFAULTS["wake_word"])
    models = wake_models()
    if name in models:
        return models[name]
    hint = ""
    if name == "igris":
        hint = (f" Put a trained model at {CUSTOM_WAKE_DIR}/igris_v0.1.onnx "
                "(or run jarvis-config install-wake-word), then select it.")
    raise SystemExit(f"[jarvis] wake_word '{name}' is not installed."
                     f" Installed: {', '.join(sorted(models)) or 'none'}."
                     + hint)


def on_signal(_signum, _frame):
    global _running
    _running = False


# --------------------------------------------------------------------------
# Audio in
# --------------------------------------------------------------------------

def open_mic():
    """Continuous raw 16kHz mono s16 stream from PipeWire on stdout.

    pw-record exits early if its stderr is subprocess.DEVNULL, so give it a
    real file to write to. Mark the stream as capture/communication audio:
    pw-record otherwise advertises its default Playback/Music properties,
    which lets WirePlumber apply playback policy when another player starts
    and can leave the wake listener starved or linked to the wrong policy
    target.
    """
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    err = safefile.open_w_nofollow(os.path.join(STATE_DIR, "pw-record.log"))
    return subprocess.Popen(
        ["pw-record", "--media-category=Capture",
         "--media-role=Communication", "--rate=16000", "--channels=1",
         "--format=s16", "--latency=40ms", "-"],
        stdout=subprocess.PIPE,
        stderr=err,
    )


def read_chunk(mic):
    """Read one full frame. Short reads are normal while the stream spins up,
    so only a dead pw-record counts as the end of the stream."""
    buf = b""
    while len(buf) < CHUNK_BYTES:
        part = mic.stdout.read(CHUNK_BYTES - len(buf))
        if not part:
            if mic.poll() is not None:
                return None
            time.sleep(0.01)
            continue
        buf += part
    return np.frombuffer(buf, dtype=np.int16)


def rms(samples):
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


def tone(freq, ms=120):
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    envelope = np.minimum(1.0, np.minimum(t * 40, (n / RATE - t) * 40))
    wave_data = 0.25 * np.sin(2 * np.pi * freq * t) * envelope
    return (wave_data * 32767).astype(np.int16).tobytes()


def chime(kind):
    """Short feedback tone so you know it heard you, without a notification.

    The timeout matters more than the tone: pw-play blocking on a wedged
    audio server would otherwise hang the listener, not just skip a beep.
    """
    freq = 880 if kind == "start" else 440
    try:
        subprocess.run(
            ["pw-play", "--rate=16000", "--channels=1", "--format=s16", "-"],
            input=tone(freq), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        log("chime timed out; is the audio server healthy?")


def capture_command(mic, ambient, listen):
    """Record until the speaker stops. Returns int16 samples, or None.

    While recording, publishes gated amplitude buckets for the EQ widget
    (10Hz, transient -- raw audio is accumulated only for transcription and
    never stored). Buckets zero out when recording ends.
    """
    threshold = max(ambient * 3.0, 300.0)
    frames = []
    speech_time = 0.0
    silence_time = 0.0
    elapsed = 0.0
    frame_secs = CHUNK_SAMPLES / RATE
    history = []
    last_pub = 0.0

    try:
        while _running and elapsed < listen["max_command"]:
            samples = read_chunk(mic)
            if samples is None:
                return None
            frames.append(samples)
            elapsed += frame_secs

            level = rms(samples)
            history.append(rms_to_100(level, threshold))
            now = time.monotonic()
            if now - last_pub >= 0.1:
                publish_mic(history)
                last_pub = now

            if level > threshold:
                speech_time += frame_secs
                silence_time = 0.0
            else:
                silence_time += frame_secs
                if speech_time >= listen["min_speech"] and silence_time >= listen["silence_tail"]:
                    break
    finally:
        publish_mic([])  # listening over: dim the EQ immediately

    if speech_time < listen["min_speech"]:
        return None
    return np.concatenate(frames)


def write_wav(samples, path):
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(RATE)
        fh.writeframes(samples.tobytes())


# --------------------------------------------------------------------------
# Bounded subprocess execution
# --------------------------------------------------------------------------

# Ceilings on what a child process can make this always-on service hold.
MAX_CAPTURE_BYTES = 1 << 20   # any child's stdout
MAX_ERR_BYTES = 64 << 10      # stderr is only ever quoted in error messages
MAX_SPOKEN_CHARS = 1200       # the reply is three short sentences; this is slack
# Pipes are drained and capped below, but a child writing to a *file* -- the
# agent's reply file, piper's wav -- is writing past us, and a runaway one
# would keep going until its timeout or until the disk filled. RLIMIT_FSIZE
# is the ceiling the kernel enforces on our behalf: the child dies on SIGXFSZ
# instead. Both values are deliberately generous, because that rlimit applies
# to every file the child writes and not only the one we asked for -- an agent
# CLI also writes its own session and cache files, and killing it over one of
# those would be a bug we shipped for no gain. Read them as "cannot fill the
# disk", not as a tight fit: the reply we actually keep is capped again at
# read-back by safefile, and a capped reply synthesises to about two minutes
# of audio.
MAX_REPLY_FILE_BYTES = 64 << 20   # agent {outfile}: text, read back capped
MAX_WAV_BYTES = 64 << 20          # piper -f: ~2 min of 22 kHz 16-bit mono
# Process-wide file ceiling for the AGENT call only. Must stay far above any
# SQLite WAL the agent CLI checkpoints: opencode shares opencode.db with
# every other local session, and with a 64MB ceiling here its
# `PRAGMA wal_checkpoint` dies with SIGXFSZ and the call exits 1 -- every
# voice question answered "Sorry, I could not get an answer." Verified
# empirically: 64MB breaks opencode 1.18.30, 4GB works. Still bounds a
# runaway well below disk size; the reply itself is capped again at read-back.
AGENT_FILE_LIMIT_BYTES = 4 << 30

BoundedRun = collections.namedtuple(
    "BoundedRun", "returncode stdout stderr overflowed")


def _fsize_limiter(limit):
    """A preexec_fn that caps what the child may write to any single file.

    Deliberately one syscall and nothing else: this runs between fork and
    exec in a process that inherited our threads' locks, so anything that
    could allocate or take a lock would risk wedging the child there.
    """
    def apply():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    return apply


def run_bounded(argv, *, timeout, stdout_limit=MAX_CAPTURE_BYTES,
                stderr_limit=MAX_ERR_BYTES, input_text=None, cwd=None,
                file_limit=None):
    """subprocess.run(capture_output=True) minus the unbounded buffering.

    capture_output accumulates everything the child ever prints before any
    caller-side truncation can happen, so one runaway or compromised
    executable could grow this service without limit. Here each stream is
    drained into a capped buffer as it is produced; the moment either stream
    passes its ceiling the child is killed and the result comes back marked
    `overflowed` -- callers treat that as a failure, never as a long answer.
    A timeout kills the child and re-raises subprocess.TimeoutExpired, same
    as subprocess.run. `input_text` is fed to the child's stdin from a
    thread, so a child that never reads it cannot deadlock us; with no
    input_text, stdin is /dev/null rather than our own.

    `file_limit` caps, via RLIMIT_FSIZE, what the child may write to any file
    it opens -- the stream ceilings above say nothing about those. A child
    that exceeds it dies on SIGXFSZ, which arrives here as a non-zero
    returncode.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        preexec_fn=_fsize_limiter(file_limit) if file_limit else None,
    )
    out_buf, err_buf = bytearray(), bytearray()
    overflowed = threading.Event()

    def drain(stream, buf, limit):
        try:
            while True:
                chunk = stream.read(1 << 16)
                if not chunk:
                    return
                if len(buf) + len(chunk) > limit:
                    buf += chunk[:limit - len(buf)]
                    overflowed.set()
                    proc.kill()
                    # Keep the pipe moving until EOF so the dying child is
                    # never blocked writing to it.
                    while stream.read(1 << 16):
                        pass
                    return
                buf += chunk
        except (OSError, ValueError):
            pass

    def feed():
        try:
            proc.stdin.write(input_text.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, out_buf, stdout_limit)),
        threading.Thread(target=drain, args=(proc.stderr, err_buf, stderr_limit)),
    ]
    if input_text is not None:
        threads.append(threading.Thread(target=feed))
    for t in threads:
        t.daemon = True
        t.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for t in threads:
            t.join(timeout=5)
        raise
    # A grandchild holding the pipe open could stall a reader past the
    # child's own exit; the join timeout (plus daemon threads) means it
    # stalls the reader, not the listener.
    for t in threads:
        t.join(timeout=5)

    return BoundedRun(
        returncode=returncode,
        stdout=out_buf.decode("utf-8", "replace"),
        stderr=err_buf.decode("utf-8", "replace"),
        overflowed=overflowed.is_set(),
    )


# --------------------------------------------------------------------------
# Playback focus
# --------------------------------------------------------------------------

PLAYBACK_DUCK_LEVEL = 0.20
PLAYBACK_DUCK_EPSILON = 0.025
NON_MEDIA_OUTPUTS = frozenset((
    "wayvibes", "effect_output.j313-convolver", "quickshell",
))


def playback_is_active():
    """Return whether a real application is currently outputting audio.

    The wake model can often recognize a wake word over music, but the
    following utterance is much less reliable when the speaker signal is
    still loud. Query PipeWire's node state instead of guessing from a
    process name: this covers mpv, browsers, and other MPRIS players while
    excluding Omarchy's always-running audio-effect streams.
    """
    try:
        proc = run_bounded(["pw-dump"], timeout=2,
                           stdout_limit=4 << 20, stderr_limit=16 << 10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0 or proc.overflowed:
        return False
    try:
        nodes = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return False
    for node in nodes:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        info = node.get("info") or {}
        if info.get("state") != "running":
            continue
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        name = props.get("node.name", "")
        app = props.get("application.name", "")
        if name in NON_MEDIA_OUTPUTS or app in NON_MEDIA_OUTPUTS:
            continue
        return True
    return False


def _sink_volume():
    try:
        proc = run_bounded(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            timeout=2, stdout_limit=1024, stderr_limit=4096)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or proc.overflowed:
        return None
    match = re.search(r"Volume:\s+([0-9]+(?:\.[0-9]+)?)", proc.stdout)
    if not match:
        return None
    return float(match.group(1)), "[MUTED]" in proc.stdout


def begin_playback_focus():
    """Duck active media for the command, returning state for restoration.

    This is deliberately sink-level and temporary: it works for mpv,
    Chromium, and any other player routed to the current sink. It never
    raises or blocks the voice pipeline if PipeWire is unavailable.
    """
    if not playback_is_active():
        return None
    original = _sink_volume()
    if original is None or original[0] <= PLAYBACK_DUCK_LEVEL:
        return None
    try:
        proc = run_bounded(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@",
             f"{PLAYBACK_DUCK_LEVEL:.2f}"],
            timeout=2, stdout_limit=1024, stderr_limit=4096)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or proc.overflowed:
        return None
    log("playback ducked for voice capture")
    return original


def end_playback_focus(original):
    """Restore the sink only if nobody changed it while we were listening."""
    if original is None:
        return
    current = _sink_volume()
    if current is None or abs(current[0] - PLAYBACK_DUCK_LEVEL) > \
            PLAYBACK_DUCK_EPSILON:
        return
    try:
        proc = run_bounded(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@",
             f"{original[0]:.4f}"],
            timeout=2, stdout_limit=1024, stderr_limit=4096)
        if proc.returncode == 0 and original[1]:
            run_bounded(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
                        timeout=2, stdout_limit=1024, stderr_limit=4096)
    except (OSError, subprocess.TimeoutExpired):
        return
    log("playback restored after voice capture")


# --------------------------------------------------------------------------
# Transcribe -> agent -> speak
# --------------------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# voxtype logs to stdout alongside the transcript.
VOXTYPE_NOISE = ("Loading ", "Audio format:", "Processing ", "whisper_")


def resolve_stt(cfg):
    """Validate the [stt] section, failing loudly on typos.

    A misspelled engine or model must refuse startup with a clear message,
    not silently transcribe with whatever voxtype happens to default to.
    Parakeet is experimental until voxtype's ONNX path is verified here:
    set JARVIS_EXPERIMENTAL_STT=1 to opt in.
    """
    stt = merge(DEFAULTS["stt"], cfg.get("stt", {}))
    engine = stt.get("engine", "whisper")
    experimental = os.environ.get("JARVIS_EXPERIMENTAL_STT", "").strip() == "1"
    allowed_engines = STT_ENGINES + (STT_ENGINES_EXPERIMENTAL
                                     if experimental else ())
    if engine not in allowed_engines:
        if engine in STT_ENGINES_EXPERIMENTAL:
            raise SystemExit(
                f"[jarvis] stt engine {engine!r} is experimental and not "
                "verified on this voxtype build. Use engine = \"whisper\", "
                "or set JARVIS_EXPERIMENTAL_STT=1 to opt in.")
        raise SystemExit(f"[jarvis] unknown stt engine {engine!r}. "
                         f"Available: {', '.join(STT_ENGINES)}")
    model = stt.get("model", "")
    allowed = WHISPER_MODELS if engine == "whisper" else PARAKEET_MODELS
    if model not in allowed:
        raise SystemExit(f"[jarvis] unknown {engine} model {model!r}. "
                         f"Available: {', '.join(allowed)}")
    if engine == "whisper" and not os.path.isabs(model):
        # An absolute path names a custom .bin; otherwise the file must be
        # on disk already -- downloading happens outside the daemon.
        if not os.path.exists(os.path.join(VOXTYPE_MODELS_DIR,
                                            f"ggml-{model}.bin")):
            raise SystemExit(
                f"[jarvis] whisper model {model!r} is not downloaded. "
                f"Run: voxtype setup model (pick it), then restart Jarvis.")
    if not str(stt.get("language") or "").strip():
        raise SystemExit("[jarvis] stt language cannot be empty")
    if not str(stt.get("fallback_engine") or "").strip():
        raise SystemExit("[jarvis] stt fallback_engine cannot be empty")
    return stt


def write_voxtype_config(stt):
    """Materialize the jarvis-owned voxtype config for transcription.

    Minimal on purpose: transcribe only needs the engine section. The
    user's own config is never read or written here.
    """
    if stt["engine"] == "whisper":
        text = ("[whisper]\n"
                f"model = \"{stt['model']}\"\n"
                f"language = \"{stt['language']}\"\n"
                f"initial_prompt = \"{stt.get('vocabulary', '')}\"\n")
    else:
        text = ("[parakeet]\n"
                f"model = \"{stt['model']}\"\n")
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    safefile.write_atomic(JARVIS_VOX_CONFIG, text)


def _transcribe_once(cmd):
    """One voxtype run: transcript tail, or '' on any failure."""
    try:
        proc = run_bounded(cmd, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise
    if proc.overflowed:
        log("voxtype exceeded its output ceiling; transcription discarded")
        return ""
    if proc.returncode != 0:
        log(f"voxtype transcribe failed (exit {proc.returncode})")
        return ""
    lines = []
    for raw in proc.stdout.splitlines():
        line = ANSI.sub("", raw).strip()
        if not line or " INFO " in line or " WARN " in line:
            continue
        if line.startswith(VOXTYPE_NOISE):
            continue
        lines.append(line)
    return lines[-1] if lines else ""


def transcribe(path, stt):
    """Run the configured local STT engine; opt-in cloud fallback on empty.

    Local passes use the jarvis-owned -c config (pinned model, command
    vocabulary prompt). The cloud fallback, when enabled, goes through the
    user's own voxtype config -- their keys, their explicit opt-in -- and
    only when local transcription came back empty.
    """
    text = _transcribe_once(["voxtype", "-c", JARVIS_VOX_CONFIG,
                             "transcribe", "--engine", stt["engine"], path])
    if not text and stt.get("cloud_fallback"):
        fb = stt.get("fallback_engine") or ""
        if fb:
            log(f"local transcription empty; trying cloud fallback ({fb})")
            text = _transcribe_once(["voxtype", "transcribe",
                                     "--engine", fb, path])
    return text


def ask_agent(agent, prompt, web=False):
    """Run the configured agent CLI and return its spoken reply, or ''.

    web=True runs the agent's web_command instead -- the search-capable
    second half of a search exchange. The caller treats that reply as
    tainted: no directive from it is ever executed.
    """
    outfile = None
    uses_outfile = agent.web_uses_outfile if web else agent.uses_outfile
    if uses_outfile:
        fd, outfile = tempfile.mkstemp(suffix=".txt", prefix="jarvis-reply-")
        os.close(fd)

    system_extra = ""
    if agent.actions and not web:
        apps = installed_apps()
        if apps:
            system_extra = "Installed apps: " + ", ".join(apps) + "."

    argv, stdin_payload = agent.build_invocation(prompt, outfile, system_extra,
                                                 web=web)

    try:
        try:
            proc = run_bounded(argv, timeout=agent.timeout,
                               input_text=stdin_payload, cwd=agent_cwd(),
                               file_limit=AGENT_FILE_LIMIT_BYTES)
        except FileNotFoundError:
            log(f"agent '{agent.name}': '{agent.executable}' not found on PATH")
            return ""
        except subprocess.TimeoutExpired:
            log(f"agent '{agent.name}' timed out after {agent.timeout:.0f}s")
            return ""

        if proc.overflowed:
            log(f"agent '{agent.name}' exceeded its output ceiling; reply discarded")
            return ""
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:200]
            log(f"agent '{agent.name}' failed (exit {proc.returncode}): {detail}")
            return ""

        if outfile:
            try:
                # mkstemp made this one, but it lives in a world-writable /tmp
                # for the lifetime of the agent call: read it back the same
                # careful way as anything else, and cap what a runaway agent
                # can make us hold in memory.
                return safefile.read_text(outfile, safefile.MAX_TEXT_BYTES).strip()
            except OSError:
                log(f"agent '{agent.name}' wrote no reply file")
                return ""

        return clean_reply(proc.stdout, agent.strip_prefixes)
    finally:
        if outfile:
            try:
                os.unlink(outfile)
            except OSError:
                pass


def clean_reply(text, strip_prefixes):
    """Drop ANSI codes and any configured progress-log lines."""
    lines = []
    for raw in text.splitlines():
        line = ANSI.sub("", raw).rstrip()
        if strip_prefixes and line.strip().startswith(strip_prefixes):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Actions: a structured directive, brokered outside the agent
#
# The agent CLI never gets a shell or a tool grant. When actions are on, the
# agent asks for an action by ending its reply with one directive line; the
# daemon parses it against a strict pattern, validates the argument again
# here, and execs the jarvis-open broker directly -- one argv, no shell --
# which validates it a third time and can launch an installed .desktop entry
# or open an http(s) URL, nothing else. A prompt-level instruction plus a
# shell allowlist is not an authorization boundary; this is enforced where
# the agent cannot reach it.
#
# The optional search hand-off (run_search) rides the same rails: the
# no-tools first call may request one web-enabled round, and the reply of
# that round -- the only place web content can enter -- has every directive
# stripped and ignored, so what came off the web can never act here.
# --------------------------------------------------------------------------

DIRECTIVE_RE = re.compile(
    r"<<jarvis:(open-app|open-url|search|volume|brightness|workspace|mute|media|music|mymusic"
    r"|focus-window|move-window|fullscreen|lock|screenshot|notify"
    r"|close-window|logout|reboot|poweroff|wifi)"
    r"(?:\s+([^<>]{1,256}?))?\s*>>")
_DIRECTIVE_KINDS = {"open-app": "app", "open-url": "url", "search": "search",
                    "volume": "volume", "brightness": "brightness",
                    "workspace": "workspace", "mute": "mute",
                    "media": "media", "music": "music", "mymusic": "mymusic",
                    "focus-window": "focus-window",
                    "move-window": "move-window",
                    "fullscreen": "fullscreen", "lock": "lock",
                    "screenshot": "screenshot", "notify": "notify",
                    "close-window": "close-window", "logout": "logout",
                    "reboot": "reboot", "poweroff": "poweroff",
                    "wifi": "wifi"}
# Web-tainted replies get every <<jarvis:*>> stripped, even a malformed one
# that DIRECTIVE_RE would not match -- what came off the web is never trusted
# to even look like a directive.
TAINT_STRIP_RE = re.compile(r"<<jarvis:.*?(?:>>|$)", re.DOTALL)
# Search queries are daemon-sanitized before reaching web_command: collapse
# whitespace, cap length, strip anything directive-shaped.
SEARCH_STRIP_RE = re.compile(r"<<.*?>>")
# What we will pass the broker as an app query: printable, no leading dash,
# short. The broker only fuzzy-matches it against installed .desktop names.
APP_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}$")
# Same shape jarvis-open itself enforces before handing a URL to xdg-open.
URL_RE = re.compile(r"^https?://[^\s\"'\\<>]+$")


def jarvis_open_path():
    """The broker binary: installed under ~/.local/share/jarvis/bin, or next
    to this file when running from a checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(JARVIS_BIN, "jarvis-open"),
                      os.path.join(here, "jarvis-open")):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


VOLUME_RE = re.compile(r"^(\d{1,3}|up|down|mute|unmute)$")
BRIGHTNESS_RE = re.compile(r"^(\d{1,3}|up|down)$")
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,31}$")
MUTE_RE = re.compile(r"^(on|off|toggle|mute|unmute)$")
MEDIA_RE = re.compile(r"^(play|pause|toggle|next|prev|stop|quit)$")
# Account collections: favourites/watch-later open your liked/WL pages (your
# login comes from the default browser, never from credentials here); named
# playlists resolve through the [music.playlists] map in config.toml, so no
# cookie or token is ever read, stored or logged.
MYMUSIC_RE = re.compile(
    r"^(liked|favourites|favorites|watchlater|watch later"
    r"|playlist [A-Za-z0-9 _.'\-]{1,40})$")
# Broker exec ceilings: network-backed verbs get longer than the 15s default.
BROKER_TIMEOUTS = {"music": 40, "mymusic": 40}


def directive_summary(kind, value):
    """Structurally safe audit summary: categories and lengths, never content.

    Free-form agent text (app queries, workspace names) is logged as a
    LENGTH only -- it can carry spoken words. URL destinations log as
    origin-only (no path, query or userinfo). Only values from a tiny
    allowlisted enum (volume/brightness/mute settings) log verbatim.
    """
    if kind == "url":
        try:
            origin = urllib.parse.urlsplit(value)
            host = origin.hostname or ""
            port = origin.port
            return f"{origin.scheme}://{host}" + (f":{port}" if port else "")
        except ValueError:
            return "(invalid url)"
    if kind in ("volume", "brightness", "mute", "media"):
        return value[:16] if isinstance(value, str) else kind
    if isinstance(value, (list, tuple)):
        return f"{kind}_len={sum(len(str(v)) for v in value)}"
    return f"{kind}_len={len(value)}"


# The list behind the actions prompt costs a broker spawn plus a full
# .desktop scan. Fine once, needless on every question of a conversation --
# but the daemon runs for days, so a process-lifetime cache would hide a
# newly installed app until restart. A short TTL gets both, and failures are
# not cached, so a transient broker problem is retried on the next exchange.
_APPS_TTL_SECONDS = 60.0
_apps_cache = {"at": 0.0, "names": []}


def installed_apps():
    """App names for the actions system prompt, from the broker's `list`.

    The agent has no way to run `jarvis-open list` itself any more, so tell
    it what is installed up front. Bounded like every other child, capped
    well below any prompt-size trouble, and cached briefly (see above).
    """
    now = time.monotonic()
    if _apps_cache["names"] and now - _apps_cache["at"] < _APPS_TTL_SECONDS:
        return _apps_cache["names"]
    broker = jarvis_open_path()
    if broker is None:
        return []
    try:
        proc = run_bounded([broker, "list"], timeout=10,
                           stdout_limit=256 << 10, stderr_limit=16 << 10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 or proc.overflowed:
        return []
    names, total = [], 0
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        total += len(name) + 2
        if total > 4000:
            break
        names.append(name)
    _apps_cache["at"] = now
    _apps_cache["names"] = names
    return names


def extract_directive(reply):
    """Split a reply into (spoken_text, directive-or-None).

    Directives are stripped from spoken/UI text whether or not actions are
    enabled. Grok often inlines them on the same line as the sentence
    (`Playing X now. <<jarvis:music ...>>`); those must still count.
    Only the first valid directive is executed.
    """
    directive = None
    for match in DIRECTIVE_RE.finditer(reply or ""):
        if directive is None:
            directive = (_DIRECTIVE_KINDS[match.group(1)],
                         (match.group(2) or "").strip())
    spoken = DIRECTIVE_RE.sub("", reply or "")
    spoken = TAINT_STRIP_RE.sub("", spoken)
    spoken = re.sub(r"[ \t]{2,}", " ", spoken)
    spoken = re.sub(r"\n{3,}", "\n\n", spoken).strip(" \t")
    spoken = "\n".join(line.strip() for line in spoken.splitlines()).strip()
    return spoken, directive


def run_directive(directive, mode="safe", approval_source="voice",
                  require_arm=False, arm_file=ARM_FILE):
    """Public entry: publishes the tool category for the activity indicator,
    then enforces + executes. Category only -- never arguments.

    Returns True on success, False on refusal/failure, or "pending" when
    the broker staged an on-screen confirmation (needs_confirm verb on
    voice authority): the caller must tell the user to tap Confirm/Deny.
    """
    publish_tool(f"broker:{directive[0]}")
    try:
        return _run_directive(directive, mode, approval_source,
                              require_arm, arm_file)
    finally:
        publish_tool(f"broker:{directive[0]}")


def _refuse(mode, kind, summary, approval_source, reason):
    """Shared refusal: log the reason, audit the shape, return False."""
    log(f"directive refused: {reason}")
    audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
           "args_summary_sanitized": summary,
           "approval_source": approval_source,
           "result_code": "refused-validation"})
    return False


def _run_directive(directive, mode="safe", approval_source="voice",
                   require_arm=False, arm_file=ARM_FILE):
    """Validate one directive and exec the broker for it. True on success.

    Enforcement lives here, not in the prompt: Safe mode refuses everything
    (always-on mic never acts); Workspace/Privileged allow exactly the
    brokered verbs below, each re-validated here AND in jarvis-open, exec'd
    as argv with no shell anywhere in the path. Voice never authorizes
    destructive ops -- there are none in this verb set by design.

    require_arm=True (the always-on voice path) additionally demands a live
    arm window: a disarmed or expired workspace/privileged state refuses
    every request. Explicit physical invocations (--ask hotkey/button) are
    the arming act itself and pass require_arm=False.
    """
    kind, value = directive
    summary = directive_summary(kind, value)
    if mode == "safe":
        log("directive refused: safe mode never acts")
        audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-safe"})
        return False
    if require_arm and not arm_active(mode, arm_file=arm_file):
        log("directive refused: disarmed or expired arm state")
        audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
               "args_summary_sanitized": summary,
               "approval_source": approval_source,
               "result_code": "refused-disarmed"})
        return False
    if kind == "app" and not APP_QUERY_RE.match(value):
        log("directive refused: app name failed validation")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:app",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-validation"})
        return False
    if kind == "url" and not URL_RE.match(value):
        log("directive refused: not a plain http(s) url")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:url",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-validation"})
        return False
    if kind == "volume":
        if not VOLUME_RE.match(value):
            log("directive refused: volume failed validation")
            audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:volume",
                   "args_summary_sanitized": summary,
                   "approval_source": approval_source, "result_code": "refused-validation"})
            return False
        if value.isdigit() and not 0 <= int(value) <= 100:
            log("directive refused: volume out of range")
            audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:volume",
                   "args_summary_sanitized": summary,
                   "approval_source": approval_source, "result_code": "refused-range"})
            return False
    if kind == "brightness":
        if not BRIGHTNESS_RE.match(value):
            log("directive refused: brightness failed validation")
            audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:brightness",
                   "args_summary_sanitized": summary,
                   "approval_source": approval_source, "result_code": "refused-validation"})
            return False
        if value.isdigit() and not 1 <= int(value) <= 100:
            log("directive refused: brightness out of range")
            audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:brightness",
                   "args_summary_sanitized": summary,
                   "approval_source": approval_source, "result_code": "refused-range"})
            return False
    if kind == "workspace" and not WORKSPACE_RE.match(value):
        log("directive refused: workspace name failed validation")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:workspace",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-validation"})
        return False
    if kind == "mute" and not MUTE_RE.match(value):
        log("directive refused: mute failed validation")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:mute",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-validation"})
        return False
    if kind == "media" and not MEDIA_RE.match(value):
        log("directive refused: media failed validation")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:media",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-validation"})
        return False
    if kind == "music" and not MUSIC_QUERY_RE.match(value):
        log("directive refused: music query failed validation")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:music",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-validation"})
        return False
    if kind == "mymusic" and not MYMUSIC_RE.match(" ".join(value.lower().split())):
        log("directive refused: mymusic spec failed validation")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "broker:mymusic",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-validation"})
        return False
    # New broker verbs validate against the shared contract grammar (see
    # daemon/actions.py): fixed intents, strict args, confirmation flags.
    argv_extra = []
    if kind in ("focus-window",):
        if not actions.FOCUS_QUERY_RE.match(value):
            return _refuse(mode, kind, summary, approval_source,
                           "focus query failed validation")
    elif kind == "move-window":
        # Fast path passes [query, target]; agent directives pass one
        # string split on the last space (target is the validated tail).
        if isinstance(value, list) and len(value) == 2:
            query, target = value
        elif isinstance(value, str):
            query, _, target = value.rpartition(" ")
        else:
            return _refuse(mode, kind, summary, approval_source,
                           "move-window needs QUERY TARGET")
        target = {"next": "+1", "previous": "-1"}.get(target, target)
        if not query or not actions.FOCUS_QUERY_RE.match(query) or \
                not (target in ("+1", "-1") or WORKSPACE_RE.match(target)):
            return _refuse(mode, kind, summary, approval_source,
                           "move-window needs QUERY TARGET")
        argv_extra = [query, target]
        summary = directive_summary(kind, value)
    elif kind in ("fullscreen", "lock", "close-window", "logout", "reboot",
                  "poweroff"):
        if value:
            return _refuse(mode, kind, summary, approval_source,
                           "this verb takes no argument")
    elif kind == "screenshot":
        if value and value not in ("full", "window"):
            return _refuse(mode, kind, summary, approval_source,
                           "screenshot wants full|window")
        argv_extra = [value] if value else []
    elif kind == "notify":
        if not actions.NOTIFY_RE.match(value):
            return _refuse(mode, kind, summary, approval_source,
                           "notification text failed validation")
    elif kind == "wifi":
        if not actions.WIFI_RE.match(value):
            return _refuse(mode, kind, summary, approval_source,
                           "wifi wants on|off")
    broker = jarvis_open_path()
    if broker is None:
        log("directive refused: jarvis-open broker not found")
        audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-no-broker"})
        return False
    broker_argv = [broker, kind] + argv_extra
    if not argv_extra and value:
        broker_argv.append(value)
    # Supervised invocations (--ask at a keyboard, panel/button taps)
    # attest needs_confirm verbs directly; the always-on voice path never
    # passes the flag, so the broker stages an on-screen confirmation.
    if actions.ACTIONS.get(kind, {}).get("confirm", False) \
            and approval_source != "voice":
        broker_argv.append("--confirm-button")
    try:
        proc = run_bounded(broker_argv,
                           timeout=BROKER_TIMEOUTS.get(kind, 15),
                           stdout_limit=64 << 10, stderr_limit=16 << 10)
    except (OSError, subprocess.TimeoutExpired):
        log("jarvis-open did not run")
        audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "error-exec"})
        return False
    if proc.returncode == 2:
        # Broker staged an on-screen confirmation: not a refusal, not a
        # success. The caller tells the user to tap Confirm/Deny.
        log(f"jarvis-open: confirmation staged for {kind}")
        audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
               "args_summary_sanitized": summary,
               "approval_source": approval_source,
               "result_code": "pending-confirm"})
        return "pending"
    if proc.returncode != 0 or proc.overflowed:
        log(f"jarvis-open refused: {(proc.stderr or proc.stdout).strip()[:200]}")
        audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-broker"})
        return False
    if kind in ("url", "music", "mymusic"):
        # A search URL carries the spoken question verbatim in its query
        # string, and the journal must not learn the transcript through a
        # side door. Audit the destination's origin, never the full URL --
        # and build that origin from hostname/port rather than netloc, which
        # would carry any `user:password@` straight into the journal we are
        # trying to keep secrets out of. The music broker prints the resolved
        # watch URL on stdout, so it gets the same treatment.
        log(f"jarvis-open: opened {summary} (full url not journaled)")
    else:
        log(f"jarvis-open: {proc.stdout.strip()[:200]}")
    audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
           "args_summary_sanitized": summary,
           "approval_source": approval_source, "result_code": "ok"})
    return True


MAX_SEARCH_QUERY_CHARS = 400


def run_search(agent, query, log_text=False, mode="safe",
               approval_source="voice"):
    """Public entry: publishes the search category for the activity
    indicator, then runs the single tainted hop."""
    publish_tool("search")
    try:
        return _run_search(agent, query, log_text, mode, approval_source)
    finally:
        publish_tool("search")


def _run_search(agent, query, log_text=False, mode="safe",
                approval_source="voice"):
    """The web-enabled second half of a search exchange. Returns spoken text.
    The gating here is by construction, not by trust. The first call ran
    with no tools at all, so nothing from the open web can have entered it:
    a directive it emits traces back to the speaker, and is executed. This
    call reads the web, so nothing it emits is trusted: every directive in
    its reply -- an open, another search, even a malformed <<jarvis:*>> --
    is stripped and ignored, which is what makes granting the search tool
    safe at all, and why there is exactly one hop. The full-tools agent
    never sees raw web: this path goes through web_command only.
    """
    if not agent.web:
        log("agent asked to search but has no web_command; refused")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "search",
               "args_summary_sanitized": f"query_len=0",
               "approval_source": approval_source, "result_code": "refused-no-web"})
        return "Sorry, I cannot search the web."
    # Daemon sanitizes: whitespace collapse, strip anything directive-shaped,
    # 400 char cap. The query is derived from speech: audit sizes, never text.
    query = SEARCH_STRIP_RE.sub(" ", query)
    query = " ".join(query.split())
    if not 0 < len(query) <= MAX_SEARCH_QUERY_CHARS:
        log("search query failed validation; refused")
        audit({"mode": mode, "transcript_len": 0, "tool_name": "search",
               "args_summary_sanitized": f"query_len={len(query)}",
               "approval_source": approval_source, "result_code": "refused-validation"})
        return "Sorry, I could not run that search."
    log(f"searching ({len(query)} characters)")
    raw = ask_agent(agent, query, web=True)
    reply, stray = extract_directive(raw)
    if stray:
        log(f"directive in a web-tainted reply ignored ({stray[0]})")
    # Belt-and-braces: strip any remaining <<jarvis:*>> the strict parser
    # did not match, then audit lengths only.
    tainted = TAINT_STRIP_RE.sub("", reply).strip()
    if tainted != reply:
        log("web-tainted markup stripped from reply")
        reply = tainted
    audit({"mode": mode, "transcript_len": 0, "tool_name": "search",
           "args_summary_sanitized":
               f"query_len={len(query)} result_len={len(reply)}",
           "approval_source": approval_source, "result_code": "ok"})
    return (reply[:MAX_SPOKEN_CHARS]
            or "Sorry, the search did not come back with an answer.")


def speak(text, voice):
    # Belt-and-braces: respond() caps the reply too, but nothing longer than
    # this ever reaches the synthesiser regardless of the path in.
    text = text[:MAX_SPOKEN_CHARS]
    if not text:
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out = tmp.name
    try:
        try:
            run_bounded(
                [VENV_PY, "-m", "piper", "-m", voice, "-f", out],
                input_text=text, timeout=180,
                stdout_limit=64 << 10, stderr_limit=64 << 10,
                file_limit=MAX_WAV_BYTES,
            )
        except subprocess.TimeoutExpired:
            log("speech synthesis timed out")
            return
        # A piper failure still leaves a bare 44-byte wav header behind.
        # The capped reply synthesises to at most a couple of minutes of
        # audio, so a playback still running at five is a wedged audio
        # server holding the listener hostage, not a long answer.
        if os.path.getsize(out) > 44:
            _play_with_levels(out)
        else:
            set_level(0)
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
        set_level(0)


def _play_with_levels(path, timeout=300):
    """Play a wav via pw-play while publishing its real amplitude envelope.

    The envelope is computed from the file bytes (see _wav_envelope), and
    the published level follows actual playback position -- the avatar mouth
    moves with the audio, and only while audio plays. Silent stretches
    publish 0; nothing is ever faked.
    """
    envelope = _wav_envelope(path)
    try:
        proc = subprocess.Popen(
            ["pw-play", path], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log(f"playback did not start ({exc})")
        return
    start = time.monotonic()
    try:
        while True:
            if proc.poll() is not None:
                break
            if time.monotonic() - start > timeout:
                log("playback timed out; is the audio server healthy?")
                break
            idx = int((time.monotonic() - start) * 10)
            set_level(envelope[idx] if 0 <= idx < len(envelope) else 0)
            time.sleep(0.1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        set_level(0)


def respond(agent, voice, text, log_text=False, mode="safe",
            approval_source="voice"):
    """Shared tail of the pipeline: ask, then say the answer out loud.

    The words themselves only reach the journal when log_transcripts opted
    in; by default the journal records that an exchange happened and how big
    it was, because spoken content can carry secrets and journald persists.
    Every exchange also appends a redacted audit record (sizes/kinds only).
    """
    log(f"heard: {text}" if log_text else f"heard {len(text)} characters")
    log(f"mode: {mode} approval: {approval_source}")
    if agent.actions:
        # Routine commands skip the model round-trip entirely: the intent is
        # unambiguous, the broker verbs are deterministic, and the LLM would
        # only add latency and failure modes. Same enforcement as the
        # directive path (mode + arm gating inside run_directive); a refusal
        # or a non-match falls through to the agent below. Compound speech
        # ("workspace 5 and open chromium") runs each matched clause.
        routines = match_routine_intents(text)
        if routines:
            pending = False
            ok_any = False
            spoken = []
            for routine in routines:
                outcome = run_directive(
                    routine, mode, approval_source,
                    require_arm=(approval_source == "voice"))
                audit({"mode": mode, "transcript_len": len(text),
                       "tool_name": f"broker:{routine[0]}",
                       "args_summary_sanitized": directive_summary(*routine),
                       "approval_source": approval_source,
                       "result_code": ("pending-confirm" if outcome == "pending"
                                       else "ok-intent" if outcome else "refused")})
                if outcome == "pending":
                    pending = True
                    spoken.append(PENDING_CONFIRM)
                elif outcome:
                    ok_any = True
                    spoken.append(ROUTINE_CONFIRM.get(routine[0],
                                                      "Doing it now."))
            if pending or ok_any:
                answer = " ".join(spoken) if spoken else "Doing it now."
                log(f"reply: {len(answer)} characters (routine intent, "
                    "no model call)")
                publish_ui_text(RESPONSE_FILE, answer)
                set_state("speaking")
                speak(answer, voice)
                return answer
            log("routine intent refused; falling through to the agent")
    answer, directive = extract_directive(ask_agent(agent, text))
    answer = answer[:MAX_SPOKEN_CHARS]
    if directive and directive[0] == "search":
        answer = run_search(agent, directive[1], log_text, mode,
                            approval_source)
        directive = None
    if directive:
        if agent.actions:
            ok = run_directive(directive, mode, approval_source,
                               require_arm=(approval_source == "voice"))
            if ok == "pending":
                note = PENDING_CONFIRM
                answer = (answer + " " + note).strip() if answer else note
            elif ok and not answer:
                answer = "Doing it now."
            elif not ok:
                answer = (answer + " Sorry, that did not open.").strip()
        else:
            log("agent sent a directive but actions are off; ignored")
            audit({"mode": mode, "transcript_len": len(text),
                   "tool_name": f"broker:{directive[0]}",
                   "args_summary_sanitized": directive_summary(*directive),
                   "approval_source": approval_source,
                   "result_code": "ignored-actions-off"})
            if not answer:
                answer = "Sorry, acting is turned off."
    # The generic fallback comes last, after directive handling: a reply that
    # was nothing but a directive line strips to empty, and silence is the
    # one answer a voice assistant must never give.
    if not answer:
        answer = "Sorry, I could not get an answer."
    log(f"reply: {answer[:120]}" if log_text else f"reply: {len(answer)} characters")
    audit({"mode": mode, "transcript_len": len(text),
           "tool_name": f"agent:{agent.name}",
           "args_summary_sanitized": f"reply_len={len(answer)}",
           "approval_source": approval_source, "result_code": "ok"})
    publish_ui_text(RESPONSE_FILE, answer)
    set_state("speaking")
    speak(answer, voice)
    return answer


def handle_command(mic, ambient, agent, voice, listen, log_text=False,
                   mode="safe", stt=None):
    focus = begin_playback_focus()
    text = ""
    try:
        chime("start")
        set_state("listening")
        samples = capture_command(mic, ambient, listen)
        if samples is None:
            log("nothing said")
            return

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        try:
            write_wav(samples, path)
            set_state("thinking")
            chime("stop")
            text = transcribe(path, stt or DEFAULTS["stt"])
        except subprocess.TimeoutExpired:
            # One slow transcription should cost you one question, not the
            # listener. Letting this escape kills the daemon, and systemd's
            # Restart=on-failure then brings the microphone back up on its
            # own, which is a strange way for an armed mic to behave.
            log("transcription timed out")
            return
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    finally:
        # Restore before Piper speaks, so Jarvis is not quieter than the
        # music it just ducked.
        end_playback_focus(focus)

    if not text:
        log("empty transcription")
        return
    publish_ui_text(REQUEST_FILE, text)
    respond(agent, voice, text, log_text, mode, "voice")


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def listen_forever(agent, voice, wake_path, wake_key, listen, log_text=False,
                   mode="safe", auto_disarm_seconds=0, stt=None):
    from openwakeword.model import Model

    model = Model(wakeword_model_paths=[wake_path])
    log(f"model loaded, listening for '{wake_key.rsplit('_v', 1)[0].replace('_', ' ')}'")
    clear_startup_error()  # we are up; any older refusal is stale

    mic = open_mic()
    clear_ui_text()
    set_state("idle")

    ambient = 200.0
    last_fire = 0.0
    armed_at = time.time()

    def refresh_arm_window():
        """Make the non-safe timeout an inactivity window, not a boot timer.

        A command that starts music should not make the next command
        impossible merely because the original physical arm happened five
        minutes earlier. This never affects Safe (which has no deadline), and
        it does not remove the Workspace/Privileged timeout: every new wake
        buys only the configured window from that interaction.
        """
        nonlocal armed_at
        if not auto_disarm_seconds:
            return
        armed_at = time.time()
        try:
            safefile.write_atomic(ARM_FILE,
                                  f"{armed_at + auto_disarm_seconds:.0f}")
        except OSError:
            pass

    try:
        while _running:
            # Workspace/Privileged never stay armed: explicit physical action
            # buys a short window, then the mic goes off by itself.
            if auto_disarm_seconds and time.time() - armed_at > auto_disarm_seconds:
                log(f"auto-disarm: {mode} window "
                    f"({auto_disarm_seconds:.0f}s) expired; stopping")
                audit({"mode": mode, "transcript_len": 0,
                       "tool_name": "daemon",
                       "args_summary_sanitized": "auto-disarm",
                       "approval_source": "timer", "result_code": "ok"})
                return
            samples = read_chunk(mic)
            if samples is None:
                log("mic stream ended, restarting")
                mic.kill()
                time.sleep(1)
                mic = open_mic()
                model.reset()
                continue

            level = rms(samples)
            # Slow rolling floor so the VAD adapts to the room.
            if level < ambient * 2:
                ambient = ambient * 0.995 + level * 0.005

            scores = model.predict(samples)
            score = float(scores.get(wake_key, 0.0))

            if score > listen["wake_threshold"] and time.time() - last_fire > listen["cooldown"]:
                log(f"wake word detected ({score:.2f})")
                note_wake()  # 2s avatar flash, distinct from recording
                clear_ui_text()
                handle_command(mic, ambient, agent, voice, listen, log_text,
                               mode, stt)
                refresh_arm_window()
                # Nothing drained the mic while we were thinking and speaking,
                # so the pipe holds seconds of stale audio (including our own
                # reply). Start a fresh stream rather than replay it.
                mic.kill()
                mic = open_mic()
                model.reset()
                last_fire = time.time()
                set_state("idle")
    finally:
        clear_ui_text()
        set_state("off")
        mic.kill()
        log("stopped")


def main():
    parser = argparse.ArgumentParser(
        prog="jarvis-listen",
        description="Wake-word voice assistant. With no arguments, listens forever.",
    )
    parser.add_argument("--config", default=CONFIG_PATH,
                        help=f"config file (default: {CONFIG_PATH})")
    parser.add_argument("--ask", metavar="TEXT",
                        help="skip the mic: send TEXT to the agent and speak the reply")
    parser.add_argument("--source", default="hotkey",
                        choices=("hotkey", "button", "voice"),
                        help="approval source recorded in the audit log for --ask "
                             "(default: hotkey; the mic path always logs voice)")
    parser.add_argument("--mode", choices=MODES,
                        help="override config mode for this invocation only")
    parser.add_argument("--agents", action="store_true",
                        help="list configured agents and exit")
    parser.add_argument("--check", action="store_true",
                        help="verify config and runtime dependencies, then exit")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.mode:
        cfg = dict(cfg)
        cfg["mode"] = args.mode
    mode = current_mode(cfg)
    listen = merge(DEFAULTS["listen"], cfg.get("listen", {}))

    if args.agents:
        for name in sorted(cfg.get("agents", {})):
            try:
                agent = Agent(name, cfg["agents"][name])
            except ValueError as exc:
                print(f"  {name:12} INVALID -- {exc}")
                continue
            found = "ok" if shutil.which(agent.executable) else "not installed"
            mark = "*" if name == cfg.get("agent") else " "
            kind = capability_label(agent)
            if agent.web:
                kind += " +web"
            print(f"{mark} {name:12} {found:15} {kind}")
        return 0

    try:
        agent = select_agent(cfg)
        voice = resolve_voice(cfg)
        wake_path, wake_key = resolve_wake_model(cfg)
        stt = resolve_stt(cfg)
    except SystemExit as exc:
        # A failed config must leave a reason on screen, not a silent mic
        # or a systemd restart loop. The panel reads this file; the unit's
        # start limits keep a bad config from cycling forever.
        note_startup_error(str(exc))
        raise
    log_text = bool(cfg.get("log_transcripts", False))
    # Fail fast on a bad [stt] section (typo'd engine/model), while --check
    # and the panel can still report it -- better than a listener that
    # transcribes with the wrong model or dies on restart.
    write_voxtype_config(stt)

    if args.check:
        ok = True
        for label, path in (("voice", voice), ("wake model", wake_path)):
            exists = os.path.exists(path)
            ok &= exists
            print(f"{'ok ' if exists else 'MISSING'}  {label}: {path}")
        for cmd in ("pw-record", "pw-play", "voxtype", agent.executable):
            found = shutil.which(cmd)
            ok &= bool(found)
            print(f"{'ok ' if found else 'MISSING'}  {cmd}: {found or '-'}")
        fb = f" +cloud-fallback:{stt['fallback_engine']}" \
            if stt.get("cloud_fallback") else ""
        print(f"ok   stt: {stt['engine']}/{stt['model']} "
              f"({stt['language']}){fb}")
        vox_ok = os.path.exists(JARVIS_VOX_CONFIG)
        ok &= vox_ok
        print(f"{'ok ' if vox_ok else 'MISSING'}  voxtype config: "
              f"{JARVIS_VOX_CONFIG}")
        print(f"mode: {mode}")
        print(f"{capability_label(agent)}: agent '{agent.name}'")
        # Unknown CLI posture is FAIL in every mode -- say so and fail closed.
        for problem in check_mode_invariants(mode, agent):
            print(f"FAIL  {problem}")
            ok = False
        if tool_posture(agent.executable, agent.command) == TOOLS_UNKNOWN:
            print(f"FAIL  agent '{agent.name}': tools not verified -- "
                  "run the canary before trusting it "
                  "(--ask with a known string in a disposable dir)")
            ok = False
        if not ok:
            print("check: FAIL")
        else:
            clear_startup_error()  # config validates: old refusals are stale
        return 0 if ok else 1

    if args.ask:
        respond(agent, voice, args.ask, log_text, mode, args.source)
        return 0

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    deadline = workspace_timeout(cfg, mode) if mode != "safe" else 0
    listen_forever(agent, voice, wake_path, wake_key, listen, log_text,
                   mode, deadline, stt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
