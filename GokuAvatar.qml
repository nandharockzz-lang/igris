import QtQuick
import Quickshell

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

  // Nested types often resolve Qt.resolvedUrl() against the shell's base
  // URL, not this file — so "assets/goku-idle.png" 404s and the canvas
  // fallback stays on screen. Build a file:// URL from this document, and
  // fall back to the installed plugin path if that probe escapes the tree.
  readonly property url spriteSource: {
    var file = "goku-" + root.imageKey + ".png"
    var probe = String(Qt.resolvedUrl("assets/goku-idle.png"))
    var slash = probe.lastIndexOf("/")
    var dir = slash >= 0 ? probe.substring(0, slash + 1) : ""
    if (dir.indexOf("file:") === 0 &&
        (dir.indexOf("/dorian.voice/") !== -1 || dir.indexOf("/igris/") !== -1))
      return dir + file
    var home = Quickshell.env("HOME") || ""
    return "file://" + home + "/.config/omarchy/plugins/dorian.voice/assets/" + file
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

  readonly property bool spriteReady: sprite.status === Image.Ready

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

  // Fallback character: hidden once the PNG is actually on screen. The
  // sprites are mostly transparent, so leaving this mounted underneath
  // made the canvas drawing show through the Goku PNG.
  ChibiAvatar {
    anchors.fill: parent
    visible: !root.spriteReady
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
    anchors.margins: 4
    fillMode: Image.PreserveAspectFit
    smooth: true
    mipmap: true
    asynchronous: false
    source: root.spriteSource
    visible: root.spriteReady
    opacity: root.state === "off" ? 0.7 : 1.0
    sourceSize: Qt.size(Math.max(1, width) * 2, Math.max(1, height) * 2)
    // Idle float; speaking pulse. Both frozen under reduced motion
    // because the host stops the bob timer and liveScale pins to 1.
    transform: Translate { y: -root.bob }
    scale: root.liveScale
    onStatusChanged: {
      if (status === Image.Error)
        console.warn("[dorian.voice] goku sprite failed:", source)
    }
  }
}
