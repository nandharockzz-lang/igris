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

# Next to this file, in the repo and in ~/.local/share/jarvis alike.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safefile

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

DEFAULTS = {
    "agent": "claude",
    "wake_word": "hey_jarvis",
    "voice": "en_US-amy-medium.onnx",
    # What you say near an open microphone can carry secrets, and journald
    # persists what we print. Off means the journal records sizes and
    # outcomes, never the words.
    "log_transcripts": False,
    # Explicit mode, not a prompt promise. Safe is the only mode allowed for
    # always-on wake-word listening: agent=opencode-voice (all tools denied
    # in its agent file) or a CLI-verified answer-only preset, actions=false.
    # Workspace ("Basic" in the UI) is an explicit physical action only
    # (hotkey / push-to-talk --ask / widget arm) with auto-disarm; it unlocks
    # the deterministic broker verbs and nothing else. Privileged ("Full")
    # adds sandboxed full-tools behind the jarvis-agent-run wrapper plus a
    # button/keyboard confirmation for anything destructive -- voice never
    # authorizes that. Basic gets the longer window because its risk surface
    # is broker verbs only; Full keeps the short one.
    "mode": "safe",
    "workspace": {
        "auto_disarm_seconds": 1800,
        "full_disarm_seconds": 300,
    },
    "listen": {
        "wake_threshold": 0.5,
        "silence_tail": 1.2,
        "min_speech": 0.4,
        "max_command": 15.0,
        "cooldown": 1.0,
    },
    "draggable_avatar_enabled": True,
    "agents": {
        "claude": {
            # No {prompt} in argv: the transcript is fed to `claude -p` on
            # stdin, where it is not readable out of the process list.
            #
            # The tool flags are the deny boundary, and both are load-bearing.
            # Passing no tool flags at all is *not* answer-only: `claude -p`
            # still exposes its built-in read tools, and still loads whatever
            # MCP servers the user's own configuration defines, so a sentence
            # spoken near the mic could read local files through an agent we
            # meant to be a text box. `--tools ""` is the CLI's empty built-in
            # allowlist, and `--strict-mcp-config` with no accompanying
            # --mcp-config loads no MCP servers, so an inherited user or
            # project config cannot put tools back. With actions = true the
            # daemon parses a strictly validated <<jarvis:open-...>> directive
            # out of the reply text and execs the jarvis-open broker itself,
            # so there is still never a tool grant to aim at.
            "command": ["claude", "-p",
                        "--tools", "", "--strict-mcp-config",
                        "--append-system-prompt", "{system}"],
            # Off unless the config says otherwise. Letting a sentence spoken
            # near the mic open apps and URLs is a decision the person
            # installing this should make on purpose, not one they inherit
            # from a default. It also means these DEFAULTS stay safe as a
            # fallback: an unreadable config drops back to here, and dropping
            # back should never quietly grant more than was granted before.
            "actions": False,
        },
    },
}

# The voice-style half of the system prompt. Always sent.
STYLE_PROMPT = (
    "You are a voice assistant. Your reply will be read aloud by a "
    "text-to-speech engine, so answer in at most three short sentences of "
    "plain spoken English. No markdown, no lists, no code blocks, no URLs."
)

# Sent whenever the invocation grants the CLI no tools, which is what the
# shipped presets do. Without it the model does not know its tools are gone:
# it answers a "read this file" with tool-call syntax, which is then read
# aloud as punctuation soup. Telling it plainly gets a plain refusal instead.
# Left off an invocation the user has given tools to, where it would be false.
NO_TOOLS_PROMPT = (
    "You have no tools in this conversation. You cannot read or write files, "
    "run commands, or browse. If answering would need one, say so in a short "
    "spoken sentence. Never write out a tool call or any other markup."
)

# The actions half. Only sent to agents configured with actions = true. The
# agent is never given a tool or a shell: it asks for an action by ending its
# reply with one directive line, and the daemon decides whether anything
# happens. See extract_directive/run_directive below. Workspace mode only:
# Safe mode refuses to start with actions on (see check_mode_invariants).
ACTIONS_PROMPT = (
    "You cannot run commands, but you can ask Jarvis to open things or to "
    "change a desktop setting. To open an installed app, add a line at the "
    "end of your reply of exactly this form: <<jarvis:open-app NAME>>. "
    "To open a web page in the browser: <<jarvis:open-url URL>> (http or "
    "https only). For desktop settings, one of: <<jarvis:volume 0-100|up|"
    "down|mute|unmute>>, <<jarvis:brightness 1-100|up|down>>, "
    "<<jarvis:workspace NAME>>, <<jarvis:mute on|off|toggle>>. "
    "To play music or a video, name it with words joined by plus signs: "
    "<<jarvis:music song+name>> opens the top YouTube result straight "
    "away. For your own library: <<jarvis:mymusic liked>>, "
    "<<jarvis:mymusic watchlater>>, or <<jarvis:mymusic playlist NAME>> "
    "for a playlist from your config (it opens in your logged-in browser). "
    "To pause, resume or skip what is already playing in any media "
    "player: <<jarvis:media toggle|play|pause|next|prev>>. To close the "
    "mini player: <<jarvis:media quit>>. "
    "You cannot browse results, click anything, or control a page: say "
    "so out loud if asked. At most one "
    "such line per reply. The line is stripped before your reply is spoken, "
    "so also say in your reply what you are doing. If asked to do anything "
    "else to the machine, say out loud that you cannot."
)

# The web half. Only sent to agents configured with a web_command. The first
# call still runs with no tools; asking to search hands the exchange to a
# second, search-capable invocation whose reply is treated as tainted -- see
# run_search below.
WEB_PROMPT = (
    "If answering needs current information from the web, reply with only "
    "this line and nothing else: <<jarvis:search WHAT TO LOOK UP>>. Jarvis "
    "will run one web-enabled round and speak its answer. Do not search for "
    "things you already know, and never combine a search line with an open "
    "line."
)

# System prompt for the web-enabled second call. Deliberately excludes
# ACTIONS_PROMPT and WEB_PROMPT: this call can read the open web, so it gets
# no way to ask for anything -- no opens, no further searches.
WEB_TURN_PROMPT = (
    "Use your web search tool to find what the question needs, then answer "
    "from what you found. Say plainly if the search settles nothing. Do not "
    "read URLs aloud."
)

def _state_root():
    """Where the pipeline-state file lives.

    XDG_RUNTIME_DIR is per-user and mode 0700, so it is the right home. The
    old fallback was tempfile.gettempdir() -- i.e. a predictable path inside a
    world-writable /tmp, where another local user could pre-plant `state` as a
    FIFO (blocking the bar widget's reader, which polls every second) or
    `state.tmp` as a symlink (redirecting our write onto one of this user's
    own files). Fall back to a directory only this user can write instead.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return runtime
    return os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state")


STATE_DIR = os.path.join(_state_root(), "jarvis")
STATE_FILE = os.path.join(STATE_DIR, "state")
# Visible mode + auto-disarm deadline for the widget/panel. STATE_FILE keeps
# its single pipeline token for compatibility; these sit beside it. All are
# daemon-published numbers/tokens only -- never transcripts, audio, or args:
#   mode       safe|workspace|privileged (authoritative; panel reflects it)
#   armed_until  epoch seconds when a non-safe window ends
#   wake       epoch seconds of the last wake-word detection (2s flash)
#   level      "0-100 epoch" playback amplitude (10Hz while speaking)
#   mic        12 gated mic-amplitude buckets + epoch (10Hz while listening)
#   tool       "<category> <epoch>" of the last broker/search attempt
MODE_FILE = os.path.join(STATE_DIR, "mode")
ARM_FILE = os.path.join(STATE_DIR, "armed_until")
WAKE_FILE = os.path.join(STATE_DIR, "wake")
LEVEL_FILE = os.path.join(STATE_DIR, "level")
MIC_FILE = os.path.join(STATE_DIR, "mic")
TOOL_FILE = os.path.join(STATE_DIR, "tool")
# Current exchange for the persistent Jarvis console. These files live in the
# per-user runtime directory, are bounded, and are cleared on a new turn or
# daemon stop. They are UI state, not audit/log storage.
REQUEST_FILE = os.path.join(STATE_DIR, "request")
RESPONSE_FILE = os.path.join(STATE_DIR, "response")
UI_TEXT_LIMIT = 1200

# Redacted audit log. Deliberately NOT the journal and NOT under
# XDG_RUNTIME_DIR (tmpfs, cleared on reboot): it must survive to prove
# containment later. One JSON object per line, redacted by construction --
# sizes and kinds, never transcripts, secrets or full URLs.
def _audit_path():
    base = (os.environ.get("XDG_STATE_HOME")
            or os.path.join(HOME, ".local", "state"))
    return os.path.join(base, "jarvis", "audit.log")

AUDIT_FILE = _audit_path()

# Explicit modes. Safe is the only one allowed for always-on listening.
MODES = ("safe", "workspace", "privileged")

# The agent CLI's working directory. Deliberately NOT $HOME as hygiene, so a
# relative path lands in an empty private directory rather than among the
# user's files -- but this is NOT a security boundary and is never claimed
# as one. Confinement comes from tool denial (safe), the broker allowlist
# (workspace), and the jarvis-agent-run mount container (privileged), all
# enforced outside the agent. 0700 and under XDG_RUNTIME_DIR where available.
AGENT_CWD = os.path.join(STATE_DIR, "agent-cwd")

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
            key, DEFAULTS["workspace"][key]))
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


def check_mode_invariants(mode, agent):
    """Enforcement, not prompt promises. Returns [error strings].

    Fail-closed in every mode: the daemon's own agent CLI must be positively
    verified tool-free (TOOLS_DENIED), never merely "not known to grant".
    Unknown posture is FAIL, not safe, everywhere -- there are no blessed
    exceptions.
    """
    errors = []
    if mode not in MODES:
        return [f"unknown mode '{mode}'; want one of {', '.join(MODES)}"]
    posture = tool_posture(agent.executable, agent.command)
    if posture == TOOLS_UNKNOWN:
        errors.append(
            f"refuses agent '{agent.name}': tools not verified "
            f"(unknown CLI posture is FAIL in {mode} mode, not safe)")
    if posture == TOOLS_GRANTED:
        errors.append(
            f"refuses agent '{agent.name}': CLI tools granted")
    if mode == "safe":
        # Always-on mic: no broker actions at all, no web-exfil channel.
        if agent.actions:
            errors.append(
                f"safe mode refuses actions=true on '{agent.name}': "
                "workspace mode or later only")
        if agent.web and any("WebFetch" in p or "Bash" in p
                             or "--dangerously" in p
                             for p in agent.web_command or []):
            errors.append("safe mode refuses a web_command with "
                          "WebFetch/Bash")
    elif mode == "workspace":
        # Broker verbs only, still never a CLI tool grant.
        if grants_tools(agent.command):
            errors.append(
                f"workspace mode refuses agent '{agent.name}': CLI tool "
                "flags in `command` (broker verbs only, no Bash)")
    elif mode == "privileged":
        # Full-tools only ever behind the jarvis-agent-run sandbox wrapper;
        # voice never authorizes destructive ops (button/keyboard only).
        # The daemon cannot verify the wrapper from argv alone, so this mode
        # always logs loudly and requires the sandbox to exist.
        if jarvis_agent_run_path() is None:
            errors.append("privileged mode needs the jarvis-agent-run "
                          "sandbox wrapper installed")
    return errors


def jarvis_agent_run_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(JARVIS_BIN, "jarvis-agent-run"),
                      os.path.join(here, "jarvis-agent-run")):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def merge(base, override):
    """Recursive dict merge; override wins. Used to layer config over DEFAULTS."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


class Agent:
    """One configured agent CLI: how to invoke it and what it's allowed to do."""

    def __init__(self, name, spec):
        command = spec.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError(f"agent '{name}': 'command' must be a non-empty array")
        if not all(isinstance(part, str) for part in command):
            raise ValueError(f"agent '{name}': every 'command' entry must be a string")

        self.name = name
        self.command = command
        self.actions = bool(spec.get("actions", False))

        # A TOML string here would iterate as characters, and an empty prefix
        # matches every line -- either way clean_reply would quietly eat the
        # whole reply. A non-string would TypeError mid-exchange instead of
        # at startup. Refuse all of it here, loudly.
        prefixes = spec.get("strip_prefixes", [])
        if isinstance(prefixes, str) or not isinstance(prefixes, list) \
                or not all(isinstance(p, str) and p for p in prefixes):
            raise ValueError(f"agent '{name}': 'strip_prefixes' must be an "
                             "array of non-empty strings")
        self.strip_prefixes = tuple(prefixes)

        try:
            self.timeout = float(spec.get("timeout", 180))
        except (TypeError, ValueError):
            raise ValueError(f"agent '{name}': 'timeout' must be a number")
        if not 0 < self.timeout <= 3600:
            raise ValueError(f"agent '{name}': 'timeout' must be between "
                             "0 and 3600 seconds")

        # A second argv for the web-enabled round of a search exchange --
        # the one place a (CLI-enforced, read-only) search tool grant
        # belongs. Its presence is what enables search for this agent.
        web_command = spec.get("web_command")
        if web_command is not None:
            if not isinstance(web_command, list) or not web_command \
                    or not all(isinstance(p, str) for p in web_command):
                raise ValueError(f"agent '{name}': 'web_command' must be a "
                                 "non-empty array of strings")
        self.web_command = web_command
        self.web = web_command is not None
        self.web_uses_outfile = any("{outfile}" in part
                                    for part in web_command or [])
        # A {outfile} anywhere in argv means the reply is written to a file
        # rather than printed -- the escape hatch for CLIs whose stdout is a
        # progress log.
        self.uses_outfile = any("{outfile}" in part for part in command)

    @property
    def system_prompt(self):
        parts = [STYLE_PROMPT]
        if not grants_tools(self.command):
            parts.append(NO_TOOLS_PROMPT)
        if self.actions:
            parts.append(ACTIONS_PROMPT)
        if self.web:
            parts.append(WEB_PROMPT)
        return "\n\n".join(parts)

    @property
    def executable(self):
        return self.command[0]

    def build_invocation(self, prompt, outfile, system_extra="", web=False):
        """(argv, stdin_payload) for one question.

        The transcript only lands in argv if the command template asks for it
        with {prompt} -- argv is readable by every process on the machine, so
        the presets don't. Without {prompt}, the transcript is fed on stdin;
        a template that names neither {prompt} nor {system} gets both there,
        system prompt first, for CLIs with no system-prompt flag.

        web=True builds the search-capable second call: web_command's argv,
        and a system prompt that offers no directives of any kind.
        """
        if web:
            command = self.web_command
            system = STYLE_PROMPT + "\n\n" + WEB_TURN_PROMPT
        else:
            command = self.command
            system = self.system_prompt
        if system_extra:
            system += "\n\n" + system_extra
        fields = {
            "{prompt}": prompt,
            "{system}": system,
            "{outfile}": outfile or "",
        }
        used = set()
        argv = []
        for part in command:
            for token, value in fields.items():
                if token in part:
                    used.add(token)
                    part = part.replace(token, value)
            argv.append(part)
        if "{prompt}" in used:
            return argv, None
        if "{system}" in used:
            return argv, prompt
        return argv, system + "\n\n" + prompt


def agent_cwd():
    """An empty private directory to run the agent CLI in, falling back to /.

    Never $HOME. If the directory cannot be made, / is still a better cwd than
    the user's files, and the call proceeds rather than failing the reply.
    """
    try:
        os.makedirs(AGENT_CWD, mode=0o700, exist_ok=True)
        return AGENT_CWD
    except OSError as exc:
        log(f"could not create {AGENT_CWD} ({exc}); running the agent in /")
        return "/"


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


def grants_tools(argv):
    """True if this argv hands the agent CLI tools of its own.

    `--tools ""` is how the shipped invocation *removes* the built-in set, so
    a --tools whose value is empty is a denial, not a grant; anything else
    after it names tools to keep. --allowedTools adds to whatever is already
    there, so it is always a grant.
    """
    for i, part in enumerate(argv):
        if "--dangerously" in part or "--allowedTools" in part \
                or "--allowed-tools" in part:
            return True
        if part == "--tools":
            return bool(argv[i + 1].strip()) if i + 1 < len(argv) else False
        if part.startswith("--tools="):
            return bool(part.split("=", 1)[1].strip())
    return False


# Agent CLIs whose flag vocabulary this daemon actually knows. Recognising a
# *denial* is not the same problem as recognising a grant: to say an
# invocation is tool-free we have to know which flag removes the tools and
# what its absence implies, and that is per-CLI knowledge. We have it for
# Claude Code (argv flags) and for opencode (agent-file frontmatter, below).
KNOWN_CLIS = ("claude", "opencode")

TOOLS_DENIED = "denied"      # verified tool-free
TOOLS_GRANTED = "granted"    # verified to hand the CLI tools
TOOLS_UNKNOWN = "unknown"    # we cannot tell, so we must not claim


# opencode 1.18.30 carries no permission keys on `run --help`: grants live in
# the agent file frontmatter (~/.config/opencode/agents/<name>.md), where a
# missing key falls back to the default-allow ruleset. So "denied" here means
# every one of these keys is EXPLICITLY deny in that file -- anything else
# (absent, ask, allow, unparseable) fails closed to unknown. Verified against
# 1.18.30 via `opencode debug agent <name>` (effective tools map) plus the
# disposable-dir read canary; re-verify after any opencode upgrade.
OPENCODE_DENY_KEYS = (
    "bash", "edit", "read", "glob", "grep", "list", "task",
    "webfetch", "websearch", "skill", "lsp", "todowrite",
    "external_directory", "question",
)


def opencode_agent_name(argv):
    """The --agent value in an `opencode run` argv, or None."""
    for i, part in enumerate(argv):
        if part == "--agent" and i + 1 < len(argv):
            return argv[i + 1].strip() or None
        if part.startswith("--agent="):
            return part.split("=", 1)[1].strip() or None
    return None


def opencode_frontmatter_denies(name):
    """True only if the opencode agent file explicitly denies every tool key.

    Reads descriptor-first (no symlink, regular file, our uid, bounded) --
    the file decides what the always-on mic may invoke, so a planted symlink
    or FIFO there must fail closed, not redirect or block us.
    """
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".config", "opencode", "agents", name + ".md"),
        os.path.join(home, ".config", "opencode", "agent", name + ".md"),
    ]
    text = None
    for path in candidates:
        try:
            text = safefile.read_text(path, safefile.MAX_TEXT_BYTES)
            break
        except OSError:
            continue
    if text is None:
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    front = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        front.append(line)
    else:
        return False  # no closing fence: not a frontmatter we understand
    # Minimal parse of the `permission:` block: flat `key: action` pairs.
    # Anything shaped differently (nested objects, anchors, missing block)
    # fails closed -- we only recognise the exact all-deny shape we ship.
    in_perm, seen = False, {}
    for line in front:
        if re.match(r"^permission\s*:\s*$", line):
            in_perm = True
            continue
        if in_perm:
            if re.match(r"^\S", line):
                break  # next top-level key: permission block is over
            match = re.match(r"^\s+([A-Za-z_*]+)\s*:\s*(.+?)\s*$", line)
            if match:
                seen[match.group(1)] = match.group(2).strip("\"'")
    if not seen:
        return False
    if seen.get("*", "deny") != "deny":
        return False
    return all(seen.get(key) == "deny" for key in OPENCODE_DENY_KEYS)


def tool_posture(executable, argv):
    """What we can honestly say about the tools this invocation exposes.

    The trap this exists to close: `actions` says whether *Jarvis* will act on
    a <<jarvis:...>> directive. It says nothing about whether the agent CLI
    has tools of its own. Reporting "answer-only" off `actions` alone once let
    a `codex exec -s read-only` preset -- a live shell over $HOME -- describe
    itself as answer-only in the panel, the journal and --check. An unknown
    CLI is not a safe CLI, it is an unaudited one, and the label has to say so.
    """
    if grants_tools(argv):
        return TOOLS_GRANTED
    if os.path.basename(executable) not in KNOWN_CLIS:
        return TOOLS_UNKNOWN
    if os.path.basename(executable) == "opencode":
        # No argv flag can deny opencode tools; the agent file is the whole
        # story. All-deny frontmatter verifies tool-free, anything else --
        # including an unreadable or hand-edited file -- fails closed.
        name = opencode_agent_name(argv)
        if name and opencode_frontmatter_denies(name):
            return TOOLS_DENIED
        return TOOLS_UNKNOWN
    # Claude Code: tool-free requires *both* the empty built-in allowlist and
    # a strict MCP config with nothing to load, or the user's own MCP servers
    # come back. Bare `claude -p` is not answer-only.
    empty_tools = any(
        (part == "--tools" and i + 1 < len(argv) and not argv[i + 1].strip())
        or (part.startswith("--tools=") and not part.split("=", 1)[1].strip())
        for i, part in enumerate(argv)
    )
    strict_mcp = "--strict-mcp-config" in argv and not any(
        part == "--mcp-config" or part.startswith("--mcp-config=")
        for part in argv
    )
    return TOOLS_DENIED if (empty_tools and strict_mcp) else TOOLS_UNKNOWN


def capability_label(agent):
    """One phrase for the panel, the journal and --check, and never a lie.

    Unknown CLI posture is FAIL, never safe: anything we cannot positively
    verify tool-free says so out loud.
    """
    posture = tool_posture(agent.executable, agent.command)
    if posture == TOOLS_GRANTED:
        return "FAIL: CLI tools granted"
    if posture == TOOLS_UNKNOWN:
        return "FAIL: tools not verified"
    return "can act" if agent.actions else "answer-only"


def _model_listed(model_id, timeout=15):
    """Best-effort membership probe against `opencode models`.

    Returns None when opencode could not be asked at all (missing, slow,
    failing) -- startup must never depend on a probe -- else whether
    model_id is listed.
    """
    try:
        import subprocess
        proc = subprocess.run(["opencode", "models"], capture_output=True,
                              text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return any(line.strip() == model_id for line in proc.stdout.splitlines())


def select_agent(cfg):
    """Resolve cfg['agent'] to an Agent, failing loudly on a bad name."""
    name = cfg.get("agent", "claude")
    specs = cfg.get("agents", {})
    if name not in specs:
        known = ", ".join(sorted(specs)) or "none"
        raise SystemExit(f"[jarvis] unknown agent '{name}'. Configured: {known}")
    try:
        agent = Agent(name, specs[name])
    except ValueError as exc:
        # A clean message, not a traceback, for systemd's restart loop to log.
        raise SystemExit(f"[jarvis] {exc}")
    # The top-level `model` key selects the OpenCode model for opencode-voice
    # (the panel Model dropdown writes it). Injected here so config and daemon
    # agree without the panel having to know agent argv shapes.
    if name == "opencode-voice":
        model_setting = cfg.get("model")
        if not model_setting:
            log("warning: no model configured for opencode-voice; "
                "pick one in the panel")
        else:
            for template in (agent.command, agent.web_command or []):
                try:
                    idx = template.index("--agent")
                except ValueError:
                    log(f"warning: agent '{name}' argv has no '--agent'; "
                        "cannot inject the configured model")
                    break
                if len(template) < idx + 2:
                    log(f"warning: agent '{name}' argv has no agent name "
                        "after '--agent'; cannot inject the configured model")
                    break
                template.insert(idx + 2, "-m")
                template.insert(idx + 3, model_setting)
            # Advisory only: hard validation lives in `jarvis-config set
            # model` behind the panel. A hiccup in `opencode models` must
            # never kill the mic, so an unaskable probe stays silent.
            if _model_listed(model_setting) is False:
                log(f"warning: configured model '{model_setting}' is not "
                    "listed by `opencode models`; continuing anyway")
    if shutil.which(agent.executable) is None:
        log(f"warning: '{agent.executable}' is not on PATH -- replies will fail")
    # Both argv templates, not just the first: web_command carries a search
    # query derived from the same transcript, and argv is argv.
    for label, template in (("command", agent.command),
                            ("web_command", agent.web_command or [])):
        if any("{prompt}" in part for part in template):
            log(f"warning: agent '{agent.name}' puts the transcript in argv "
                f"via `{label}`, where every local process can read it; drop "
                f"{{prompt}} from `{label}` to send it on stdin instead")
    # Actions are brokered by this daemon, never by a tool grant to the CLI.
    # A command that hands the agent tools anyway isn't something we can
    # police -- it's the user's argv -- but it deserves a loud note. An
    # *empty* --tools is the opposite of a grant, so it doesn't count.
    if grants_tools(agent.command):
        log(f"warning: agent '{agent.name}' grants the CLI tools in `command`. "
            "Jarvis never needs that: actions go through the jarvis-open "
            "broker, and a search grant belongs in `web_command`. Remove the "
            "tool flags unless you accept the risk.")
    elif tool_posture(agent.executable, agent.command) == TOOLS_UNKNOWN:
        # Not an accusation, an admission: we do not know this CLI's flags, so
        # we cannot tell a text box from a shell. Saying nothing here is what
        # let a read-only Codex sandbox pass itself off as answer-only.
        log(f"warning: agent '{agent.name}' runs '{agent.executable}', whose "
            "tool flags Jarvis does not know, so it CANNOT confirm this "
            "invocation is answer-only. A sandbox flag is not a tool denial: "
            "some CLIs still read every file you can. Verify it yourself -- "
            "put a known string in a file, then run `jarvis-listen --ask "
            "\"read <that file> and tell me what it says\"`. If the string "
            "comes back, this agent can read your home directory.")
    # The web invocation reads the open internet, so what it may hold matters
    # more, not less: WebFetch or a shell there hands a hostile page an
    # exfiltration channel. WebSearch alone is the sanctioned grant.
    if any("WebFetch" in part or "Bash" in part or "--dangerously" in part
           for part in agent.web_command or []):
        log(f"warning: agent '{agent.name}' grants `web_command` more than "
            "web search. A fetch tool or a shell in the web-enabled call "
            "lets a hostile page exfiltrate or act; grant WebSearch only.")
    caps = capability_label(agent)
    if agent.web:
        caps += ", web search"
    mode = current_mode(cfg)
    log(f"agent: {agent.name} ({caps})")
    log(f"mode: {mode}")
    for problem in check_mode_invariants(mode, agent):
        log(f"REFUSING startup: {problem}")
        raise SystemExit(f"[jarvis] {problem}")
    if mode != "safe":
        log(f"workspace auto-disarm: {workspace_timeout(cfg, mode):.0f}s "
            "(explicit physical action only; voice authorizes nothing "
            "destructive)")
    # Publish mode for the widget/panel alongside the pipeline state.
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        safefile.write_atomic(MODE_FILE, mode)
        if mode == "safe":
            try:
                os.unlink(ARM_FILE)
            except OSError:
                pass
        else:
            deadline = time.time() + workspace_timeout(cfg, mode)
            safefile.write_atomic(ARM_FILE, f"{deadline:.0f}")
    except OSError:
        pass
    return agent


def resolve_voice(cfg):
    voice = cfg.get("voice", DEFAULTS["voice"])
    return voice if os.path.isabs(voice) else os.path.join(VOICES_DIR, voice)


def resolve_wake_model(cfg):
    """Map a wake-word name to the onnx file openWakeWord ships.

    Returns (path, score_key). The score key is the file stem, which is what
    Model.predict() uses to label its scores.
    """
    import openwakeword

    name = cfg.get("wake_word", DEFAULTS["wake_word"])
    models_dir = os.path.join(os.path.dirname(openwakeword.__file__),
                              "resources", "models")
    for stem in sorted(os.path.splitext(f)[0] for f in os.listdir(models_dir)
                       if f.endswith(".onnx")):
        # "hey_jarvis" should match the shipped "hey_jarvis_v0.1".
        if stem == name or stem.rsplit("_v", 1)[0] == name:
            return os.path.join(models_dir, stem + ".onnx"), stem
    raise SystemExit(f"[jarvis] unknown wake_word '{name}'. "
                     f"Available: {', '.join(WAKE_WORDS)}")


# --------------------------------------------------------------------------
# Pipeline state, shared with the bar widget
# --------------------------------------------------------------------------

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


def transcribe(path):
    """Run voxtype's local whisper model. It logs to stdout, so take the tail."""
    proc = run_bounded(["voxtype", "transcribe", path], timeout=120)
    if proc.overflowed:
        log("voxtype exceeded its output ceiling; transcription discarded")
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
    r"^\s*<<jarvis:(open-app|open-url|search|volume|brightness|workspace|mute|media|music|mymusic)"
    r"\s+([^<>\n]{1,256}?)\s*>>\s*$")
_DIRECTIVE_KINDS = {"open-app": "app", "open-url": "url", "search": "search",
                    "volume": "volume", "brightness": "brightness",
                    "workspace": "workspace", "mute": "mute",
                    "media": "media", "music": "music", "mymusic": "mymusic"}
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
# Song/artist words for the music verb: letters, digits, spaces and a small
# set of title punctuation. The broker resolves these to one top YouTube
# result itself -- the agent never sees URLs and never picks one.
MUSIC_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +.'\-]{0,79}$")
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
        return value[:16]
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

    Directive lines are stripped from the spoken text whether or not actions
    are enabled -- an ignored directive should not be read aloud either --
    and only the first one counts.
    """
    directive = None
    kept = []
    for line in reply.splitlines():
        match = DIRECTIVE_RE.match(line)
        if match:
            if directive is None:
                directive = (_DIRECTIVE_KINDS[match.group(1)],
                             match.group(2).strip())
            continue
        kept.append(line)
    return "\n".join(kept).strip(), directive


def run_directive(directive, mode="safe", approval_source="voice",
                  require_arm=False, arm_file=ARM_FILE):
    """Public entry: publishes the tool category for the activity indicator,
    then enforces + executes. Category only -- never arguments."""
    publish_tool(f"broker:{directive[0]}")
    try:
        return _run_directive(directive, mode, approval_source,
                              require_arm, arm_file)
    finally:
        publish_tool(f"broker:{directive[0]}")


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
    broker = jarvis_open_path()
    if broker is None:
        log("directive refused: jarvis-open broker not found")
        audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "refused-no-broker"})
        return False
    try:
        proc = run_bounded([broker, kind, value],
                           timeout=BROKER_TIMEOUTS.get(kind, 15),
                           stdout_limit=64 << 10, stderr_limit=16 << 10)
    except (OSError, subprocess.TimeoutExpired):
        log("jarvis-open did not run")
        audit({"mode": mode, "transcript_len": 0, "tool_name": f"broker:{kind}",
               "args_summary_sanitized": summary,
               "approval_source": approval_source, "result_code": "error-exec"})
        return False
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


# Canned confirmations for the routine fast path (generic wording only --
# song names and other speech never go back out verbatim from here).
ROUTINE_CONFIRM = {
    "app": "Opening it now.", "url": "Opening it now.",
    "volume": "Volume adjusted.", "brightness": "Brightness adjusted.",
    "workspace": "Switching workspace.", "mute": "Done.",
    "media": "Done.", "music": "Playing it now.",
    "mymusic": "Playing it now.",
}


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
    m = re.match(r"^(?:play|put on)\s+(.{1,80})$", t)
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
    m = re.match(r"^open ([a-z0-9][a-z0-9 ._\-]{0,30})$", t)
    if m:
        return ("app", m.group(1).strip())
    return None


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
        # or a non-match falls through to the agent below.
        routine = match_routine_intent(text)
        if routine is not None:
            if run_directive(routine, mode, approval_source,
                             require_arm=(approval_source == "voice")):
                answer = ROUTINE_CONFIRM.get(routine[0], "Doing it now.")
                audit({"mode": mode, "transcript_len": len(text),
                       "tool_name": f"broker:{routine[0]}",
                       "args_summary_sanitized": directive_summary(*routine),
                       "approval_source": approval_source,
                       "result_code": "ok-intent"})
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
            if ok and not answer:
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
                   mode="safe"):
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
            text = transcribe(path)
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
                   mode="safe", auto_disarm_seconds=0):
    from openwakeword.model import Model

    model = Model(wakeword_model_paths=[wake_path])
    log(f"model loaded, listening for '{wake_key.rsplit('_v', 1)[0].replace('_', ' ')}'")

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
                               mode)
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

    agent = select_agent(cfg)
    voice = resolve_voice(cfg)
    wake_path, wake_key = resolve_wake_model(cfg)
    log_text = bool(cfg.get("log_transcripts", False))

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
        return 0 if ok else 1

    if args.ask:
        respond(agent, voice, args.ask, log_text, mode, args.source)
        return 0

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    deadline = workspace_timeout(cfg, mode) if mode != "safe" else 0
    listen_forever(agent, voice, wake_path, wake_key, listen, log_text,
                   mode, deadline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
