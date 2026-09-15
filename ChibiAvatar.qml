import QtQuick

// Full-body chibi avatar for the Jarvis draggable window, drawn in QML.
// No external assets: every pixel below is code, so there is nothing to
// license and nothing to download. An original character with styling
// nods to classic super-saiyan-style shonen heroes -- huge spiky hair,
// teal eyes, orange gi over blue -- not a copy of any existing character.
//
// Hair carries the mode like a transformation: black when disarmed or in
// Safe, glowing gold in Basic (workspace), red in Full (privileged).
// The aura matches the mode; when disarmed everything else is muted gray.
//
// This is the big idle-stage character (~96px). The bar, panel and card
// keep using the head-only Avatar.qml, which stays legible at 16-28px.
// (That one keeps its mic badge; the floating character has none by
// request -- the bar icon still shows the mic state.)
//
// state: "off" | "idle" | "wake" | "listening" | "thinking" | "speaking"
//        | "tool" | "error"
// mode:  "safe" | "workspace" | "privileged"  (hair + aura)
// level: 0-100 playback amplitude (mouth opening; 0 unless audio plays)
Canvas {
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

  onStateChanged: requestPaint()
  onModeChanged: requestPaint()
  onLevelChanged: requestPaint()
  onBlinkChanged: requestPaint()
  onBobChanged: requestPaint()
  onReduceMotionChanged: requestPaint()
  onHighContrastChanged: requestPaint()
  Component.onCompleted: requestPaint()

  // Aura color carries the mode. "off" is always gray so the character
  // can never imply a live mic.
  readonly property color auraColor: {
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
    var u = s / 28  // unit: authored on a 28-unit grid, scales linearly
    var off = root.state === "off"
    var err = root.state === "error"
    var focused = root.state === "thinking" || root.state === "tool"
    var st = root.state

    // -- hair transformation ---------------------------------------------------
    var hair, hairShade, hairHi, glowing = false
    if (off || root.mode === "safe") {
      hair = "#23262f"; hairShade = "#101218"; hairHi = "#4b5265"  // base black
    } else if (root.mode === "privileged") {
      hair = "#e0342b"; hairShade = "#a31d15"; hairHi = "#ff9a86"  // red
    } else {
      hair = "#f4b90b"; hairShade = "#cf8c06"; hairHi = "#ffeda8"  // gold
      glowing = !err
    }

    var skin = off ? "#c9cfd8" : "#ffd9c2"
    var gi = off ? "#565f73" : "#e86a17"
    var giBlue = off ? "#4a5265" : "#2b4acb"
    var trimRed = off ? "#565f73" : "#c93a3a"
    var lace = off ? "#565f73" : "#e8dcc0"
    var iris = err ? "#b4232a" : (off ? "#31456e" : "#159e8c")
    var ink = err ? "#b4232a" : "#31456e"
    var mouthInk = "#7c2d3e"
    ctx.lineCap = "round"
    ctx.lineJoin = "round"

    function rgba(hex, a) {
      var r = parseInt(hex.slice(1, 3), 16)
      var g = parseInt(hex.slice(3, 5), 16)
      var b = parseInt(hex.slice(5, 7), 16)
      return "rgba(" + r + "," + g + "," + b + "," + a + ")"
    }
    function circle(x, y, r, fill) {
      ctx.beginPath()
      ctx.arc(x, y, r, 0, Math.PI * 2)
      ctx.fillStyle = fill
      ctx.fill()
    }
    function seg(x1, y1, x2, y2, w, color) {
      ctx.beginPath()
      ctx.moveTo(x1, y1)
      ctx.lineTo(x2, y2)
      ctx.lineWidth = w
      ctx.strokeStyle = color
      ctx.stroke()
    }

    var hcX = 14 * u, hcY = 9.5 * u, faceR = 4.7 * u

    // -- aura (static; the body bobs inside it) --------------------------------
    var auraR = st === "wake" ? 13.2 * u : 12.2 * u
    var ag = ctx.createRadialGradient(c, 14 * u, 2 * u, c, 14 * u, auraR)
    var auraBase = st === "wake" ? "#e3b341" : "#" + root.auraColor.toString().slice(1)
    ag.addColorStop(0, rgba(auraBase, 0.34))
    ag.addColorStop(0.7, rgba(auraBase, 0.14))
    ag.addColorStop(1, rgba(auraBase, 0))
    ctx.fillStyle = ag
    ctx.fillRect(0, 0, s, s)

    ctx.save()
    // Wake does a little jump; otherwise the idle bob applies.
    var jump = (st === "wake" && !root.reduceMotion) ? -1.2 * u : 0
    ctx.translate(0, root.bob + jump)

    // -- ground shadow -----------------------------------------------------------
    ctx.beginPath()
    ctx.ellipse(c, 26.6 * u, 6.0 * u, 1.1 * u, 0, 0, Math.PI * 2)
    ctx.fillStyle = "rgba(0,0,0,0.35)"
    ctx.fill()

    // -- boots --------------------------------------------------------------------
    function boot(x) {
      var w = 2.5 * u, top = 23.0 * u, bot = 26.2 * u
      ctx.beginPath()
      if (ctx.roundRect) ctx.roundRect(x, top, w, bot - top, 0.7 * u)
      else ctx.rect(x, top, w, bot - top)
      ctx.fillStyle = giBlue
      ctx.fill()
      seg(x + 0.2 * u, top + 0.9 * u, x + w - 0.2 * u, top + 0.9 * u, 0.55 * u, trimRed)
      circle(x + w / 2, top + 1.9 * u, 0.45 * u, lace)
    }
    boot(10.15 * u)
    boot(15.35 * u)

    // -- gi pants --------------------------------------------------------------------
    ctx.fillStyle = gi
    ctx.fillRect(10.4 * u, 19.8 * u, 2.0 * u, 3.6 * u)
    ctx.fillRect(15.6 * u, 19.8 * u, 2.0 * u, 3.6 * u)

    // -- torso: orange gi, blue undershirt V, blue belt ----------------------------------
    ctx.fillStyle = gi
    ctx.beginPath()
    ctx.moveTo(10.6 * u, 14.2 * u)
    ctx.lineTo(17.4 * u, 14.2 * u)
    ctx.lineTo(16.9 * u, 19.6 * u)
    ctx.lineTo(11.1 * u, 19.6 * u)
    ctx.closePath()
    ctx.fill()
    ctx.fillStyle = giBlue
    ctx.beginPath()
    ctx.moveTo(12.4 * u, 14.0 * u)
    ctx.lineTo(15.6 * u, 14.0 * u)
    ctx.lineTo(14.0 * u, 15.8 * u)
    ctx.closePath()
    ctx.fill()
    ctx.fillStyle = giBlue
    ctx.fillRect(10.8 * u, 18.6 * u, 6.4 * u, 1.5 * u)
    seg(16.6 * u, 20.1 * u, 17.6 * u, 21.2 * u, 0.7 * u, giBlue)
    seg(17.2 * u, 20.1 * u, 18.4 * u, 20.9 * u, 0.7 * u, giBlue)

    // -- arms --------------------------------------------------------------------------
    // Orange sleeve stub, skin forearm, blue wristband, fist.
    function arm(sx, sy, hx, hy) {
      var mx = (sx + hx) / 2, my = (sy + hy) / 2
      seg(sx, sy, mx, my, 2.3 * u, gi)
      seg(mx, my, hx, hy, 1.5 * u, skin)
      var t1 = 0.58, t2 = 0.86
      seg(mx + (hx - mx) * t1, my + (hy - my) * t1,
          mx + (hx - mx) * t2, my + (hy - my) * t2, 1.9 * u, giBlue)
      circle(hx, hy, 1.05 * u, skin)
    }
    var shL = [10.9 * u, 14.8 * u], shR = [17.1 * u, 14.8 * u]
    if (st === "wake") {
      arm(shL[0], shL[1], 8.6 * u, 7.6 * u)
      arm(shR[0], shR[1], 19.4 * u, 7.6 * u)
    } else if (st === "listening") {
      arm(shL[0], shL[1], 9.5 * u, 19.6 * u)
      arm(shR[0], shR[1], 19.3 * u, 10.6 * u)   // hand cupped to ear
    } else if (st === "tool") {
      arm(shL[0], shL[1], 16.5 * u, 17.4 * u)  // crossed arms
      arm(shR[0], shR[1], 11.5 * u, 17.4 * u)
    } else if (focused) {
      arm(shL[0], shL[1], 9.5 * u, 19.6 * u)
      arm(shR[0], shR[1], 19.1 * u, 7.2 * u)   // scratching head
    } else {
      arm(shL[0], shL[1], 9.5 * u, 19.6 * u)
      arm(shR[0], shR[1], 18.5 * u, 19.6 * u)
    }

    // -- neck + ears ----------------------------------------------------------------------
    ctx.fillStyle = skin
    ctx.fillRect(13.2 * u, 13.4 * u, 1.6 * u, 1.2 * u)
    circle(9.4 * u, 9.7 * u, 0.75 * u, skin)
    circle(18.6 * u, 9.7 * u, 0.75 * u, skin)

    // -- huge spiky hair ----------------------------------------------------------------------
    // Wide fan plus long side spikes; drawn behind the face. In Basic the
    // whole mane gets a translucent glow pass first.
    function spikes(tipScale, fill) {
      ctx.fillStyle = fill
      ctx.beginPath()
      var n = 12
      for (var i = 0; i <= n; i++) {
        var a = Math.PI * (0.90 + 1.20 * i / n)
        var tall = (i % 2) * 1.2 + ((i >= 5 && i <= 7) ? 1.0 : 0)
        var tipLen = (7.6 + tall) * tipScale * u
        var bxx = hcX + Math.cos(a) * 4.2 * u
        var byy = hcY + Math.sin(a) * 4.2 * u
        var txx = hcX + Math.cos(a) * tipLen
        var tyy = hcY + Math.sin(a) * tipLen
        var a2 = Math.PI * (0.90 + 1.20 * (i + 0.5) / n)
        var nxx = hcX + Math.cos(a2) * 4.7 * u
        var nyy = hcY + Math.sin(a2) * 4.7 * u
        if (i === 0) ctx.moveTo(bxx, byy)
        else ctx.lineTo(bxx, byy)
        ctx.lineTo(txx, tyy)
        ctx.lineTo(nxx, nyy)
      }
      ctx.closePath()
      ctx.fill()
    }
    if (glowing) {
      ctx.save()
      ctx.globalAlpha = 0.35
      spikes(1.22, hairHi)
      ctx.restore()
    }
    spikes(1.0, hairShade)
    // inner bright mass so the mane reads full, not hollow
    ctx.beginPath()
    ctx.arc(hcX, hcY - 1.2 * u, 4.9 * u, 0, Math.PI * 2)
    ctx.fillStyle = hair
    ctx.fill()

    // -- long side locks framing the face ----------------------------------------------------------
    ctx.fillStyle = hair
    ctx.beginPath()
    ctx.moveTo(9.9 * u, 7.6 * u)
    ctx.lineTo(10.9 * u, 7.9 * u)
    ctx.lineTo(10.1 * u, 13.6 * u)
    ctx.lineTo(9.2 * u, 13.2 * u)
    ctx.closePath()
    ctx.fill()
    ctx.beginPath()
    ctx.moveTo(18.1 * u, 7.6 * u)
    ctx.lineTo(17.1 * u, 7.9 * u)
    ctx.lineTo(17.9 * u, 13.6 * u)
    ctx.lineTo(18.8 * u, 13.2 * u)
    ctx.closePath()
    ctx.fill()

    // -- face -----------------------------------------------------------------------------------
    circle(hcX, hcY, faceR, skin)

    // -- fringe with long points ----------------------------------------------------------------------------
    ctx.fillStyle = hair
    ctx.beginPath()
    ctx.moveTo(hcX - faceR, hcY - 0.6 * u)
    ctx.quadraticCurveTo(hcX, hcY - 5.6 * u, hcX + faceR, hcY - 0.6 * u)
    ctx.quadraticCurveTo(hcX + 3.4 * u, hcY - 1.6 * u, hcX + 2.3 * u, hcY + 0.6 * u)
    ctx.quadraticCurveTo(hcX + 1.2 * u, hcY - 2.0 * u, hcX + 0.1 * u, hcY + 0.6 * u)
    ctx.quadraticCurveTo(hcX - 1.1 * u, hcY - 2.0 * u, hcX - 2.2 * u, hcY + 0.6 * u)
    ctx.quadraticCurveTo(hcX - 3.3 * u, hcY - 1.6 * u, hcX - faceR, hcY - 0.6 * u)
    ctx.fill()
    // shine streaks
    ctx.lineWidth = 0.65 * u
    ctx.strokeStyle = hairHi
    ctx.beginPath()
    ctx.moveTo(hcX - 2.9 * u, hcY - 2.4 * u)
    ctx.quadraticCurveTo(hcX - 1.9 * u, hcY - 3.1 * u, hcX - 0.7 * u, hcY - 3.0 * u)
    ctx.moveTo(hcX + 1.0 * u, hcY - 3.1 * u)
    ctx.quadraticCurveTo(hcX + 2.0 * u, hcY - 3.1 * u, hcX + 3.0 * u, hcY - 2.3 * u)
    ctx.stroke()

    // -- ahoge ------------------------------------------------------------------------------------------
    ctx.beginPath()
    ctx.moveTo(hcX + 0.4 * u, hcY - 5.4 * u)
    ctx.quadraticCurveTo(hcX + 2.2 * u, hcY - 7.2 * u, hcX + 3.9 * u, hcY - 6.2 * u)
    ctx.lineWidth = 0.75 * u
    ctx.strokeStyle = hair
    ctx.stroke()

    // -- brows (determined slant, steeper when concentrating) ---------------------------------------------------
    ctx.lineWidth = 0.7 * u
    ctx.strokeStyle = hairShade
    ctx.beginPath()
    if (focused || st === "tool") {
      ctx.moveTo(hcX - 3.3 * u, hcY - 1.7 * u)
      ctx.lineTo(hcX - 1.1 * u, hcY - 0.8 * u)
      ctx.moveTo(hcX + 3.3 * u, hcY - 1.7 * u)
      ctx.lineTo(hcX + 1.1 * u, hcY - 0.8 * u)
    } else {
      ctx.moveTo(hcX - 3.1 * u, hcY - 1.7 * u)
      ctx.lineTo(hcX - 1.2 * u, hcY - 1.2 * u)
      ctx.moveTo(hcX + 1.2 * u, hcY - 1.2 * u)
      ctx.lineTo(hcX + 3.1 * u, hcY - 1.7 * u)
    }
    ctx.stroke()

    // -- eyes ----------------------------------------------------------------------------------------------------------
    function eye(ex) {
      var ey = hcY + 0.8 * u
      if (err) {
        seg(ex - 1.2 * u, ey - 1.2 * u, ex + 1.2 * u, ey + 1.2 * u, 0.85 * u, ink)
        seg(ex + 1.2 * u, ey - 1.2 * u, ex - 1.2 * u, ey + 1.2 * u, 0.85 * u, ink)
        return
      }
      if (root.blink || off) {
        ctx.beginPath()
        ctx.moveTo(ex - 1.4 * u, ey)
        ctx.quadraticCurveTo(ex, ey + 0.55 * u, ex + 1.4 * u, ey)
        ctx.lineWidth = 0.75 * u
        ctx.strokeStyle = "#1c2233"
        ctx.stroke()
        return
      }
      ctx.beginPath()
      ctx.ellipse(ex, ey, 1.5 * u, 1.9 * u, 0, 0, Math.PI * 2)
      ctx.fillStyle = "#ffffff"
      ctx.fill()
      var look = focused ? -0.45 * u : 0
      circle(ex + look, ey + 0.3 * u, 0.95 * u, iris)
      circle(ex - 0.38 * u + look, ey - 0.25 * u, 0.4 * u, "#ffffff")
      circle(ex + 0.5 * u + look, ey + 0.8 * u, 0.2 * u, "#ffffff")
    }
    eye(hcX - 2.15 * u)
    eye(hcX + 2.15 * u)

    // -- blush -----------------------------------------------------------------------------------------------
    if (!off && !err) {
      ctx.strokeStyle = "rgba(244,114,182,0.7)"
      ctx.lineWidth = 0.7 * u
      ctx.beginPath()
      ctx.moveTo(hcX - 3.8 * u, hcY + 2.1 * u)
      ctx.lineTo(hcX - 2.5 * u, hcY + 2.1 * u)
      ctx.moveTo(hcX + 2.5 * u, hcY + 2.1 * u)
      ctx.lineTo(hcX + 3.8 * u, hcY + 2.1 * u)
      ctx.stroke()
    }

    // -- sweat drop while thinking ----------------------------------------------------------------------------------
    if (focused) {
      var sx = hcX + 4.9 * u, sy = hcY + 0.2 * u
      ctx.beginPath()
      ctx.moveTo(sx, sy - 1.4 * u)
      ctx.quadraticCurveTo(sx + 1.1 * u, sy + 0.4 * u, sx, sy + 1.0 * u)
      ctx.quadraticCurveTo(sx - 1.1 * u, sy + 0.4 * u, sx, sy - 1.4 * u)
      ctx.fillStyle = "#7dd3fc"
      ctx.fill()
    }

    // -- mouth ----------------------------------------------------------------------------------------------------------------
    // Speaking: opening follows the real playback amplitude. Anything else:
    // a small state glyph. Never open unless audio is actually playing.
    ctx.strokeStyle = mouthInk
    ctx.fillStyle = mouthInk
    ctx.lineWidth = 0.7 * u
    var my = hcY + 3.3 * u
    if (root.state === "speaking" && root.level > 4) {
      var open = (0.8 + root.level / 100 * 2.1) * u
      ctx.beginPath()
      ctx.ellipse(c, my + 0.4 * u, 1.25 * u, open, 0, 0, Math.PI * 2)
      ctx.fill()
    } else if (err) {
      ctx.beginPath()
      ctx.moveTo(c - 1.3 * u, my)
      ctx.quadraticCurveTo(c - 0.65 * u, my - 0.8 * u, c, my)
      ctx.quadraticCurveTo(c + 0.65 * u, my + 0.8 * u, c + 1.3 * u, my)
      ctx.stroke()
    } else if (focused || st === "tool") {
      seg(c - 1.15 * u, my, c + 1.15 * u, my, 0.7 * u, mouthInk)
    } else if (off) {
      seg(c - 0.95 * u, my, c + 0.95 * u, my, 0.7 * u, mouthInk)
    } else if (st === "wake") {
      ctx.beginPath()
      ctx.arc(c, my - 0.4 * u, 1.35 * u, 0.15 * Math.PI, 0.85 * Math.PI)
      ctx.fill()
    } else {
      // confident smirk
      ctx.beginPath()
      ctx.arc(c - 0.15 * u, my - 0.9 * u, 1.3 * u, 0.3 * Math.PI, 0.85 * Math.PI)
      ctx.stroke()
    }

    ctx.restore()
  }
}
