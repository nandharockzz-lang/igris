import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Mpris
import Quickshell.Wayland

// Persistent Jarvis console. The service is intentionally separate from the
// bar widget: the bar is a small control, while this layer stays visible on
// the primary monitor and expands only when Jarvis has something to show.
Item {
  id: root

  property var shell: null
  property string omarchyPath: ""

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string runtimeDir:
    (Quickshell.env("XDG_RUNTIME_DIR") ||
     Quickshell.env("XDG_STATE_HOME") || (root.home + "/.local/state")) + "/jarvis"
  readonly property string toggleBin: root.home + "/.local/share/jarvis/bin/jarvis-toggle"
  readonly property string brokerBin: root.home + "/.local/share/jarvis/bin/jarvis-open"

  // Staged destructive/security-sensitive action awaiting an on-screen
  // tap. The broker writes it, these buttons run `jarvis-open confirm`
  // or `deny`; expiry (120s) is enforced by the broker on read, and the
  // card re-polls, so a stale file clears itself from the screen.
  property bool pendingActive: false
  property string pendingText: ""

  property string serviceState: "unknown"
  property string pipeline: "off"
  property string mode: "safe"
  property int remainingSecs: 0
  property real voiceLevel: 0
  property var micBars: []
  property int wakeAt: 0
  property string requestText: ""
  property string responseText: ""
  property bool expanded: false
  property bool busy: false
  property bool wakeFresh: false
  property bool draggableAvatarEnabled: true
  property bool avatarEnabled: true

  readonly property string cfgPath:
    (Quickshell.env("XDG_CONFIG_HOME") || (root.home + "/.config"))
    + "/jarvis/config.toml"

  // Hold the card open briefly after speaking ends so the reply stays
  // readable. Pure QML: no daemon change needed.
  Timer {
    id: holdTimer
    interval: 3000
    repeat: false
    onTriggered: root.refreshExpanded()
  }

  // The avatar enable flag lives in the daemon's TOML config, which QML
  // cannot parse — read the single line through a process like every
  // other daemon value (XHR on file:// is disabled in this shell build).
  Process {
    id: avatarCfgProc
    command: ["sh", "-c",
      "timeout 1 grep -m1 '^draggable_avatar_enabled' \"$1\" 2>/dev/null",
      "jarvis-avatar-cfg", root.cfgPath]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var m = String(text || "").match(/=\s*(true|false)/)
        var enabled = m ? m[1] === "true" : true
        if (root.avatarEnabled !== enabled) root.avatarEnabled = enabled
        if (root.draggableAvatarEnabled !== enabled) root.draggableAvatarEnabled = enabled
      }
    }
  }

  Timer {
    id: avatarCfgTimer
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: { if (!avatarCfgProc.running) avatarCfgProc.running = true }
  }

  Loader {
    id: draggableAvatarLoader
    active: true
    source: Qt.resolvedUrl("DraggableAvatar.qml")
    visible: true
    onLoaded: root.syncAvatarProps()
  }

  // Peek: right-clicking the idle avatar re-opens the card on demand
  // (media controls included) without any voice activity. Auto-hides, and
  // any click inside the card extends the stay so it can't vanish
  // mid-interaction.
  Timer {
    id: peekTimer
    interval: 15000
    repeat: false
    onTriggered: {
      root.peeked = false
      root.refreshExpanded()
    }
  }

  function peekCard() {
    root.peeked = true
    peekTimer.restart()
    root.refreshExpanded()
  }

  function keepPeek() {
    if (root.peeked) peekTimer.restart()
  }

  Connections {
    enabled: draggableAvatarLoader.item !== null
    target: draggableAvatarLoader.item
    function onPeekRequested() { root.peekCard() }
    function onPeekKept() { root.keepPeek() }
    function onPlaybackToggleRequested() { root.togglePlayback() }
    function onMediaLinkRequested() { root.openMediaLink() }
    function onPendingAnswered(ok) { root.answerPending(ok) }
  }

  function syncAvatarProps() {
    var a = draggableAvatarLoader.item
    if (!a) return
    // Pure-view avatar: service owns every value. Guard each assignment
    // so a half-loaded item can never throw the sync off.
    try {
      if ("avatarState" in a) a.avatarState = root.avatarState
      if ("mode" in a) a.mode = root.mode
      if ("modeColor" in a) a.modeColor = root.modeColor
      if ("statusText" in a) a.statusText = root.statusText
      if ("voiceLevel" in a) a.voiceLevel = root.voiceLevel
      if ("pipeline" in a) a.pipeline = root.pipeline
      if ("serviceState" in a) a.serviceState = root.serviceState
      if ("requestText" in a) a.requestText = root.requestText
      if ("responseText" in a) a.responseText = root.responseText
      if ("hasMedia" in a) a.hasMedia = root.hasMedia
      if ("mediaTitle" in a) a.mediaTitle = root.mediaTitle
      if ("mediaArtist" in a) a.mediaArtist = root.mediaArtist
      if ("mediaUrl" in a) a.mediaUrl = root.mediaUrl
      if ("mediaPlaying" in a)
        a.mediaPlaying = !!(root.activePlayer && root.activePlayer.isPlaying)
      if ("listening" in a) a.listening = root.listening
      if ("micBars" in a) a.micBars = root.micBars
      if ("pendingActive" in a) a.pendingActive = root.pendingActive
      if ("pendingText" in a) a.pendingText = root.pendingText
      if ("cardOpen" in a) a.cardOpen = root.consoleVisible
      if ("avatarVisible" in a) a.avatarVisible = !root.active && root.avatarEnabled
    } catch (e) {}
  }

  onAvatarStateChanged: root.syncAvatarProps()
  onModeChanged: root.syncAvatarProps()
  onVoiceLevelChanged: root.syncAvatarProps()
  onPipelineChanged: root.syncAvatarProps()
  onRequestTextChanged: root.syncAvatarProps()
  onResponseTextChanged: root.syncAvatarProps()
  onHasMediaChanged: root.syncAvatarProps()
  onMediaUrlChanged: root.syncAvatarProps()
  onMediaTitleChanged: root.syncAvatarProps()

  readonly property bool armed: root.serviceState === "active"
  readonly property bool failed: root.serviceState === "failed"
  readonly property bool listening: root.pipeline === "listening"
  readonly property bool speaking: root.pipeline === "speaking"
  readonly property bool hasText: root.requestText !== "" || root.responseText !== ""
  readonly property var mprisPlayers: Mpris.players ? Mpris.players.values : []
  readonly property var activePlayer: root.selectActivePlayer()
  // Playing first, then a paused player that still has a track. Pause must
  // not hide the mini player; hasMedia still never opens the card alone.
  readonly property bool hasMedia: !!root.activePlayer
  readonly property string mediaTitle: root.activePlayer
    ? (root.activePlayer.trackTitle || root.activePlayer.identity || "Media") : ""
  readonly property string mediaArtist: root.activePlayer
    ? (root.activePlayer.trackArtist || "") : ""
  readonly property string mediaUrl: {
    var p = root.activePlayer
    if (!p) return ""
    var md = p.metadata || {}
    var u = ""
    try { u = String(md["xesam:url"] || md["xesam:Url"] || "") } catch (e) { u = "" }
    if (u.indexOf("http://") === 0 || u.indexOf("https://") === 0) return u
    return ""
  }
  readonly property string modeName: {
    if (root.mode === "privileged") return "FULL"
    if (root.mode === "workspace") return "BASIC"
    return "SAFE"
  }
  readonly property color modeColor: {
    if (root.failed) return "#f85149"
    if (!root.armed) return "#8b949e"
    if (root.mode === "privileged") return "#f0883e"
    if (root.mode === "workspace") return "#3fb950"
    return "#58a6ff"
  }
  readonly property string statusText: {
    if (root.failed) return "Assistant error"
    if (!root.armed) return "Mic off"
    if (root.pipeline === "listening") return "Listening…"
    if (root.pipeline === "thinking") return "Thinking…"
    if (root.pipeline === "speaking") return "Speaking…"
    if (root.remainingSecs > 0 && root.mode !== "safe")
      return root.modeName + " · " + Math.floor(root.remainingSecs / 60) + ":" +
        (root.remainingSecs % 60 < 10 ? "0" : "") + (root.remainingSecs % 60)
    return root.modeName + " · Ready"
  }
  readonly property string avatarState: {
    if (root.failed) return "error"
    if (!root.armed) return "off"
    var now = Date.now() / 1000
    if (root.listening) return "listening"
    if (root.speaking) return "speaking"
    if (root.pipeline === "thinking") return "thinking"
    if (root.wakeAt > 0 && now - root.wakeAt < 2) return "wake"
    return "idle"
  }
  // Active = voice activity, the post-speech hold, or a manual peek from
  // the avatar. hasText is content (shown inside the card while open),
  // hasMedia never opens the card by itself.
  property bool peeked: false
  readonly property bool active: root.listening ||
    root.speaking || root.pipeline === "thinking" || holdTimer.running ||
    root.peeked
  readonly property bool showAvatar: root.active
  // A staged confirmation opens the card on its own: the tap it needs
  // is on screen, with nothing else to do first.
  readonly property bool consoleVisible: root.active || root.pendingActive

  function selectActivePlayer() {
    var list = root.mprisPlayers || []
    var paused = null
    for (var i = 0; i < list.length; i++) {
      var p = list[i]
      if (!p) continue
      if (p.isPlaying) return p
      if (!paused && (p.trackTitle || p.trackArtist || p.identity))
        paused = p
    }
    return paused
  }

  function refreshWakeFresh() {
    var now = Date.now() / 1000
    root.wakeFresh = root.wakeAt > 0 && now >= root.wakeAt &&
      now - root.wakeAt < 2
  }

  function refresh() {
    if (!stateProc.running) stateProc.running = true
    if (!requestProc.running) requestProc.running = true
    if (!responseProc.running) responseProc.running = true
    if (!pendingProc.running) pendingProc.running = true
    root.refreshWakeFresh()
  }

  // Live poll: without this the overlay only refreshed after a click and
  // went stale (stuck text, stuck expanded). Same 1s cadence the bar uses.
  Timer {
    id: pollTimer
    interval: 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  onActiveChanged: root.syncAvatarProps()
  onConsoleVisibleChanged: root.syncAvatarProps()
  onPendingActiveChanged: root.syncAvatarProps()
  onMicBarsChanged: root.syncAvatarProps()

  function refreshExpanded() {
    root.expanded = root.active
    root.syncAvatarProps()
  }

  function notePipelineTransition(prev) {
    // Entering voice activity cancels the hold; leaving speaking starts it.
    if (root.listening || root.speaking || root.pipeline === "thinking") {
      if (holdTimer.running) holdTimer.stop()
    } else if (prev === "speaking" && !holdTimer.running) {
      holdTimer.restart()
    }
  }

  function toggleMode() {
    if (root.busy) return
    root.busy = true
    toggleProc.running = true
  }

  function disarm() {
    if (root.busy || !root.armed) return
    root.busy = true
    stopProc.running = true
  }

  function togglePlayback() {
    var p = root.activePlayer
    if (!p) {
      console.log("[jarvis] play/pause: no active player")
      return
    }
    try {
      if (p.canTogglePlaying) p.togglePlaying()
      else if (p.isPlaying && p.canPause) p.pause()
      else if (!p.isPlaying && p.canPlay) p.play()
      else console.log("[jarvis] play/pause: player advertises no usable transport")
    } catch (e) {
      console.log("[jarvis] play/pause failed: " + e)
    }
    root.keepPeek()
  }

  function openMediaLink() {
    root.keepPeek()
    // Broker reads MPRIS xesam:url and opens Chromium. QML metadata keys
    // are not a reliable source for the YouTube link.
    openMediaProc.command = [root.brokerBin, "media-link"]
    openMediaProc.running = true
  }

  Process {
    id: openMediaProc
  }

  Process {
    id: stateProc
    command: ["sh", "-c",
      "J=\"$1\"; " +
      "systemctl --user is-active \"$2\" 2>/dev/null; " +
      "for f in state mode armed_until wake level mic; do " +
      "timeout 1 head -c 160 \"$J/$f\" 2>/dev/null; echo; done",
      "jarvis-console", root.runtimeDir, "jarvis"]
    stdout: StdioCollector {
      id: stateOut
      waitForEnd: true
      onStreamFinished: {
        var lines = String(text || "").trim().split("\n")
        var prevPipeline = root.pipeline
        root.serviceState = (lines[0] || "unknown").trim()
        root.pipeline = lines.length > 1 ? lines[1].trim() : "off"
        root.mode = (lines.length > 2 &&
          (lines[2].trim() === "workspace" || lines[2].trim() === "privileged"))
          ? lines[2].trim() : "safe"
        var until = parseInt(lines.length > 3 ? lines[3].trim() : "", 10)
        root.remainingSecs = !isNaN(until)
          ? Math.max(0, until - Math.floor(Date.now() / 1000)) : 0
        var wake = parseInt(lines.length > 4 ? lines[4].trim() : "", 10)
        root.wakeAt = isNaN(wake) ? 0 : wake
        root.refreshWakeFresh()
        var level = (lines.length > 5 ? lines[5].trim() : "").split(/\s+/)
        var levelAt = level.length > 1 ? parseFloat(level[1]) : 0
        root.voiceLevel = level.length > 1 && Date.now() / 1000 - levelAt < 0.7
          ? Math.max(0, Math.min(100, parseInt(level[0] || "0", 10))) : 0
        var mic = (lines.length > 6 ? lines[6].trim() : "").split(/\s+/)
        var micAt = mic.length > 12 ? parseFloat(mic[12]) : 0
        if (mic.length > 12 && Date.now() / 1000 - micAt < 1.5) {
          var bars = []
          for (var i = 0; i < 12; i++)
            bars.push(Math.max(0, Math.min(100, parseInt(mic[i] || "0", 10))))
          root.micBars = bars
        } else root.micBars = []
        root.notePipelineTransition(prevPipeline)
        root.refreshExpanded()
      }
    }
  }

  Process {
    id: requestProc
    command: ["sh", "-c", "timeout 1 head -c 1200 \"$1/request\" 2>/dev/null", "jarvis-request", root.runtimeDir]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: { root.requestText = String(text || "").trim(); root.syncAvatarProps() }
    }
  }

  Process {
    id: responseProc
    command: ["sh", "-c", "timeout 1 head -c 1200 \"$1/response\" 2>/dev/null", "jarvis-response", root.runtimeDir]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: { root.responseText = String(text || "").trim(); root.syncAvatarProps() }
    }
  }

  Process {
    id: toggleProc
    command: [root.toggleBin]
    onExited: { root.busy = false; root.refresh() }
  }

  // Pending confirmation, polled with everything else. The file is JSON
  // {description, created}; anything unparseable or older than the
  // broker's TTL reads as absent.
  Process {
    id: pendingProc
    command: ["sh", "-c", "timeout 1 head -c 4096 \"$1/pending-action\" 2>/dev/null",
              "jarvis-pending", root.runtimeDir]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var active = false
        var label = ""
        try {
          var p = JSON.parse(String(text || ""))
          if (p && p.description && p.created) {
            var age = Date.now() / 1000 - parseInt(p.created, 10)
            if (age >= 0 && age < 120) {
              active = true
              label = String(p.description).slice(0, 200)
            }
          }
        } catch (e) {}
        root.pendingActive = active
        root.pendingText = label
        root.refreshExpanded()
      }
    }
  }

  function answerPending(confirmed) {
    if (root.busy) return
    root.busy = true
    pendingAnswerProc.command = [root.brokerBin, confirmed ? "confirm" : "deny"]
    pendingAnswerProc.running = true
  }

  Process {
    id: pendingAnswerProc
    onExited: {
      root.busy = false
      root.pendingActive = false
      root.pendingText = ""
      root.refresh()
    }
  }

  Process {
    id: stopProc
    command: ["systemctl", "--user", "stop", "jarvis"]
    onExited: { root.busy = false; root.refresh() }
  }
}
