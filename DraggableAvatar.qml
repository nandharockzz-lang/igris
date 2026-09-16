import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

// Idle avatar for Jarvis. Pure view: service.qml owns every daemon value
// (pipeline, text, media) and pushes it here via syncAvatarProps().
// This window is visible only while the service is NOT active — the
// top-right card handles the active state. Media never opens anything.
//
// Positioning: a PanelWindow is a top-level layer surface, so it cannot be
// moved with drag.target. Instead it is anchored top-left and dragged by
// updating its margins, which is also what gets persisted.
PanelWindow {
    id: root
    // Stay mapped through the fade-out so hide animates instead of popping.
    readonly property bool shown: root.avatarVisible && root.avatarEnabled
    visible: root.shown || hideTimer.running
    screen: Quickshell.screens && Quickshell.screens.length > 0 ? Quickshell.screens[0] : null
    anchors { top: true; left: true }
    margins { left: root.posX; top: root.posY }
    implicitWidth: 96
    implicitHeight: 96
    color: "transparent"
    WlrLayershell.namespace: "dorian-voice-avatar"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

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

    // Top-left margins = avatar position. Persisted across reloads.
    property int posX: 60
    property int posY: 60

    Timer {
        id: hideTimer
        interval: 240
        repeat: false
    }

    onShownChanged: {
        if (!root.shown && !root.reduceMotion) hideTimer.restart()
        else hideTimer.stop()
    }

    readonly property string toggleBin: (Quickshell.env("HOME") || "") + "/.local/share/jarvis/bin/jarvis-toggle"
    readonly property string posFile: (Quickshell.env("HOME") || "") + "/.local/state/jarvis/draggable-avatar-pos"

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
                        root.posX = Math.max(0, px)
                        root.posY = Math.max(0, py)
                    }
                }
            }
        }
    }

    // Ask the service to re-open the card on demand (right-click).
    signal peekRequested()

    // Single left-click is delayed so a double-click never also toggles:
    // left = arm/step mode, right = peek the card, double-left = disarm.
    Timer {
        id: clickTimer
        interval: 260
        repeat: false
        onTriggered: {
            if (dragArea.draggedPx <= 5) toggleMode()
        }
    }

    MouseArea {
        id: dragArea
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
        hoverEnabled: true

        property point pressOffset
        property real draggedPx: 0

        onPressed: function(mouse) {
            pressOffset = Qt.point(mouse.x, mouse.y)
            draggedPx = 0
        }
        onPositionChanged: function(mouse) {
            if (!pressed) return
            var dx = mouse.x - pressOffset.x
            var dy = mouse.y - pressOffset.y
            if (dx !== 0 || dy !== 0) {
                draggedPx += Math.abs(dx) + Math.abs(dy)
                root.posX = Math.max(0, root.posX + Math.round(dx))
                root.posY = Math.max(0, root.posY + Math.round(dy))
                saveTimer.restart()
            }
        }
        onClicked: function(mouse) {
            if (draggedPx > 5) return
            if (mouse.button === Qt.RightButton) root.peekRequested()
            else clickTimer.restart()
        }
        onDoubleClicked: {
            clickTimer.stop()
            disarm()
        }
    }

    // -- subtle idle life (local; off under reduced motion) -----------------
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
        posReadProc.running = true
    }

    // -- full-body chibi character (no box; floats on the layer) ------------------
    Item {
        id: content
        anchors.fill: parent
        opacity: root.shown ? 1 : 0
        scale: root.shown ? 1 : 0.6

        Behavior on opacity {
            enabled: !root.reduceMotion
            NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
        }
        Behavior on scale {
            enabled: !root.reduceMotion
            NumberAnimation { duration: 300; easing.type: Easing.OutBack }
        }

        ChibiAvatar {
            anchors.fill: parent
            side: 96 // matches the window above; avoids a width<->side loop
            state: root.avatarState
            mode: root.mode
            level: root.voiceLevel
            blink: root.blink
            bob: root.bob
            reduceMotion: root.reduceMotion
            highContrast: root.highContrast
        }
    }
}
