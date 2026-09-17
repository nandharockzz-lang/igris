import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

// Arm/disarm the "hey jarvis" wake-word listener.
//
// The listener is a systemd user unit that holds the mic open and runs
// openWakeWord continuously (~3% of one core), so it is off by default and
// this widget is the switch. Left click opens the settings panel, right click
// steps the Basic/Full toggle (SUPER+SHIFT+J does the same), middle click
// restarts. Tapping the panel avatar disarms.
// Pipeline state comes from small files the daemon writes, which keeps this
// widget from having to talk to the process at all.
//
// Wake / listen / speak feedback lives on the bar Avatar itself (and on the
// persistent console in service.qml). A second overlay avatar here used to
// duplicate that signal; it was removed so the bar stays a single control.
BarWidget {
  id: root
  moduleName: "dorian.voice"

  readonly property string unit: setting("unit", "jarvis")
  readonly property int pollSeconds: Math.max(1, setting("pollSeconds", 1))
  readonly property bool notify: setting("notify", true)
  readonly property bool reduceMotion: setting("reduceMotion", false)
  readonly property bool highContrast: setting("highContrast", false)
  readonly property string toggleBin:
    (Quickshell.env("HOME") || "") + "/.local/share/jarvis/bin/jarvis-toggle"

  // serviceState: "active" | "inactive" | "unknown"
  // pipeline: "idle" | "listening" | "thinking" | "speaking" | "off"
  // mode: "safe" | "workspace" | "privileged" (daemon-published; safe default)
  // armedUntil: epoch seconds when a workspace/privileged window ends (0 = none)
  property string serviceState: "unknown"
  property string pipeline: "off"
  property string mode: "safe"
  property double armedUntil: 0
  property int remainingSecs: 0
  property bool busy: false

  // UI telemetry (daemon-published numbers only -- never audio or words).
  property real voiceLevel: 0
  property var micBars: []
  property string toolCat: ""
  property double toolAt: 0
  property double wakeAt: 0
  property bool blink: false
  property real bob: 0
  // Freshness flags, refreshed every second by the poll timer (bindings
  // alone cannot observe wall-clock passing).
  property bool wakeFresh: false
  property bool toolFresh: false

  function refreshFreshness() {
    var now = Date.now() / 1000
    root.wakeFresh = root.wakeAt > 0 && (now - root.wakeAt) < 2
    root.toolFresh = root.toolAt > 0 && (now - root.toolAt) < 4
  }

  readonly property bool armed: serviceState === "active"
  readonly property bool failed: serviceState === "failed"

  // Single avatar state for both widget and panel. Never implies a live
  // mic: anything but an armed pipeline renders "off".
  readonly property string avatarState: {
    if (root.failed) return "error"
    if (!root.armed) return "off"
    if (root.pipeline === "listening") return "listening"
    if (root.pipeline === "speaking") return "speaking"
    if (root.pipeline === "thinking") return root.toolFresh ? "tool" : "thinking"
    if (root.wakeFresh) return "wake"
    return "idle"
  }

  readonly property string icon: {
    if (!armed) return "󰍭"          // mic-off
    if (pipeline === "listening") return "󰋎"
    if (pipeline === "thinking") return "󰔟"
    if (pipeline === "speaking") return "󰕾"
    return "󰍬"                      // armed, waiting for the wake word
  }

  // UI names for the internal modes. Daemon state stays safe/workspace/
  // privileged; only the labels are friendly.
  readonly property string modeName: {
    if (root.mode === "privileged") return "Full"
    if (root.mode === "workspace") return "Basic"
    return "Safe"
  }

  readonly property string stateLabel: {
    if (root.failed) return "Voice assistant error -- check the journal"
    if (!armed) return "Mic off -- Disarmed"
    var tag = root.modeName + " armed"
    if (root.armed && root.mode !== "safe" && root.remainingSecs > 0) {
      var m = Math.floor(root.remainingSecs / 60)
      var s = root.remainingSecs % 60
      tag += " " + m + ":" + (s < 10 ? "0" + s : s)
    }
    if (pipeline === "listening") return tag + " · Listening…"
    if (pipeline === "thinking")
      return tag + (root.toolFresh ? " · Running " + root.toolCat + "…" : " · Thinking…")
    if (pipeline === "speaking") return tag + " · Speaking…"
    if (root.wakeFresh) return tag + " · Wake word heard…"
    return tag + ": say \"hey jarvis\""
  }

  function refresh() {
    if (!stateProc.running) stateProc.running = true
  }

  function refreshFast() {
    // 10Hz telemetry (playback amplitude, mic buckets) while it can change.
    // Gated to active speech: idle bar costs one 1s poll, nothing more.
    if (root.armed && !root.failed
        && (root.pipeline === "listening" || root.pipeline === "speaking")) {
      if (!fastProc.running) fastProc.running = true
    }
  }

  // Basic/Full cycle (hotkey, right-click, hero switch): a physical request
  // to jarvis-toggle. The daemon validates and owns the outcome; a refusal
  // surfaces as a notification, never a silent bypass.
  function toggle() {
    if (busy) return
    busy = true
    controlProc.command = [root.toggleBin]
    controlProc.running = true
  }

  // Low-level direct controls (IPC only): start/stop exactly what config says.
  function startNow() {
    if (busy) return
    busy = true
    controlProc.command = ["systemctl", "--user", "start", unit]
    controlProc.running = true
  }

  function stopNow() {
    if (busy) return
    busy = true
    controlProc.command = ["systemctl", "--user", "stop", unit]
    controlProc.running = true
  }

  function restart() {
    if (busy) return
    busy = true
    controlProc.command = ["systemctl", "--user", "restart", unit]
    controlProc.running = true
  }

  function sendNote(body) {
    if (!notify) return
    // argv, not a shell string: Bar has no shellQuote(), and building one by
    // hand is how a notification body ends up interpreted as shell.
    Quickshell.execDetached(["notify-send", "-a", "Jarvis", "Voice assistant", body])
  }

  // ---- settings panel ------------------------------------------------
  // The panel is a separate QML file loaded lazily and handed a reference
  // back to this widget, so it can read the live pipeline state and arm or
  // disarm without duplicating any of it.
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  // Named open/close, not openPanel/closePanel: Bar.findPanelWidget only
  // treats a widget as panel-bearing when it exposes open(), close() and
  // `opened`. Miss one and hotkeys and `omarchy-shell shell toggle` silently
  // skip the widget.
  function open()        { if (panelLoader.item) panelLoader.item.open() }
  function close()       { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }

  readonly property bool popoutSwitchClosing:
    panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // One cheap call for the whole slow state: unit status, pipeline, mode,
  // deadline, wake marker, last tool category. Daemon-published tokens only.
  Process {
    id: stateProc
    // Never read from /tmp: a predictable path in a world-writable directory
    // lets another local user plant `state` as a FIFO, and this poll runs
    // every second. Match the daemon's private fallback, bound the read, and
    // cap how long it may block if the file is a pipe anyway.
    // `unit` is a widget setting, so it is a string someone can type. It is
    // passed as a positional argument and referenced as "$1", never pasted
    // into the script text -- concatenating it would make a bar setting able
    // to carry its own shell.
    command: ["sh", "-c",
      "J=\"${XDG_RUNTIME_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}}/jarvis\"; " +
      "systemctl --user is-active \"$1\" 2>/dev/null; timeout 2 head -c 64 \"$J/state\" 2>/dev/null; echo; " +
      "timeout 2 head -c 16 \"$J/mode\" 2>/dev/null; echo; " +
      "timeout 2 head -c 16 \"$J/armed_until\" 2>/dev/null; echo; " +
      "timeout 2 head -c 16 \"$J/wake\" 2>/dev/null; echo; " +
      "timeout 2 head -c 48 \"$J/tool\" 2>/dev/null",
      "jarvis-state", root.unit]
    stdout: StdioCollector {
      id: stateOut
      waitForEnd: true
      onStreamFinished: {
        var lines = String(text || "").trim().split("\n")
        root.serviceState = (lines[0] || "unknown").trim()
        root.pipeline = root.serviceState === "active"
          ? (lines.length > 1 && lines[1].trim() ? lines[1].trim() : "idle")
          : "off"
        var m = (lines.length > 2 ? lines[2].trim() : "")
        root.mode = (m === "workspace" || m === "privileged") ? m : "safe"
        var until = parseInt(lines.length > 3 ? lines[3].trim() : "", 10)
        root.armedUntil = isNaN(until) ? 0 : until
        if (root.armed && root.mode !== "safe" && root.armedUntil > 0) {
          var now = Math.floor(Date.now() / 1000)
          root.remainingSecs = Math.max(0, root.armedUntil - now)
        } else {
          root.remainingSecs = 0
        }
        var wake = parseInt(lines.length > 4 ? lines[4].trim() : "", 10)
        root.wakeAt = isNaN(wake) ? 0 : wake
        // Tool line is "<category> <epoch>": category only, never arguments.
        var tool = (lines.length > 5 ? lines[5].trim() : "").split(/\s+/)
        root.toolCat = tool.length === 2 ? String(tool[0]).slice(0, 32) : ""
        var tat = tool.length === 2 ? parseInt(tool[1], 10) : NaN
        root.toolAt = isNaN(tat) ? 0 : tat
        root.refreshFreshness()
      }
    }
  }

  // Fast telemetry: playback amplitude + mic buckets, 100ms, gated.
  Process {
    id: fastProc
    command: ["sh", "-c",
      "J=\"${XDG_RUNTIME_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}}/jarvis\"; " +
      "timeout 2 head -c 24 \"$J/level\" 2>/dev/null; echo; " +
      "timeout 2 head -c 96 \"$J/mic\" 2>/dev/null",
      "jarvis-level"]
    stdout: StdioCollector {
      id: fastOut
      waitForEnd: true
      onStreamFinished: {
        var now = Date.now() / 1000
        var lines = String(text || "").trim().split("\n")
        // Level line is "<0-100> <epoch>": stale files never animate.
        var lv = (lines.length > 0 ? lines[0].trim() : "").split(/\s+/)
        var lvl = parseInt(lv[0] || "", 10)
        var lepoch = lv.length > 1 ? parseFloat(lv[1]) : 0
        root.voiceLevel = (!isNaN(lvl) && (now - lepoch) < 0.5)
          ? Math.max(0, Math.min(100, lvl)) : 0
        // Mic line is 12 buckets + epoch: stale means listening ended.
        var parts = (lines.length > 1 ? lines[1].trim() : "").split(/\s+/)
        var mepoch = parts.length > 12 ? parseFloat(parts[12]) : 0
        if ((now - mepoch) < 1.5 && parts.length > 12) {
          var bars = []
          for (var i = 0; i < 12; i++) {
            var v = parseInt(parts[i] || "0", 10)
            bars.push(isNaN(v) ? 0 : Math.max(0, Math.min(100, v)))
          }
          root.micBars = bars
        } else {
          root.micBars = []
        }
        root.refreshFast()
      }
    }
    onExited: root.refreshFast()
  }

  Process {
    id: controlProc
    onExited: function(exitCode) {
      root.busy = false
      root.refresh()
      // jarvis-toggle notifies the outcome itself; only note hard failures.
      if (exitCode !== 0) root.sendNote("toggle failed (exit " + exitCode + ")")
    }
  }

  Timer {
    interval: root.pollSeconds * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: {
      root.refresh()
      root.refreshFreshness()
    }
  }

  // Subtle idle life: blink + bob, both off under reduced motion.
  Timer {
    id: blinkTimer
    interval: 3400
    running: !root.reduceMotion
    repeat: true
    onTriggered: {
      root.blink = true
      blinkOff.restart()
    }
  }
  Timer {
    id: blinkOff
    interval: 160
    repeat: false
    onTriggered: root.blink = false
  }
  Timer {
    id: bobTimer
    interval: 900
    running: !root.reduceMotion
    repeat: true
    onTriggered: root.bob = root.bob === 0 ? 1.1 : 0
  }

  IpcHandler {
    target: "dorian.voice"

    function arm(): void { if (!root.armed) root.startNow() }
    function disarm(): void { if (root.armed) root.stopNow() }
    function toggleArmed(): void { root.toggle() }
    function cycle(): void { root.toggle() }
    function restart(): void { root.restart() }
    function settings(): void { root.togglePanel() }
    function toggle(): void { root.togglePanel() }
    function open(): void { root.open() }
    function close(): void { root.close() }
    // Kept for IPC compat; wake feedback is on the bar Avatar / service console.
    function toggleOverlay(): void {}
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.icon
    labelVisible: false
    // Light up only while it is actually doing something, so an armed but
    // idle listener stays visually quiet.
    active: (root.armed && root.pipeline !== "idle") || root.opened
    fixedWidth: root.vertical ? -1 : 0
    fixedHeight: root.vertical ? root.barSize : -1
    tooltipText: root.stateLabel
      + (root.armed ? "\n~3% of one core while armed" : "\nmic is off")
      + "\nleft: settings · right: Basic/Full toggle"

    Avatar {
      anchors.centerIn: parent
      side: Math.max(16, Math.min(parent.width, parent.height) - 8)
      state: root.avatarState
      mode: root.mode
      level: root.voiceLevel
      blink: root.blink
      bob: root.reduceMotion ? 0 : root.bob
      reduceMotion: root.reduceMotion
      highContrast: root.highContrast
    }

    // Left opens the panel, matching every other bar widget. Right steps
    // the Basic/Full toggle; middle restarts. Tapping the panel avatar
    // disarms -- see Panel.qml.
    onPressed: function(b) {
      if (b === Qt.RightButton) root.toggle()
      else if (b === Qt.MiddleButton) root.restart()
      else root.togglePanel()
    }
  }
}
