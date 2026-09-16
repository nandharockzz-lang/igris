import QtQuick

// Jarvis minibar card. Hosted inside the draggable avatar's fullscreen
// overlay so it cannot lose a z-order fight with a second layer surface.
Rectangle {
  id: root

  signal keepPeek()
  signal toggleMode()
  signal disarm()
  signal togglePlayback()
  signal openMediaLink()
  signal answerPending(bool confirmed)

  property string avatarState: "off"
  property string mode: "safe"
  property color modeColor: "#58a6ff"
  property string statusText: "Mic off"
  property real voiceLevel: 0
  property bool listening: false
  property var micBars: []
  property string requestText: ""
  property string responseText: ""
  property bool pendingActive: false
  property string pendingText: ""
  property bool hasMedia: false
  property string mediaTitle: ""
  property string mediaArtist: ""
  property bool mediaPlaying: false
  property string mediaUrl: ""

  readonly property bool hasText: root.requestText !== "" || root.responseText !== ""

  width: 360
  height: contentColumn.implicitHeight + 28
  radius: 18
  color: Qt.rgba(0.04, 0.055, 0.09, 0.96)
  border.width: 1
  border.color: Qt.rgba(root.modeColor.r, root.modeColor.g, root.modeColor.b, 0.65)

  MouseArea {
    id: cardMouse
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    onPressed: root.keepPeek()
    onClicked: root.keepPeek()
  }

  Column {
    id: contentColumn
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
      visible: root.pendingActive
      width: parent.width
      height: pendingColumn.implicitHeight + 18
      radius: 10
      color: Qt.rgba(0.95, 0.55, 0.20, 0.14)
      border.width: 1
      border.color: "#f0883e"

      Column {
        id: pendingColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 9
        spacing: 8

        Text {
          text: "Confirm action"
          color: "#f0883e"
          font.bold: true
          font.pixelSize: 11
          font.letterSpacing: 1.5
          width: parent.width
        }
        Text {
          text: root.pendingText
          color: "#f0f6fc"
          font.pixelSize: 13
          wrapMode: Text.WordWrap
          width: parent.width
        }
        Row {
          width: parent.width
          spacing: 8

          Rectangle {
            id: confirmButton
            width: (parent.width - 8) / 2
            height: 34
            radius: 8
            color: "#3fb950"
            scale: confirmMouse.pressed ? 0.94 : 1.0
            Behavior on scale { NumberAnimation { duration: 90 } }
            Text {
              anchors.centerIn: parent
              text: "Confirm"
              color: "#081018"
              font.bold: true
              font.pixelSize: 13
            }
            MouseArea {
              id: confirmMouse
              anchors.fill: parent
              acceptedButtons: Qt.LeftButton
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: { root.keepPeek(); root.answerPending(true) }
            }
          }

          Rectangle {
            id: denyButton
            width: parent.width - confirmButton.width - 8
            height: 34
            radius: 8
            color: Qt.rgba(1, 1, 1, 0.12)
            border.width: 1
            border.color: "#8b949e"
            scale: denyMouse.pressed ? 0.94 : 1.0
            Behavior on scale { NumberAnimation { duration: 90 } }
            Text {
              anchors.centerIn: parent
              text: "Deny"
              color: "#f0f6fc"
              font.pixelSize: 13
            }
            MouseArea {
              id: denyMouse
              anchors.fill: parent
              acceptedButtons: Qt.LeftButton
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: { root.keepPeek(); root.answerPending(false) }
            }
          }
        }
      }
    }

    Rectangle {
      visible: root.hasMedia
      width: parent.width
      height: 46
      radius: 10
      color: Qt.rgba(1, 1, 1, 0.07)

      Item {
        anchors.left: parent.left
        anchors.leftMargin: 10
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width - 54
        height: titleCol.implicitHeight

        Column {
          id: titleCol
          width: parent.width
          spacing: 2
          Text {
            text: root.mediaTitle
            color: titleMouse.containsMouse ? root.modeColor : "#f0f6fc"
            font.pixelSize: 11
            font.underline: titleMouse.containsMouse
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
        MouseArea {
          id: titleMouse
          anchors.fill: parent
          enabled: root.hasMedia
          hoverEnabled: true
          cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
          z: 3
          onClicked: { root.keepPeek(); root.openMediaLink() }
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
        scale: playMouse.pressed ? 0.9 : 1.0
        Behavior on scale { NumberAnimation { duration: 90 } }
        Text {
          anchors.centerIn: parent
          text: root.mediaPlaying ? "󰏤" : "󰐊"
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
