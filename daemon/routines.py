"""Local routine-intent matching (skip the LLM for deterministic verbs)."""

from __future__ import annotations

import re

import actions

# Same grammar jarvis-open / directive path use for music queries.
MUSIC_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +.'\-]{0,79}$")

# Canned confirmations for the routine fast path (generic wording only --
# song names and other speech never go back out verbatim from here).
ROUTINE_CONFIRM = {
    "app": "Opening it now.", "url": "Opening it now.",
    "volume": "Volume adjusted.", "brightness": "Brightness adjusted.",
    "workspace": "Switching workspace.", "mute": "Done.",
    "media": "Done.", "music": "Playing it now.",
    "mymusic": "Playing it now.", "focus-window": "Focusing it now.",
    "move-window": "Moving it now.", "fullscreen": "Toggling fullscreen.",
    "lock": "Locking the screen.", "screenshot": "Screenshot saved.",
    "notify": "Noted.",
}
# Spoken when a needs_confirm verb stages on-screen approval (nothing ran).
PENDING_CONFIRM = ("That one needs a tap to confirm -- "
                   "check the card for Confirm or Deny.")


def _command_words(text):
    """Lowercased, whitespace-collapsed, minus leading/trailing politeness."""
    t = " ".join(text.strip().lower().split())
    t = re.sub(r"^(please|hey|hi|ok|so)[, ]+", "", t)
    t = re.sub(r"[, ]+(please|thanks|thank you)$", "", t)
    return t


def match_routine_intent(text):
    """(kind, value) for routine commands, else None.

    Deliberately verb-first and conservative: questions ("can you play
    something?"), chatter and bare ambiguous words ("stop") never match and
    fall through to the model. Everything returned still passes through
    run_directive's mode/arm/allowlist enforcement -- this skips the LLM
    round-trip, never a check.
    """
    t = _command_words(text)
    if not t:
        return None
    m = re.match(r"^play my\s+(liked(\s+(songs?|music|playlist))?|"
                 r"favourites?|favorites?)\s*$", t)
    if m:
        return ("mymusic", "liked")
    if re.match(r"^play my\s+watch\s?later\s*$", t):
        return ("mymusic", "watchlater")
    m = re.match(r"^play (?:my )?playlist\s+([a-z0-9 _.'\-]{1,40})$", t)
    if m:
        return ("mymusic", f"playlist {m.group(1).strip()}")
    # "player X" is a common STT of "play a/the X".
    m = re.match(r"^(?:play|put on|player)\s+(.{1,80})$", t)
    if m:
        query = " ".join(m.group(1).split())
        if MUSIC_QUERY_RE.match(query):
            return ("music", query)
        return None
    media = [
        (r"^pause(?: (?:the )?(?:music|song|video|playback))?$", "pause"),
        (r"^(?:resume|unpause)(?: (?:the )?(?:music|song|video|playback))?$",
         "play"),
        (r"^(?:next|skip)(?: (?:the )?(?:music|song|video|track))?$", "next"),
        (r"^(?:previous|last|back)(?: (?:the )?(?:music|song|video|track))?$",
         "prev"),
        (r"^stop the (?:music|song|video|playback)$", "stop"),
        (r"^quit the (?:music|player|song)$", "quit"),
        (r"^close the (?:music|player)$", "quit"),
    ]
    for pattern, value in media:
        if re.match(pattern, t):
            return ("media", value)
    volume = [
        (r"^(?:volume )?(up|louder)$", "up"),
        (r"^(?:volume )?(down|quieter|softer)$", "down"),
        (r"^(?:volume )?(mute|silence)(?: the (?:music|sound|volume|it))?$",
         "mute"),
        (r"^unmute(?: the (?:music|sound|volume|it))?$", "unmute"),
        (r"^turn (?:it |the volume |the music |the sound )?(up|down)$", None),
        (r"^volume (\d{1,3})$", None),
        (r"^(?:max|maximum|full) volume$", "100"),
    ]
    for pattern, value in volume:
        m = re.match(pattern, t)
        if m:
            if value is not None:
                return ("volume", value)
            word = m.group(1)
            if word in ("up", "down"):
                return ("volume", word)
            if word.isdigit() and 0 <= int(word) <= 100:
                return ("volume", str(int(word)))
            return None
    if t == "open youtube":
        return ("url", "https://www.youtube.com/")
    # Workspaces before generic "open APP": "open workspace 5" is not an app.
    # Names here are a single token (no spaces) so "go to workspace 5 and
    # open chromium" cannot be swallowed as a workspace named
    # "5 and open chromium" -- Hyprland then errors Bad workspace.
    _ws = r"([a-z0-9][a-z0-9_.-]{0,31})"
    m = re.match(rf"^(?:switch to|go to|move to|open|show) workspace {_ws}$",
                 t)
    if m:
        return ("workspace", m.group(1).strip())
    m = re.match(rf"^workspace {_ws}$", t)
    if m:
        return ("workspace", m.group(1).strip())
    m = re.match(r"^(?:open|launch|start|run)(?: the)? "
                 r"([a-z0-9][a-z0-9 ._\-]{0,30})$", t)
    if m and not m.group(1).startswith("workspace"):
        return ("app", m.group(1).strip())
    if re.match(r"^(?:go to |switch to |move to )?(?:the )?"
                r"(next|previous) workspace$", t):
        which = "next" if "next" in t.split() else "previous"
        return ("workspace", which)
    # Focus / move windows. Focus refuses bare "workspace ..." so the two
    # never collide; move needs an explicit workspace target.
    m = re.match(r"^(?:focus|switch to|bring to front|show) "
                 r"([a-z0-9][a-z0-9 ._\-+]{0,40})$", t)
    if m and not m.group(1).startswith("workspace"):
        return ("focus-window", m.group(1).strip())
    m = re.match(r"^move (?:the |this |current )?(.+?) to workspace "
                 r"([a-z0-9][a-z0-9 _.\-]{0,31}|next|previous)$", t)
    if m:
        query = " ".join(m.group(1).split())
        if query in ("window", "this window", "current window", "it"):
            query = "this"
        if actions.FOCUS_QUERY_RE.match(query):
            return ("move-window", [query, m.group(2).strip()])
        return None
    if re.match(r"^(?:toggle |make (?:it|this) )?fullscreen$", t):
        return ("fullscreen", "")
    if re.match(r"^lock (?:the )?screen$", t):
        return ("lock", "")
    if re.match(r"^take a screenshot$", t) or t == "screenshot":
        return ("screenshot", "full")
    if re.match(r"^screenshot (?:this|the|current) window$", t):
        return ("screenshot", "window")
    m = re.match(r"^remind me to ([a-z0-9][a-z0-9 .,!?\'\"()\-]{0,120})$",
                 t)
    if m:
        text = " ".join(m.group(1).split())
        if actions.NOTIFY_RE.match(text):
            return ("notify", text)
        return None
    # Close: the focused window only. (Music-player closes stay media quit.)
    if re.match(r"^close (?:this|the current|the) window$", t):
        return ("close-window", "")
    m = re.match(r"^(?:log|sign) (?:me )?out$", t)
    if m:
        return ("logout", "")
    if re.match(r"^reboot(?: the (?:machine|computer|system))?$", t):
        return ("reboot", "")
    if re.match(r"^(?:power off|shut down|shutdown)"
                r"(?: the (?:machine|computer|system))?$", t):
        return ("poweroff", "")
    m = re.match(r"^(?:turn )?wi-?fi (on|off)$", t)
    if m:
        return ("wifi", m.group(1))
    return None


def match_routine_intents(text):
    """One or more routine intents when the utterance is only those commands.

    Compound speech ("go to workspace 5 and open chromium") is split on
    and/then. Every clause must match or this returns None and the model
    handles the whole sentence. A single-clause match still wins first.
    """
    t = _command_words(text)
    if not t:
        return None
    one = match_routine_intent(t)
    if one is not None:
        return [one]
    parts = re.split(r"\s+(?:and then|then|and)\s+", t)
    if len(parts) < 2 or len(parts) > 4:
        return None
    intents = []
    for part in parts:
        got = match_routine_intent(part)
        if got is None:
            return None
        intents.append(got)
    return intents

