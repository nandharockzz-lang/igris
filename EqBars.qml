import QtQuick

// 12-bar voice-reception EQ. Bars render daemon-published gated amplitude
// buckets (transient, never stored); smoothing keeps motion calm and the
// whole strip dims the moment listening ends. Honors reduced motion.
Row {
  id: root

  property var bars: []
  property bool active: false
  property bool reduceMotion: false
  property bool highContrast: false
  property color foreground: "#8b949e"
  property color accent: "#3fb950"
  property real maxHeight: 36
  property real barWidth: 4
  property real radius: 2

  spacing: 3

  Repeater {
    model: 12
    Rectangle {
      property real target: {
        if (!root.active || index >= root.bars.length) return 0
        var v = Number(root.bars[index]) || 0
        return Math.max(0, Math.min(100, v)) / 100 * root.maxHeight
      }
      width: root.barWidth
      height: Math.max(2, target)
      radius: root.radius
      color: (Number(root.bars[index]) || 0) > 62 ? root.accent : root.foreground
      opacity: root.active ? (root.highContrast ? 1.0 : 0.9) : 0.25

      Behavior on height {
        enabled: !root.reduceMotion
        NumberAnimation { duration: 90; easing.type: Easing.OutCubic }
      }
      Behavior on opacity {
        enabled: !root.reduceMotion
        NumberAnimation { duration: 160 }
      }
    }
  }
}
