# Agent guide (Jarvis / igris)

Omarchy wake-word voice assistant. Plugin id stays **`dorian.voice`** (IPC,
bar widget). Product name in docs/UI is Jarvis; this repo is the **igris**
fork.

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for the pipeline and module map.
Read [`README.md`](README.md) for threat model and install — do not
restate those here.

## Hard rules (do not weaken)

- **Modes:** Safe = answer-only / always-on capable; Basic (`workspace`) =
  broker verbs only on an arm window; Full (`privileged`) = sandboxed
  tools via `jarvis-agent-run`, destructive ops need on-screen confirm.
  Voice never authorizes destruction.
- **No shell in the action path.** Agent reply → parse `<<jarvis:…>>` →
  `jarvis-open` (argv allowlist). Dual validation (daemon + broker).
- **Transcripts on stdin**, never agent argv. Audit/journal: sizes and
  kinds by default — never log spoken text unless `log_transcripts`.
- **`jarvis-config` must not rewrite `[agents.*]` command argv** from the
  panel. Tool denial is load-bearing (`--tools ""`, Grok disallow list,
  OpenCode deny-all frontmatter).
- Prefer **fail closed**: unknown CLI tool posture is FAIL, not safe.

## Where to edit

| Change | Prefer |
| --- | --- |
| Agent selection / tool-deny / capability labels | `daemon/agent_posture.py` |
| Local “play X” / volume / workspace intents | `daemon/routines.py` |
| Runtime UI state files, arm deadline | `daemon/ux_state.py` |
| Directive grammar / confirm contract | `daemon/actions.py` |
| Allowlisted verbs | `daemon/jarvis-open` |
| Wake loop / STT / speak / respond glue | `daemon/jarvis-listen.py` (keep thin) |
| Bar / panel / console | `BarWidget.qml`, `Panel.qml`, `service.qml` |

Shared install layout: one copy under `$JARVIS_DIR/lib/` via
[`install.sh`](install.sh). Do not reintroduce dual copies of
`safefile.py` / `actions.py` under `bin/`.

## Incomplete / gated features

- **Parakeet STT:** refuse unless `JARVIS_EXPERIMENTAL_STT=1`. Default
  engine is whisper only.
- **Custom wake `igris`:** only after `install-wake-word` puts a model on
  disk; panel lists **installed** wake words only.
- **Cloud STT fallback:** off by default; keep the panel warning if
  enabling.

## Verify

Use the install venv (needs numpy / project deps):

```sh
cd daemon
~/.local/share/jarvis/venv/bin/python -m unittest discover -s . -p 'test_jarvis_*.py'
```

After changing installed layout or helpers, re-run `./install.sh` (idempotent)
before claiming a desktop fix.

## Style

- Match existing comment density and security-minded prose; no drive-by
  refactors or unrelated markdown.
- QML: keep bar widget a single control; persistent console/avatar stay in
  `service.qml` / `DraggableAvatar.qml`.
- Do not commit secrets, live transcripts, or expanded hash-lock churn
  unless the task is explicitly updating `requirements.lock` /
  `voices.sha256`.
