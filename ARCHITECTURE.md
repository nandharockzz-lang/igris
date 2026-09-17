# Jarvis architecture

Omarchy plugin id: `dorian.voice`. Daemon + helpers live under
`~/.local/share/jarvis` after `./install.sh`.

## Pipeline

```
mic (16 kHz) ──> openWakeWord ──> [wake] ──> capture to silence
                                              │
                                         voxtype STT
                                              │
                                    match_routine_intent?
                                         │           │
                                        yes          no
                                         │           │
                                    jarvis-open   agent CLI
                                         │           │
                                         │     parse <<jarvis:…>>
                                         │           │
                                         └─────► jarvis-open
                                                     │
                                              Piper TTS ──> speakers
```

UI (QML bar widget / panel / service) polls `$XDG_RUNTIME_DIR/jarvis/` state
files; it does not talk to the daemon process.

## Layout after install

```
~/.local/share/jarvis/
  jarvis-listen.py      # orchestrator (wake loop, respond, main)
  jarvis-config         # settings bridge (panel shells this out)
  lib/                  # shared Python modules (one copy)
    safefile.py
    actions.py
    agent_posture.py
    ux_state.py
    routines.py
    …
  bin/
    jarvis-open
    jarvis-toggle
    jarvis-rollback
    jarvis-agent-run
    jarvis-config       # wrapper → venv python + jarvis-config
  venv/
  voices/
```

Helpers on `PATH`-style use add `…/jarvis/lib` to `sys.path` so they import
`safefile` / `actions` from the single lib tree (no dual copies under
`bin/`).

## Modes

| UI name | Internal `mode` | Behavior |
| --- | --- | --- |
| Safe | `safe` | Answer only; always-on capable; `actions=false` |
| Basic | `workspace` | Broker verbs; arm window; no agent tools |
| Full | `privileged` | Sandboxed full-tools via `jarvis-agent-run` (bwrap); short window; confirm destructive ops |

`jarvis-toggle` requests Basic/Full/stop and sets the top-level `actions`
flag; the daemon validates every transition.

## Key modules (repo `daemon/`)

| File | Role |
| --- | --- |
| `jarvis-listen.py` | Main loop: mic, wake, STT, respond, speak |
| `agent_posture.py` | Agent selection, tool-deny checks, capability labels |
| `ux_state.py` | Runtime state files, levels, arm deadline helpers |
| `routines.py` | Local intent matching (volume, media, …) |
| `actions.py` | Versioned `<<jarvis:…>>` grammar + confirm contract |
| `safefile.py` | Safe open/read/atomic write |
| `jarvis-open` | Allowlisted action broker |
| `jarvis-config` | TOML↔JSON for the panel; voice install |
| `jarvis-toggle` / `jarvis-rollback` | Mode cycle / panic |
| `jarvis-agent-run` | Full-mode bwrap runner |

## Tests

From the repo root (or `daemon/`):

```sh
cd daemon
python3 -m unittest discover -s . -p 'test_jarvis_*.py' -v
```

Tests import `jarvis-listen` and sibling modules from the same directory;
`install.sh` mirrors that layout under `lib/` for production.
