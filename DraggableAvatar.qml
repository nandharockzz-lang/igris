import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

// Idle avatar for Jarvis. Pure view: service.qml owns every daemon value
// (pipeline, text, media) and pushes it here via syncAvatarProps().
// This window is visible only while the service is NOT active — the
// top-right card handles the active state. Media never opens anything.
//
// The layer surface stays fullscreen and mapped while the feature is on.
// Dragging moves a 96px child (not the Wayland surface). When the card
// is up the sprite fades and the input mask goes empty so the overlay
// cannot steal clicks or reset x/y by unmapping.
PanelWindow {
    id: root
    readonly property bool shown: root.avatarVisible && root.avatarEnabled
    // Stay mapped while enabled. Unmapping is what zeroed the sprite
    // and broke the next right-click (MouseArea / mask / pos all reset).
    visible: root.avatarEnabled
    screen: Quickshell.screens && Quickshell.screens.length > 0 ? Quickshell.screens[0] : null
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "dorian-voice-avatar"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    mask: Region {
        Region { item: root.shown ? body : null }
        Region { item: root.cardOpen ? card : null }
    }

    // Driven by the service. Defaults render the idle Mic-off avatar.
    property bool avatarVisible: true
    property bool avatarEnabled: true
    property string avatarState: "off"
    property string mode: "safe"
    property color modeColor: Qt.rgba(0.55, 0.58, 0.62, 1)
    property string statusText: "Mic off"
    property real voiceLevel: 0
    property string pipeline: "off"
    property string serviceState: "unknown"
    property string requestText: ""
    property string responseText: ""
    property bool hasMedia: false
    property string mediaTitle: ""
    property string mediaArtist: ""
    property bool blink: false
    property real bob: 0
    property bool reduceMotion: false
    property bool highContrast: false
    property bool busy: false
    property bool cardOpen: false
    property bool listening: false
    property var micBars: []
    property bool pendingActive: false
    property string pendingText: ""
    property bool mediaPlaying: false
    property string mediaUrl: ""

    // Child position inside the fullscreen surface. Persisted across reloads.
    property int posX: 60
    property int posY: 60
    property bool posReady: false

    readonly property int avatarSide: 96
    readonly property int maxX: Math.max(0, root.width - root.avatarSide)
    readonly property int maxY: Math.max(0, root.height - root.avatarSide)
    readonly property bool surfaceReady: root.screen &&
        root.width >= root.screen.width * 0.5 &&
        root.height >= root.screen.height * 0.5

    readonly property string toggleBin: (Quickshell.env("HOME") || "") + "/.local/share/jarvis/bin/jarvis-toggle"
    readonly property string posFile: (Quickshell.env("HOME") || "") + "/.local/state/jarvis/draggable-avatar-pos"

    function clampPos() {
        if (!root.surfaceReady) return
        root.posX = Math.max(0, Math.min(root.maxX, root.posX))
        root.posY = Math.max(0, Math.min(root.maxY, root.posY))
    }

    function placeBody() {
        if (dragArea.drag.active) return
        body.x = root.posX
        body.y = root.posY
    }

    function commitBody() {
        if (!root.surfaceReady || !dragArea.didDrag) return
        root.posX = Math.max(0, Math.min(root.maxX, Math.round(body.x)))
        root.posY = Math.max(0, Math.min(root.maxY, Math.round(body.y)))
        body.x = root.posX
        body.y = root.posY
        saveTimer.restart()
    }

    onPosXChanged: root.placeBody()
    onPosYChanged: root.placeBody()
    onWidthChanged: { root.clampPos(); root.placeBody() }
    onHeightChanged: { root.clampPos(); root.placeBody() }
    onShownChanged: {
        dragArea.didDrag = false
        if (root.shown) root.placeBody()
    }

    Timer {
        id: saveTimer
        interval: 500
        repeat: false
        onTriggered: {
            var cmd = ["sh", "-c",
                "mkdir -p \"$(dirname \"$1\")\" && printf '%s %s' \"" + root.posX + "\" \"" + root.posY + "\" > \"$1\"",
                "jarvis-avatar-pos-save", root.posFile]
            posSaveProc.command = cmd
            posSaveProc.running = true
        }
    }

    Process {
        id: posSaveProc
    }

    // Position restore goes through a process: XHR on file:// is disabled
    // in this shell build, so the defaults above show first and the saved
    // spot applies a moment later when the read lands.
    Process {
        id: posReadProc
        command: ["sh", "-c", "timeout 1 cat \"$1\" 2>/dev/null",
            "jarvis-avatar-pos-read", root.posFile]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                var parts = String(text || "").trim().split(/\s+/)
                if (parts.length === 2) {
                    var px = parseInt(parts[0], 10)
                    var py = parseInt(parts[1], 10)
                    if (!isNaN(px) && !isNaN(py)) {
                        root.posX = px
                        root.posY = py
                        root.clampPos()
                    }
                }
                root.posReady = true
                root.placeBody()
            }
        }
    }

    // Ask the service to re-open the card on demand (right-click).
    signal peekRequested()
    signal peekKept()
    signal playbackToggleRequested()
    signal mediaLinkRequested()
    signal pendingAnswered(bool confirmed)

    // Single left-click is delayed so a double-click never also toggles:
    // left = arm/step mode, right = peek the card, double-left = disarm.
    Timer {
        id: clickTimer
        interval: 260
        repeat: false
        onTriggered: {
            if (!dragArea.didDrag) toggleMode()
        }
    }

    Item {
        id: body
        width: root.avatarSide
        height: root.avatarSide
        opacity: root.shown ? 1 : 0
        scale: root.shown ? 1 : 0.6
        transformOrigin: Item.Center
        enabled: root.shown

        Behavior on opacity {
            enabled: !root.reduceMotion
            NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
        }
        Behavior on scale {
            enabled: !root.reduceMotion
            NumberAnimation { duration: 300; easing.type: Easing.OutBack }
        }

        GokuAvatar {
            anchors.fill: parent
            side: root.avatarSide
            state: root.avatarState
            mode: root.mode
            level: root.voiceLevel
            blink: root.blink
            bob: dragArea.pressed ? 0 : root.bob
            reduceMotion: root.reduceMotion
            highContrast: root.highContrast
        }

        MouseArea {
            id: dragArea
            anchors.fill: parent
            z: 10
            enabled: root.shown
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            hoverEnabled: true
            cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
            preventStealing: true

            drag.target: body
            drag.axis: Drag.XAndYAxis
            drag.threshold: 5
            drag.smoothed: false
            drag.minimumX: 0
            drag.maximumX: root.maxX
            drag.minimumY: 0
            drag.maximumY: root.maxY

            property bool didDrag: false

            onPressed: function(mouse) {
                didDrag = false
                if (mouse.button === Qt.RightButton) {
                    // clicked() is left-button only; fire peek on press so a
                    // hide/unmap cannot swallow the matching release.
                    mouse.accepted = true
                    root.peekRequested()
                }
            }
            onReleased: function(mouse) {
                if (mouse.button === Qt.LeftButton && didDrag)
                    root.commitBody()
            }
            onClicked: function(mouse) {
                if (didDrag || mouse.button !== Qt.LeftButton) return
                clickTimer.restart()
            }
            onDoubleClicked: {
                clickTimer.stop()
                disarm()
            }
        }

        Connections {
            target: dragArea.drag
            function onActiveChanged() {
                if (dragArea.drag.active) dragArea.didDrag = true
                else if (dragArea.didDrag) root.commitBody()
            }
        }
    }

    ConsoleCard {
        id: card
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 18
        anchors.rightMargin: 18
        opacity: root.cardOpen ? 1 : 0
        enabled: root.cardOpen
        avatarState: root.avatarState
        mode: root.mode
        modeColor: root.modeColor
        statusText: root.statusText
        voiceLevel: root.voiceLevel
        listening: root.listening
        micBars: root.micBars
        requestText: root.requestText
        responseText: root.responseText
        pendingActive: root.pendingActive
        pendingText: root.pendingText
        hasMedia: root.hasMedia
        mediaTitle: root.mediaTitle
        mediaArtist: root.mediaArtist
        mediaPlaying: root.mediaPlaying
        mediaUrl: root.mediaUrl
        Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
        onKeepPeek: root.peekKept()
        onToggleMode: root.toggleMode()
        onDisarm: root.disarm()
        onTogglePlayback: root.playbackToggleRequested()
        onOpenMediaLink: root.mediaLinkRequested()
        onAnswerPending: function(ok) { root.pendingAnswered(ok) }
    }

    // -- subtle idle life (local; off under reduced motion) -----------------
    Timer {
        id: blinkTimer
        interval: 3400
        running: root.shown && !root.reduceMotion
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
        running: root.shown && !root.reduceMotion && !dragArea.pressed
        repeat: true
        onTriggered: root.bob = root.bob === 0 ? 1.1 : 0
    }

    // -- controls (ask the daemon; it owns the outcome) ----------------------
    function toggleMode() {
        if (root.busy) return
        root.busy = true
        toggleProc.running = true
    }

    function disarm() {
        if (root.busy || root.serviceState !== "active") return
        root.busy = true
        stopProc.running = true
    }

    Process {
        id: toggleProc
        command: [root.toggleBin]
        onExited: root.busy = false
    }

    Process {
        id: stopProc
        command: ["systemctl", "--user", "stop", "jarvis"]
        onExited: root.busy = false
    }

    Component.onCompleted: {
        if (root.screen) {
            root.posX = Math.max(0, root.screen.width - 120)
            root.posY = 60
        }
        root.placeBody()
        posReadProc.running = true
    }
}
