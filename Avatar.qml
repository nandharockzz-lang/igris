import QtQuick

// Original chibi-style avatar for the Jarvis voice widget, drawn in QML.
// No external assets: every pixel below is code, so there is nothing to
// license and nothing to download. An original character with styling
// nods to classic super-saiyan-style shonen heroes -- tall golden spikes,
// teal eyes, orange gi over blue -- not a copy of any existing character.
// States are driven by daemon-published signals only (pipeline, playback
// amplitude, wake marker) -- the mouth moves with real Piper audio and
// never otherwise.
//
// state: "off" | "idle" | "wake" | "listening" | "thinking" | "speaking"
//        | "tool" | "error"
// mode:  "safe" | "workspace" | "privileged"  (ring color)
// level: 0-100 playback amplitude (mouth opening; 0 unless audio plays)
Canvas {
  id: root

  property real side: 28
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

  onStateChanged: requestPaint()
  onModeChanged: requestPaint()
  onLevelChanged: requestPaint()
  onBlinkChanged: requestPaint()
  onBobChanged: requestPaint()
  onReduceMotionChanged: requestPaint()
  onHighContrastChanged: requestPaint()
  Component.onCompleted: requestPaint()

  // Ring color carries the mode. Safe is calm blue, Basic (workspace) green,
  // Full (privileged) orange, error red. Never green/orange while disarmed:
  // "off" always renders gray, so the avatar can never imply a live mic.
  readonly property color ringColor: {
    if (root.state === "error") return root.highContrast ? "#ff7b72" : "#f85149"
    if (root.state === "off") return root.highContrast ? "#8b949e" : "#6e7681"
    if (root.mode === "privileged") return root.highContrast ? "#ffa657" : "#f0883e"
    if (root.mode === "workspace") return root.highContrast ? "#7ee787" : "#3fb950"
    return root.highContrast ? "#79c0ff" : "#58a6ff"
  }

  onPaint: {
    var ctx = getContext("2d")
    var s = root.side
    if (s <= 0) return
    ctx.clearRect(0, 0, s, s)
    var c = s / 2
    var u = s / 28  // unit: drawing authored at 28px, scales linearly
    var lw = root.highContrast ? 2.6 * u : 1.8 * u
    var off = root.state === "off"
    var err = root.state === "error"
    var skin = off ? "#c9cfd8" : "#ffd9c2"
    var hair = off ? "#3a4356" : "#f4b90b"
    var hairShade = off ? "#2e3542" : "#cf8c06"
    var hairHi = off ? "#565f73" : "#ffeda8"
    var ink = err ? "#b4232a" : "#31456e"
    var iris = err ? "#b4232a" : (off ? "#31456e" : "#159e8c")

    // -- glow ring ---------------------------------------------------------
    ctx.beginPath()
    ctx.arc(c, c + root.bob, 12.4 * u, 0, Math.PI * 2)
    ctx.lineWidth = (root.state === "wake" ? lw + 1.6 * u : lw)
    ctx.strokeStyle = root.state === "wake" ? "#e3b341" : root.ringColor
    ctx.stroke()

    ctx.save()
    ctx.translate(0, root.bob)

    // -- spiky back hair ----------------------------------------------------
    // Tall super-saiyan-style tufts fanning out behind the head, darker
    // shade as the back layer; the bright fringe goes on top below.
    ctx.fillStyle = hairShade
    ctx.beginPath()
    var spikes = 9
    for (var i = 0; i <= spikes; i++) {
      var a = Math.PI * (1.02 + 0.96 * i / spikes)
      var tipLen = 11.2 + (i % 2) * 1.4 + (i === 4 || i === 5 ? 0.8 : 0)
      var bx = c + Math.cos(a) * 8.2 * u
      var by = c + Math.sin(a) * 8.2 * u
      var tx = c + Math.cos(a) * tipLen * u
      var ty = c + Math.sin(a) * tipLen * u
      var a2 = Math.PI * (1.02 + 0.96 * (i + 0.5) / spikes)
      var nx = c + Math.cos(a2) * 8.6 * u
      var ny = c + Math.sin(a2) * 8.6 * u
      if (i === 0) ctx.moveTo(bx, by)
      else ctx.lineTo(bx, by)
      ctx.lineTo(tx, ty)
      ctx.lineTo(nx, ny)
    }
    ctx.closePath()
    ctx.fill()

    // -- gi: orange collar, blue undershirt V ---------------------------------
    ctx.fillStyle = off ? "#4a5265" : "#e86a17"
    ctx.beginPath()
    ctx.moveTo(c - 5.6 * u, c + 11.6 * u)
    ctx.quadraticCurveTo(c, c + 5.4 * u, c + 5.6 * u, c + 11.6 * u)
    ctx.lineTo(c + 4.2 * u, c + 12.6 * u)
    ctx.quadraticCurveTo(c, c + 8.0 * u, c - 4.2 * u, c + 12.6 * u)
    ctx.closePath()
    ctx.fill()
    ctx.fillStyle = off ? "#565f73" : "#2b4acb"
    ctx.beginPath()
    ctx.moveTo(c - 2.4 * u, c + 7.4 * u)
    ctx.lineTo(c + 2.4 * u, c + 7.4 * u)
    ctx.lineTo(c, c + 10.4 * u)
    ctx.closePath()
    ctx.fill()

    // -- big chibi face ------------------------------------------------------
    ctx.beginPath()
    ctx.arc(c, c + 0.6 * u, 7.8 * u, 0, Math.PI * 2)
    ctx.fillStyle = skin
    ctx.fill()

    // -- fringe over the forehead --------------------------------------------
    ctx.fillStyle = hair
    ctx.beginPath()
    ctx.moveTo(c - 7.8 * u, c - 0.6 * u)
    ctx.quadraticCurveTo(c, c - 9.8 * u, c + 7.8 * u, c - 0.6 * u)
    ctx.quadraticCurveTo(c + 5.2 * u, c - 3.0 * u, c + 3.4 * u, c - 0.8 * u)
    ctx.quadraticCurveTo(c + 1.8 * u, c - 3.6 * u, c + 0.2 * u, c - 0.8 * u)
    ctx.quadraticCurveTo(c - 1.6 * u, c - 3.6 * u, c - 3.2 * u, c - 0.8 * u)
    ctx.quadraticCurveTo(c - 5.0 * u, c - 3.0 * u, c - 7.8 * u, c - 0.6 * u)
    ctx.fill()

    // shine streaks on the fringe
    ctx.lineWidth = 1.0 * u
    ctx.lineCap = "round"
    ctx.strokeStyle = hairHi
    ctx.beginPath()
    ctx.moveTo(c - 4.6 * u, c - 3.4 * u)
    ctx.quadraticCurveTo(c - 3.0 * u, c - 4.6 * u, c - 1.2 * u, c - 4.4 * u)
    ctx.moveTo(c + 1.6 * u, c - 4.6 * u)
    ctx.quadraticCurveTo(c + 3.2 * u, c - 4.6 * u, c + 4.8 * u, c - 3.2 * u)
    ctx.stroke()

    // -- ahoge (cowlick) -------------------------------------------------------
    ctx.beginPath()
    ctx.moveTo(c + 0.6 * u, c - 8.2 * u)
    ctx.quadraticCurveTo(c + 3.6 * u, c - 11.0 * u, c + 6.2 * u, c - 9.4 * u)
    ctx.lineWidth = 1.2 * u
    ctx.strokeStyle = hair
    ctx.stroke()

    // -- brows (determined slant, steeper when concentrating) -----------------------
    var focused = root.state === "thinking" || root.state === "tool"
    ctx.lineWidth = 1.1 * u
    ctx.strokeStyle = hairShade
    ctx.beginPath()
    if (focused) {
      ctx.moveTo(c - 5.2 * u, c - 2.6 * u)
      ctx.lineTo(c - 1.8 * u, c - 1.2 * u)
      ctx.moveTo(c + 5.2 * u, c - 2.6 * u)
      ctx.lineTo(c + 1.8 * u, c - 1.2 * u)
    } else {
      ctx.moveTo(c - 5.0 * u, c - 2.6 * u)
      ctx.lineTo(c - 2.0 * u, c - 1.8 * u)
      ctx.moveTo(c + 2.0 * u, c - 1.8 * u)
      ctx.lineTo(c + 5.0 * u, c - 2.6 * u)
    }
    ctx.stroke()

    // -- big chibi eyes ----------------------------------------------------------
    function eye(ex) {
      var ey = c + 1.2 * u
      if (err) {
        // >< squeezed-shut eyes
        ctx.beginPath()
        ctx.moveTo(ex - 1.9 * u, ey - 1.9 * u)
        ctx.lineTo(ex + 1.9 * u, ey + 1.9 * u)
        ctx.moveTo(ex + 1.9 * u, ey - 1.9 * u)
        ctx.lineTo(ex - 1.9 * u, ey + 1.9 * u)
        ctx.lineWidth = 1.3 * u
        ctx.strokeStyle = ink
        ctx.stroke()
        return
      }
      if (root.blink || off) {
        ctx.beginPath()
        ctx.moveTo(ex - 2.2 * u, ey)
        ctx.quadraticCurveTo(ex, ey + 0.9 * u, ex + 2.2 * u, ey)
        ctx.lineWidth = 1.2 * u
        ctx.strokeStyle = "#1c2233"
        ctx.stroke()
        return
      }
      // white
      ctx.beginPath()
      ctx.ellipse(ex, ey, 2.4 * u, 3.0 * u, 0, 0, Math.PI * 2)
      ctx.fillStyle = "#ffffff"
      ctx.fill()
      // iris, glancing aside while thinking
      var look = focused ? -0.7 * u : 0
      ctx.beginPath()
      ctx.arc(ex + look, ey + 0.5 * u, 1.5 * u, 0, Math.PI * 2)
      ctx.fillStyle = iris
      ctx.fill()
      // double highlight: the anime sparkle
      ctx.beginPath()
      ctx.arc(ex - 0.6 * u + look, ey - 0.4 * u, 0.65 * u, 0, Math.PI * 2)
      ctx.fillStyle = "#ffffff"
      ctx.fill()
      ctx.beginPath()
      ctx.arc(ex + 0.8 * u + look, ey + 1.3 * u, 0.32 * u, 0, Math.PI * 2)
      ctx.fill()
    }
    eye(c - 3.4 * u)
    eye(c + 3.4 * u)

    // -- blush ---------------------------------------------------------------------
    if (!off && !err) {
      ctx.strokeStyle = "rgba(244,114,182,0.7)"
      ctx.lineWidth = 1.1 * u
      ctx.lineCap = "round"
      ctx.beginPath()
      ctx.moveTo(c - 6.0 * u, c + 3.4 * u)
      ctx.lineTo(c - 4.0 * u, c + 3.4 * u)
      ctx.moveTo(c + 4.0 * u, c + 3.4 * u)
      ctx.lineTo(c + 6.0 * u, c + 3.4 * u)
      ctx.stroke()
    }

    // -- anime sweat drop while thinking ----------------------------------------------
    if (root.state === "thinking" || root.state === "tool") {
      var sx = c + 7.6 * u, sy = c + 0.4 * u
      ctx.beginPath()
      ctx.moveTo(sx, sy - 2.2 * u)
      ctx.quadraticCurveTo(sx + 1.7 * u, sy + 0.6 * u, sx, sy + 1.6 * u)
      ctx.quadraticCurveTo(sx - 1.7 * u, sy + 0.6 * u, sx, sy - 2.2 * u)
      ctx.fillStyle = "#7dd3fc"
      ctx.fill()
    }

    // -- mouth ----------------------------------------------------------------------
    // Speaking: opening follows the real playback amplitude. Anything else:
    // a small state glyph. Never open unless audio is actually playing.
    ctx.strokeStyle = "#7c2d3e"
    ctx.fillStyle = "#7c2d3e"
    ctx.lineWidth = 1.1 * u
    ctx.lineCap = "round"
    var my = c + 5.2 * u
    if (root.state === "speaking" && root.level > 4) {
      var open = (1.2 + root.level / 100 * 3.4) * u
      ctx.beginPath()
      ctx.ellipse(c, my + 0.6 * u, 2.0 * u, open, 0, 0, Math.PI * 2)
      ctx.fill()
    } else if (err) {
      // wavy distressed mouth
      ctx.beginPath()
      ctx.moveTo(c - 2.0 * u, my)
      ctx.quadraticCurveTo(c - 1.0 * u, my - 1.2 * u, c, my)
      ctx.quadraticCurveTo(c + 1.0 * u, my + 1.2 * u, c + 2.0 * u, my)
      ctx.stroke()
    } else if (root.state === "thinking" || root.state === "tool") {
      ctx.beginPath()
      ctx.moveTo(c - 1.8 * u, my)
      ctx.lineTo(c + 1.8 * u, my)
      ctx.stroke()
    } else if (off) {
      ctx.beginPath()
      ctx.moveTo(c - 1.5 * u, my)
      ctx.lineTo(c + 1.5 * u, my)
      ctx.stroke()
    } else if (root.state === "wake") {
      // open happy smile
      ctx.beginPath()
      ctx.arc(c, my - 0.6 * u, 2.1 * u, 0.15 * Math.PI, 0.85 * Math.PI)
      ctx.fill()
    } else {
      // confident smirk
      ctx.beginPath()
      ctx.arc(c - 0.2 * u, my - 1.4 * u, 2.0 * u, 0.3 * Math.PI, 0.85 * Math.PI)
      ctx.stroke()
    }

    ctx.restore()

    // -- mic badge ---------------------------------------------------------------------
    // Bottom-right: green mic while recording, gray slashed mic when the mic
    // is off, red "!" on error. The badge is the honest mic indicator.
    var bx = c + 8.6 * u, by = c + 8.6 * u, br = 4.6 * u
    var badge = "#6e7681"
    if (root.state === "listening" || root.state === "wake") badge = "#3fb950"
    else if (err) badge = "#f85149"
    else if (!off) badge = root.ringColor
    ctx.beginPath()
    ctx.arc(bx, by, br, 0, Math.PI * 2)
    ctx.fillStyle = badge
    ctx.fill()
    ctx.fillStyle = "#ffffff"
    if (err) {
      ctx.font = "bold " + (6 * u) + "px sans-serif"
      ctx.textAlign = "center"
      ctx.textBaseline = "middle"
      ctx.fillText("!", bx, by + 0.5 * u)
    } else if (off) {
      ctx.fillRect(bx - 1 * u, by - 2.4 * u, 2 * u, 3.4 * u)
      ctx.beginPath()
      ctx.arc(bx, by + 1.6 * u, 1.8 * u, 0.15 * Math.PI, 0.85 * Math.PI)
      ctx.lineWidth = 1.2 * u
      ctx.strokeStyle = "#ffffff"
      ctx.stroke()
      ctx.beginPath()
      ctx.moveTo(bx - 3 * u, by + 3 * u)
      ctx.lineTo(bx + 3 * u, by - 3 * u)
      ctx.lineWidth = 1.4 * u
      ctx.strokeStyle = "#ffffff"
      ctx.stroke()
    } else {
      ctx.fillRect(bx - 1 * u, by - 2.6 * u, 2 * u, 3.6 * u)
      ctx.beginPath()
      ctx.arc(bx, by + 1.2 * u, 1.9 * u, 0.15 * Math.PI, 0.85 * Math.PI)
      ctx.lineWidth = 1.2 * u
      ctx.strokeStyle = "#ffffff"
      ctx.stroke()
      if (root.state === "listening") {
        ctx.beginPath()
        ctx.arc(bx, by, br - 1 * u, 0, Math.PI * 2)
        ctx.lineWidth = 1 * u
        ctx.strokeStyle = "rgba(255,255,255,0.85)"
        ctx.stroke()
      }
    }
  }
}
