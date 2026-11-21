import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts

Pane {
    id: root
    property alias selectedIndex: tracks.currentIndex
    property bool canPreview: false
    signal previewRequested(string source)

    padding: 12

    ColumnLayout {
        anchors.fill: parent
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            TextField {
                id: source
                Layout.fillWidth: true
                objectName: "queuePlaylistSource"
                placeholderText: "Paste a public Spotify, Deezer, or YouTube playlist URL"
                Accessible.name: "Public playlist URL"
                onAccepted: if (preview.enabled) preview.clicked()
            }
            Button {
                id: preview
                objectName: "queuePreviewButton"
                text: "Preview playlist"
                enabled: root.canPreview && source.text.trim().length > 0
                onClicked: root.previewRequested(source.text.trim())
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 16

            ListView {
                id: tracks
                objectName: "queueTracks"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                activeFocusOnTab: true
                Accessible.name: "Queue of requested tracks"
                keyNavigationWraps: false
                model: queueModel
                currentIndex: -1
                ScrollBar.vertical: ScrollBar { id: trackScrollBar }
                delegate: Basic.ItemDelegate {
                    id: track
                    required property int index
                    required property string trackName
                    required property string trackSource
                    required property string stage
                    required property string peer
                    required property string peerQueue
                    required property int percent
                    required property string readout
                    required property string notes
                    required property string nextStep

                    width: tracks.width - trackScrollBar.width
                    highlighted: ListView.isCurrentItem
                    Accessible.name: trackName + ", " + stage + ", " + readout
                    background: Rectangle {
                        radius: 6
                        color: track.highlighted ? track.palette.highlight : track.palette.base
                        border.color: track.activeFocus ? track.palette.text : "transparent"
                    }
                    onClicked: {
                        tracks.currentIndex = index
                        tracks.forceActiveFocus()
                    }
                    contentItem: RowLayout {
                        spacing: 12
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Label {
                                Layout.fillWidth: true
                                text: track.trackName
                                color: track.highlighted ? track.palette.highlightedText : track.palette.text
                                font.weight: Font.DemiBold
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                            }
                            Label {
                                Layout.fillWidth: true
                                text: track.stage
                                color: track.highlighted ? track.palette.highlightedText : track.palette.text
                                elide: Text.ElideRight
                            }
                        }
                        ProgressBar {
                            Layout.preferredWidth: 90
                            to: 100
                            value: track.percent
                            Accessible.name: "Pipeline progress for " + track.trackName
                        }
                        Label {
                            Layout.minimumWidth: implicitWidth
                            text: track.readout
                            color: track.highlighted ? track.palette.highlightedText : track.palette.text
                        }
                    }
                }
            }

            Basic.Pane {
                id: details
                Layout.preferredWidth: Math.max(210, root.availableWidth * 0.30)
                Layout.fillHeight: true
                property Item track: tracks.currentItem
                background: Rectangle {
                    radius: 6
                    color: details.palette.base
                    border.color: details.palette.mid
                }

                ScrollView {
                    anchors.fill: parent
                    contentWidth: availableWidth
                    ColumnLayout {
                        width: parent.width
                        spacing: 8
                        Label {
                            text: "Track details"
                            font.bold: true
                        }
                        Repeater {
                            model: {
                                const track = details.track
                                if (!track)
                                    return ["Select a track to see its status and next step."]
                                return [track.trackName, track.stage,
                                        track.trackSource ? "Source: " + track.trackSource : "",
                                        track.peer ? "Peer: " + track.peer : "",
                                        track.peerQueue ? "Peer queue: " + track.peerQueue : "",
                                        track.notes, track.nextStep]
                            }
                            delegate: Label {
                                required property string modelData
                                Layout.fillWidth: true
                                visible: modelData.length > 0
                                text: modelData
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                            }
                        }
                    }
                }
            }
        }
    }
}
