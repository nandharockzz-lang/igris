#!/usr/bin/env python3
"""Versioned local-action contract shared by recognizer, broker, UI, tests.

Every deterministic desktop action Jarvis can take is one of these specs:
a fixed intent grammar (action + normalized args), strict validation, a
confirmation requirement, and a direct allowlisted argv -- no shell, no
free-form commands, no voice shell, anywhere in the path.

Request  {"v": 1, "id": str, "action": str, "args": {...},
          "needs_confirm": bool, "source": "voice"|"agent"|"button"}
Result   {"v": 1, "id": str, "ok": bool, "pending": bool,
          "error": str, "result": str}
Errors and results are redacted by construction: free speech (notification
text, queries) never appears in them, only token kinds and lengths.

needs_confirm actions never execute on voice authority alone: the broker
writes a pending file and exits 2, the card shows Confirm/Deny, and only
`jarvis-open confirm` (a tap on screen) executes. --ask/button invocations
are supervised acts at a keyboard and execute directly.
"""

import json
import os
import re
import time

CONTRACT_VERSION = 1

HOME = os.path.expanduser("~")


def _state_root():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return runtime
    return os.environ.get("XDG_STATE_HOME") or os.path.join(
        HOME, ".local", "state")


STATE_DIR = os.path.join(_state_root(), "jarvis")
PENDING_FILE = os.path.join(STATE_DIR, "pending-action")
PENDING_TTL_SECONDS = 120
PENDING_LIMIT_BYTES = 4096

# --- fixed intent grammar -------------------------------------------------
# Every value regex is anchored; nothing free-form reaches an argv.
APP_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}$")
URL_RE = re.compile(r"^https?://[^\s\"'\\<>]+$")
VOLUME_RE = re.compile(r"^(\d{1,3}|up|down|mute|unmute)$")
BRIGHTNESS_RE = re.compile(r"^(\d{1,3}|up|down)$")
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,31}$")
WORKSPACE_NUM_RE = re.compile(r"^([1-9]|10)$")
WORKSPACE_REL_RE = re.compile(r"^(next|previous)$")
MUTE_RE = re.compile(r"^(on|off|toggle|mute|unmute)$")
MEDIA_RE = re.compile(r"^(play|pause|toggle|next|prev|stop|quit)$")
MUSIC_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +.'\-]{0,79}$")
MYMUSIC_RE = re.compile(
    r"^(liked|favourites|favorites|watchlater|watch later"
    r"|playlist [A-Za-z0-9 _.'\-]{1,40})$")
FOCUS_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,60}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]+$")
NOTIFY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,!?'\"()\-]{0,199}$")
WIFI_RE = re.compile(r"^(on|off)$")

# action -> {args: {name: regex}, confirm: bool, describe: template}.
# `describe` builds the on-screen confirmation prompt from validated args
# only (no free speech passes through except notify text, which is shown
# verbatim because the user must see what they are confirming -- it is
# never journaled).
ACTIONS = {
    "app":          {"args": {"query": APP_QUERY_RE}, "confirm": False,
                     "describe": "Open {query}"},
    "url":          {"args": {"url": URL_RE}, "confirm": False,
                     "describe": "Open web page"},
    "volume":       {"args": {"value": VOLUME_RE}, "confirm": False,
                     "describe": "Set volume {value}"},
    "brightness":   {"args": {"value": BRIGHTNESS_RE}, "confirm": False,
                     "describe": "Set brightness {value}"},
    "workspace":    {"args": {"target": WORKSPACE_RE}, "confirm": False,
                     "describe": "Switch to workspace {target}"},
    "mute":         {"args": {"value": MUTE_RE}, "confirm": False,
                     "describe": "Mute {value}"},
    "media":        {"args": {"value": MEDIA_RE}, "confirm": False,
                     "describe": "Media {value}"},
    "music":        {"args": {"query": MUSIC_QUERY_RE}, "confirm": False,
                     "describe": "Play music"},
    "mymusic":      {"args": {"spec": MYMUSIC_RE}, "confirm": False,
                     "describe": "Play library"},
    "focus-window": {"args": {"query": FOCUS_QUERY_RE}, "confirm": False,
                     "describe": "Focus {query}"},
    "move-window":  {"args": {"query": FOCUS_QUERY_RE,
                              "target": WORKSPACE_RE}, "confirm": False,
                     "describe": "Move {query} to workspace {target}"},
    "fullscreen":   {"args": {}, "confirm": False,
                     "describe": "Toggle fullscreen"},
    "lock":         {"args": {}, "confirm": False,
                     "describe": "Lock the screen"},
    "screenshot":   {"args": {"mode": re.compile(r"^(full|window)$")},
                     "confirm": False,
                     "describe": "Take a screenshot"},
    "notify":       {"args": {"text": NOTIFY_RE}, "confirm": False,
                     "describe": "Show notification: {text}"},
    "close-window": {"args": {}, "confirm": True,
                     "describe": "Close the focused window"},
    "logout":       {"args": {}, "confirm": True,
                     "describe": "Log out of the session"},
    "reboot":       {"args": {}, "confirm": True,
                     "describe": "Reboot the machine"},
    "poweroff":     {"args": {}, "confirm": True,
                     "describe": "Shut down the machine"},
    "wifi":         {"args": {"value": WIFI_RE}, "confirm": True,
                     "describe": "Turn Wi-Fi {value}"},
}


def validate_args(action, args):
    """Normalized args dict, or None when the action/args are invalid."""
    spec = ACTIONS.get(action)
    if spec is None or not isinstance(args, dict):
        return None
    out = {}
    for name, pattern in spec["args"].items():
        value = args.get(name)
        if not isinstance(value, str) or not pattern.match(value):
            return None
        out[name] = value
    if action == "volume" and out.get("value", "").isdigit():
        if not 0 <= int(out["value"]) <= 100:
            return None
    if action == "brightness" and out.get("value", "").isdigit():
        if not 1 <= int(out["value"]) <= 100:
            return None
    return out


def build_request(action, args, source="voice", req_id=None):
    """A versioned request, or None when invalid. needs_confirm is derived
    from the spec -- callers cannot talk themselves out of confirmation."""
    norm = validate_args(action, args)
    if norm is None:
        return None
    if req_id is None:
        req_id = f"{int(time.time())}-{os.getpid()}"
    return {"v": CONTRACT_VERSION, "id": str(req_id)[:64],
            "action": action, "args": norm,
            "needs_confirm": bool(ACTIONS[action]["confirm"]),
            "source": source if source in ("voice", "agent", "button")
            else "voice"}


def describe(request):
    """On-screen confirmation prompt from validated args only."""
    spec = ACTIONS.get(request.get("action", ""), {})
    template = spec.get("describe", "Run action")
    try:
        return template.format(**request.get("args", {}))
    except (KeyError, IndexError):
        return template


def result(req_id, ok, pending=False, error="", result=""):
    """A versioned result. error/result carry kinds, never free speech."""
    return {"v": CONTRACT_VERSION, "id": str(req_id)[:64],
            "ok": bool(ok), "pending": bool(pending),
            "error": str(error)[:200], "result": str(result)[:200]}


def write_pending(request):
    """Stage a needs_confirm request for on-screen approval."""
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    except OSError:
        return False
    payload = {"v": CONTRACT_VERSION, "id": request["id"],
               "action": request["action"], "args": request["args"],
               "created": int(time.time()),
               "description": describe(request)}
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".jarvis.")
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
            if os.fstat(fh.fileno()).st_size > PENDING_LIMIT_BYTES:
                raise OSError("pending request too large")
        os.replace(tmp, PENDING_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass
        return False
    return True


def read_pending():
    """The staged request, or None when absent, corrupt or expired."""
    try:
        with open(PENDING_FILE) as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != CONTRACT_VERSION:
        return None
    try:
        age = time.time() - int(payload.get("created", 0))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > PENDING_TTL_SECONDS:
        return None
    request = build_request(payload.get("action", ""),
                            payload.get("args", {}),
                            source="button",
                            req_id=payload.get("id", ""))
    if request is None or not request["needs_confirm"]:
        return None
    request["description"] = str(payload.get("description", ""))[:200]
    return request


def clear_pending():
    try:
        os.unlink(PENDING_FILE)
    except OSError:
        pass
