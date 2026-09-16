#!/usr/bin/env python3
"""STT benchmark: whisper base.en vs small.en (and parakeet when available).

Corpus: tools/stt-corpus.txt, one reference phrase per line. Synthesizes
each phrase with piper, resamples to 16kHz mono, then transcribes with
`voxtype transcribe -c <model-config>` per candidate and scores WER plus
wall-clock latency.

Usage:
    tools/stt-bench.py synthesize   # piper -> /tmp/stt-corpus/*.wav
    tools/stt-bench.py bench        # transcribe + score, JSON to stdout

Nothing here touches the user's live voxtype config: candidates run with
temporary -c config files copied from ~/.config/voxtype/config.toml.
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "stt-corpus.txt")
WAV_DIR = "/tmp/stt-corpus"
JARVIS_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "jarvis")
VENV_PY = os.path.join(JARVIS_DIR, "venv", "bin", "python")
VOICE = os.path.join(JARVIS_DIR, "voices", "en_US-amy-medium.onnx")
USER_CONFIG = os.path.join(os.path.expanduser("~"), ".config", "voxtype", "config.toml")

# Same bias Jarvis writes into its owned voxtype config. The bench must
# use this, not the user's ~/.config/voxtype/config.toml prompt.
JARVIS_VOCABULARY = (
    "play pause open launch start run terminal chromium firefox "
    "browser files workspace next previous volume mute unmute "
    "brightness screenshot lock music youtube discord settings "
    "calculator close window jarvis foot"
)

CANDIDATES = [
    {"name": "whisper-base.en", "engine": "whisper", "model": "base.en"},
]


def phrases():
    with open(CORPUS) as f:
        return [ln.strip() for ln in f if ln.strip()]


def norm(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def wer(ref, hyp):
    r, h = norm(ref).split(), norm(hyp).split()
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1] / len(r)


def synthesize():
    os.makedirs(WAV_DIR, exist_ok=True)
    for i, phrase in enumerate(phrases()):
        raw = os.path.join(WAV_DIR, f"cmd{i:02d}-raw.wav")
        out = os.path.join(WAV_DIR, f"cmd{i:02d}.wav")
        p = subprocess.run(
            [VENV_PY, "-m", "piper", "-m", VOICE, "-f", raw],
            input=phrase.encode(), capture_output=True, timeout=120)
        if p.returncode != 0 or not os.path.exists(raw):
            print(f"piper failed for {phrase!r}: {p.stderr.decode()[-300:]}",
                  file=sys.stderr)
            continue
        # Resample to 16kHz mono for voxtype transcribe.
        import numpy as np
        from scipy.io import wavfile
        from scipy.signal import resample_poly
        sr, data = wavfile.read(raw)
        if data.ndim > 1:
            data = data.mean(axis=1).astype(data.dtype)
        if sr != 16000:
            data = resample_poly(data, 16000, sr).astype(np.int16)
        wavfile.write(out, 16000, data)
        os.remove(raw)
        print(f"wrote {out}")


def candidate_config(cand):
    text = (f"[{cand['engine']}]\n"
            f"model = \"{cand['model']}\"\n"
            f"language = \"en\"\n"
            f"initial_prompt = \"{JARVIS_VOCABULARY}\"\n")
    path = os.path.join(WAV_DIR, f"config-{cand['name']}.toml")
    with open(path, "w") as f:
        f.write(text)
    return path


def transcribe(path, cand, cfg):
    cmd = ["voxtype", "-c", cfg, "transcribe",
           "--engine", cand["engine"], path]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, None
    dt = time.monotonic() - t0
    if p.returncode != 0:
        return None, dt
    # voxtype logs to stdout alongside the transcript; take the tail,
    # mirroring jarvis-listen's transcribe().
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return (lines[-1] if lines else ""), dt


def bench(prefix="cmd"):
    refs = phrases()
    wavs = [os.path.join(WAV_DIR, f"{prefix}{i:02d}.wav")
            for i in range(len(refs))]
    missing = [w for w in wavs if not os.path.exists(w)]
    if missing:
        sys.exit(f"missing wavs (run synthesize first): {missing[:3]}")
    results = []
    for cand in CANDIDATES:
        cfg = candidate_config(cand)
        for ref, wav in zip(refs, wavs):
            hyp, dt = transcribe(wav, cand, cfg)
            results.append({
                "model": cand["name"], "ref": ref,
                "hyp": hyp, "wer": None if hyp is None else wer(ref, hyp),
                "latency_s": dt,
            })
    print(json.dumps(results, indent=1))
    for cand in CANDIDATES:
        rows = [r for r in results if r["model"] == cand["name"]
                and r["wer"] is not None]
        ok = [r for r in rows if r["latency_s"] is not None]
        mw = sum(r["wer"] for r in rows) / len(rows) if rows else float("nan")
        ml = sum(r["latency_s"] for r in ok) / len(ok) if ok else float("nan")
        print(f"{cand['name']}: mean WER {mw:.3f}  "
              f"mean latency {ml:.1f}s  n={len(rows)}/{len(refs)}",
              file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("synthesize", "bench",
                                                 "bench-mic"):
        sys.exit("usage: stt-bench.py [synthesize|bench|bench-mic]")
    {"synthesize": synthesize, "bench": lambda: bench("cmd"),
     "bench-mic": lambda: bench("mic")}[sys.argv[1]]()
