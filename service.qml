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
    target: draggableAvatarLoader.item
    function onPeekRequested() { root.peekCard() }
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

  readonly property bool armed: root.serviceState === "active"
  readonly property bool failed: root.serviceState === "failed"
  readonly property bool listening: root.pipeline === "listening"
  readonly property bool speaking: root.pipeline === "speaking"
  readonly property bool hasText: root.requestText !== "" || root.responseText !== ""
  readonly property var mprisPlayers: Mpris.players ? Mpris.players.values : []
  readonly property var activePlayer: root.selectActivePlayer()
  // selectActivePlayer() only returns an actually-playing player, so
  // hasMedia is false when paused — and it never opens the card alone.
  readonly property bool hasMedia: !!root.activePlayer
  readonly property string mediaTitle: root.activePlayer
    ? (root.activePlayer.trackTitle || root.activePlayer.identity || "Media") : ""
  readonly property string mediaArtist: root.activePlayer
    ? (root.activePlayer.trackArtist || "") : ""
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
  readonly property bool consoleVisible: root.active

  function selectActivePlayer() {
    var list = root.mprisPlayers || []
    for (var i = 0; i < list.length; i++) {
      var p = list[i]
      if (p && p.isPlaying) return p
    }
    return null
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

  Process {
    id: stopProc
    command: ["systemctl", "--user", "stop", "jarvis"]
    onExited: { root.busy = false; root.refresh() }
  }

  // Delayed hide so the card's fade-out animation can play instead
  // of being cut off the moment active flips false.
  Timer {
    id: cardHideTimer
    interval: 240
    repeat: false
  }

  onConsoleVisibleChanged: {
    if (!root.consoleVisible) cardHideTimer.restart()
    else cardHideTimer.stop()
  }

  // Dedicated card surface: a small top-right window sized to the card,
  // not a fullscreen transparent sheet. The window unmaps with the card
  // (visible follows it), and the mask limits input to the card rect, so
  // the click region always exactly matches the visible card -- never a
  // stale fullscreen region, never an invisible catcher.
  PanelWindow {
    id: overlay
    visible: card.visible
    screen: Quickshell.screens && Quickshell.screens.length > 0 ? Quickshell.screens[0] : null
    anchors { top: true; right: true }
    implicitWidth: card.width + 18
    implicitHeight: card.height + 18
    color: "transparent"
    WlrLayershell.namespace: "dorian-voice-console"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    mask: Region {
      Region { item: card }
    }

    Rectangle {
      id: card
      // Idle is the draggable avatar (separate window). This card exists
      // only while voice-active, so it is always the full 360px layout.
      // It stays mapped through the fade-out so hide animates smoothly.
      visible: root.consoleVisible || cardHideTimer.running
      opacity: root.consoleVisible ? 1 : 0
      anchors.top: parent.top
      anchors.right: parent.right
      anchors.topMargin: 18
      anchors.rightMargin: 18
      width: 360
      height: contentColumn.implicitHeight + 28
      radius: 18
      color: Qt.rgba(0.04, 0.055, 0.09, 0.96)
      border.width: 1
      border.color: Qt.rgba(root.modeColor.r, root.modeColor.g, root.modeColor.b, 0.65)

      Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

      // Hide animation stays inside the window: fade plus a slight rise.
      // (A slide off-screen would clip on the dedicated surface and drag
      // the input mask away from the fading card.)
      transform: Translate {
        y: root.consoleVisible ? 0 : -10
        Behavior on y {
          NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
        }
      }

      // NOTE: no Behavior on width/height here on purpose. The height is
      // bound to the content's implicit height, and animating it raced the
      // layershell input mask: the button rendered before its click region
      // existed, so clicks visibly landed on a dead button.

      // Card background catcher, beneath every control: any press inside
      // the card extends a peek so it can't vanish mid-interaction, and
      // the opaque card owns its clicks instead of leaking them through.
      MouseArea {
        id: cardMouse
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onPressed: root.keepPeek()
        onClicked: root.keepPeek()
      }

      Column {
        id: contentColumn
        visible: true
        anchors.fill: parent
        anchors.margins: 14
        spacing: 9

        Row {
          width: parent.width
          spacing: 10

          Item {
            id: miniAvatarWrap
            width: 76
            height: 76
            // Press feedback mirroring the play button, so a click visibly
            // registers even before the daemon state round-trips back.
            scale: avatarMouse.pressed ? 0.92 : 1.0
            Behavior on scale { NumberAnimation { duration: 90 } }

            GokuAvatar {
              anchors.centerIn: parent
              side: 76
              state: root.avatarState
              mode: root.mode
              level: root.voiceLevel
              blink: false
              bob: 0
              reduceMotion: false
              highContrast: false
            }

            // Left steps Basic/Full, right disarms. Explicit buttons,
            // hover and cursor: this is a control, not decoration.
            MouseArea {
              id: avatarMouse
              anchors.fill: parent
              acceptedButtons: Qt.LeftButton | Qt.RightButton
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: function(mouse) {
                root.keepPeek()
                if (mouse.button === Qt.RightButton) root.disarm()
                else root.toggleMode()
              }
            }
          }

          Column {
            visible: true
            width: parent.width - 86
            spacing: 3
            anchors.verticalCenter: parent.verticalCenter

            Text {
              text: "JARVIS"
              color: root.modeColor
              font.bold: true
              font.pixelSize: 12
              font.letterSpacing: 2
            }
            Text {
              text: root.statusText
              color: "#e6edf3"
              font.pixelSize: 13
              elide: Text.ElideRight
              width: parent.width
            }
            Text {
              text: "SUPER+SHIFT+J  ·  toggle mode"
              color: "#8b949e"
              font.pixelSize: 9
              elide: Text.ElideRight
              width: parent.width
            }
          }

        }

        EqBars {
          visible: root.listening
          width: parent.width
          bars: root.micBars
          active: root.listening
          reduceMotion: false
          highContrast: false
          foreground: "#8b949e"
          accent: root.modeColor
          maxHeight: 28
          barWidth: 5
        }

        Column {
          visible: root.hasText
          width: parent.width
          spacing: 5

          Text {
            visible: root.requestText !== ""
            text: root.requestText
            color: "#8b949e"
            font.pixelSize: 11
            elide: Text.ElideRight
            width: parent.width
          }
          Text {
            visible: root.responseText !== ""
            text: root.responseText
            color: "#f0f6fc"
            font.pixelSize: 13
            wrapMode: Text.WordWrap
            width: parent.width
            maximumLineCount: 6
            elide: Text.ElideRight
          }
        }

        Rectangle {
          visible: root.hasMedia
          width: parent.width
          height: 46
          radius: 10
          color: Qt.rgba(1, 1, 1, 0.07)

          Column {
            anchors.left: parent.left
            anchors.leftMargin: 10
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - 54
            spacing: 2
            Text {
              text: root.mediaTitle
              color: "#f0f6fc"
              font.pixelSize: 11
              elide: Text.ElideRight
              width: parent.width
            }
            Text {
              text: root.mediaArtist
              color: "#8b949e"
              font.pixelSize: 10
              elide: Text.ElideRight
              width: parent.width
            }
          }

          Rectangle {
            id: playButton
            anchors.right: parent.right
            anchors.rightMargin: 8
            anchors.verticalCenter: parent.verticalCenter
            width: 44
            height: 36
            radius: 8
            z: 2
            color: root.modeColor
            // Shrink while held so a click visibly registers even before
            // the player state round-trips back.
            scale: playMouse.pressed ? 0.9 : 1.0
            Behavior on scale { NumberAnimation { duration: 90 } }
            Text {
              anchors.centerIn: parent
              text: root.activePlayer && root.activePlayer.isPlaying ? "󰏤" : "󰐊"
              color: "#081018"
              font.pixelSize: 16
            }
            MouseArea {
              id: playMouse
              anchors.fill: parent
              acceptedButtons: Qt.LeftButton
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.togglePlayback()
            }
          }
        }
      }
    }
  }
}
