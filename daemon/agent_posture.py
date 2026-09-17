"""Agent CLI posture: selection, tool-deny verification, capability labels."""

from __future__ import annotations

import os
import re
import shutil
import sys
import time

import safefile
from ux_state import (
    AGENT_CWD, ARM_FILE, MODE_FILE, MODES, STATE_DIR,
    current_mode, workspace_timeout,
)

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".local", "share", "jarvis")
JARVIS_BIN = os.path.join(JARVIS_DIR, "bin")


def log(msg):
    print(f"[jarvis] {msg}", file=sys.stderr, flush=True)


# Grok Build (grok CLI) --tools "" is NOT an empty allowlist: empty is
# treated as unset and every built-in stays. The voice preset must name
# every filesystem/shell/web tool in --disallowed-tools. tool_posture
# requires this set before it will call the invocation answer-only.
GROK_DISALLOWED_TOOLS = (
    "read_file", "search_replace", "grep", "list_dir",
    "run_terminal_cmd", "run_terminal_command",
    "web_search", "web_fetch", "todo_write",
    "spawn_subagent", "memory_search", "Agent",
)
GROK_DISALLOWED_TOOLS_ARG = ",".join(GROK_DISALLOWED_TOOLS)
GROK_DEFAULT_MODEL = "grok-4.6"

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
    "stt": {
        "engine": "whisper",
        # Benchmark (14 TTS + 14 mic phrases, this CPU): base.en and
        # small.en tie on accuracy (WER 0.058) while base.en answers in
        # 1.3s vs 8.2s. Bigger is not better here; revisit after the
        # parakeet comparison.
        "model": "base.en",
        "language": "en",
        # Command-vocabulary bias for whisper's initial prompt. Short nouns
        # and verbs from the broker grammar, not sentences. Whisper otherwise
        # prefers common English ("player") over command English ("play").
        "vocabulary": (
            "play pause open launch start run terminal chromium firefox "
            "browser files workspace next previous volume mute unmute "
            "brightness screenshot lock music youtube discord settings "
            "calculator close window jarvis foot"
        ),
        # Opt-in cloud fallback for empty local transcripts. Off by
        # default: local-first, and a cloud engine sends audio off-machine.
        "cloud_fallback": False,
        "fallback_engine": "soniox",
    },
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
        # Grok Build CLI. Prompt on stdin via --prompt-file /dev/stdin so
        # the transcript is not in argv. --tools "" is NOT a deny on this
        # CLI (empty is treated as unset); --disallowed-tools must name
        # every filesystem/shell/web tool. Canary: a disposable file's
        # contents must not come back from --ask. Re-run after upgrades.
        "grok": {
            "command": [
                "grok",
                "--prompt-file", "/dev/stdin",
                "--output-format", "plain",
                "--effort", "low",
                "--max-turns", "1",
                "--no-plan",
                "--no-subagents",
                "--disable-web-search",
                "--disallowed-tools", GROK_DISALLOWED_TOOLS_ARG,
                "--system-prompt-override", "{system}",
            ],
            "actions": False,
            "timeout": 180,
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
    "spoken sentence. Never write out a tool call (no XML tags, no "
    "function-call JSON). A <<jarvis:...>> line at the end of your reply is "
    "how you ask Jarvis to act -- that is not a tool call, and it is not "
    "markup to skip."
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
    "Windows and workspaces: <<jarvis:focus-window NAME>> focuses a running "
    "app, <<jarvis:move-window NAME TARGET>> moves it to a workspace "
    "(number, name, next or previous), <<jarvis:workspace NAME>> (or a "
    "number, next, previous) switches there, <<jarvis:fullscreen>> toggles "
    "fullscreen on the focused window. Screen and capture: <<jarvis:lock>> "
    "locks the screen, <<jarvis:screenshot>> (or <<jarvis:screenshot "
    "window>>) saves to ~/Pictures, <<jarvis:notify TEXT>> shows a "
    "notification. Destructive verbs exist but need the user's on-screen "
    "tap before they run: <<jarvis:close-window>>, <<jarvis:logout>>, "
    "<<jarvis:reboot>>, <<jarvis:poweroff>>, <<jarvis:wifi on|off>>. "
    "Emit one directive line per action (compound requests may use more "
    "than one) and say in your reply what they do; if the user must "
    "confirm, say that too. "
    "You cannot browse results, click anything, or control a page: say "
    "so out loud if asked. "
    "The line is stripped before your reply is spoken, "
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
KNOWN_CLIS = ("claude", "opencode", "grok")

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
    if os.path.basename(executable) == "grok":
        # Grok Build: --tools "" is unset (all tools stay). Answer-only
        # requires --disallowed-tools to name every filesystem/shell/web
        # tool. Verified against grok 1.0.30 with a disposable-file canary.
        return TOOLS_DENIED if grok_tools_denied(argv) else TOOLS_UNKNOWN
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


def grok_tools_denied(argv):
    """True only if argv names every required Grok tool in --disallowed-tools."""
    denied = set()
    for i, part in enumerate(argv):
        raw = ""
        if part == "--disallowed-tools" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif part.startswith("--disallowed-tools="):
            raw = part.split("=", 1)[1]
        else:
            continue
        denied.update(t.strip() for t in raw.split(",") if t.strip())
    required = set(GROK_DISALLOWED_TOOLS) - {"run_terminal_command"}
    return required <= denied


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


def inject_model_flag(template, model_setting, after_flag=None):
    """Insert or replace -m/--model in an argv template (mutates in place)."""
    for flag in ("-m", "--model"):
        if flag in template:
            idx = template.index(flag)
            if idx + 1 < len(template):
                template[idx + 1] = model_setting
            else:
                template.append(model_setting)
            return
    if after_flag and after_flag in template:
        idx = template.index(after_flag)
        template.insert(idx + 2, "-m")
        template.insert(idx + 3, model_setting)
        return
    template.insert(1, "-m")
    template.insert(2, model_setting)

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
    # The top-level `model` key is injected as `-m` for agents that take it
    # (opencode-voice, grok). The panel Model dropdown writes that key.
    if name in ("opencode-voice", "grok"):
        model_setting = cfg.get("model")
        if name == "grok" and (
                not model_setting or not str(model_setting).startswith("grok-")):
            model_setting = GROK_DEFAULT_MODEL
        if not model_setting:
            log(f"warning: no model configured for {name}; pick one in the panel")
        else:
            after = "--agent" if name == "opencode-voice" else None
            for label, template in (("command", agent.command),
                                    ("web_command", agent.web_command or [])):
                if not template:
                    continue
                if after and after not in template:
                    log(f"warning: agent '{name}' {label} has no '{after}'; "
                        "cannot inject the configured model there")
                    continue
                inject_model_flag(template, model_setting, after_flag=after)
            if name == "opencode-voice" and _model_listed(model_setting) is False:
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

