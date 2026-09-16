import QtQuick

// Goku-style chibi avatar for the draggable window and the card.
// Shows user-supplied PNGs from assets/ (goku-idle/listening/thinking/
// speaking.png); the canvas ChibiAvatar stays mounted underneath as a
// guaranteed fallback, so a missing or broken file degrades to the drawn
// character instead of an empty box.
//
// Same property interface as ChibiAvatar, so hosts swap one line:
//   state: "off" | "idle" | "wake" | "listening" | "thinking" |
//          "speaking" | "tool" | "error"
//   mode:  "safe" | "workspace" | "privileged"  (ring color)
//   level: 0-100 playback amplitude (speaking pulse; still under
//          reduced motion)
// Mapping: off/idle -> goku-idle, wake/listening -> goku-listening,
// thinking/tool -> goku-thinking, speaking/error -> goku-speaking.
Item {
  id: root

  property real side: 96
  property string state: "off"
  property string mode: "safe"
  property real level: 0
  property bool reduceMotion: false
  property bool highContrast: false
  property bool blink: false
  property real bob: 0

  width: side
  height: side
  implicitWidth: side
  implicitHeight: side

  readonly property string imageKey: {
    var st = root.state
    if (st === "listening" || st === "wake") return "listening"
    if (st === "thinking" || st === "tool") return "thinking"
    if (st === "speaking" || st === "error") return "speaking"
    return "idle"
  }

  // Mode ring: the transformation signal for the image path. Gray while
  // off so the character can never imply a live mic.
  readonly property color ringColor: {
    if (root.state === "error") return root.highContrast ? "#ff7b72" : "#f85149"
    if (root.state === "off") return root.highContrast ? "#8b949e" : "#6e7681"
    if (root.mode === "privileged") return root.highContrast ? "#ffa657" : "#f0883e"
    if (root.mode === "workspace") return root.highContrast ? "#7ee787" : "#3fb950"
    return root.highContrast ? "#79c0ff" : "#58a6ff"
  }

  // Still image under reduced motion or when idle: bob comes from the
  // host timer, which is already gated on !reduceMotion.
  readonly property real liveScale: {
    if (root.reduceMotion) return 1.0
    if (root.state === "speaking")
      return 1.0 + Math.max(0, Math.min(100, root.level)) / 100 * 0.05
    return 1.0
  }

  Rectangle {
    id: ring
    anchors.fill: parent
    anchors.margins: 2
    radius: width / 2
    color: "transparent"
    border.color: root.ringColor
    border.width: root.highContrast ? 4 : 2
    opacity: root.state === "off" ? 0.45 : 0.9
  }

  // Fallback character: always mounted, covered when the PNG is Ready.
  ChibiAvatar {
    anchors.fill: parent
    side: root.side
    state: root.state
    mode: root.mode
    level: root.level
    blink: root.blink
    bob: root.bob
    reduceMotion: root.reduceMotion
    highContrast: root.highContrast
  }

  Image {
    id: sprite
    anchors.fill: parent
    anchors.margins: 6
    fillMode: Image.PreserveAspectFit
    smooth: true
    mipmap: true
    cache: false
    source: Qt.resolvedUrl("assets/goku-" + root.imageKey + ".png")
    visible: status === Image.Ready
    // Idle float; speaking pulse. Both frozen under reduced motion
    // because the host stops the bob timer and liveScale pins to 1.
    transform: Translate { y: -root.bob }
    scale: root.liveScale
  }
}
