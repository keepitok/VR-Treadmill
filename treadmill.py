import os
import re
import signal
import time
import threading

from pynput.keyboard import Key, Listener
from PyQt6 import QtCore
from PyQt6.QtWidgets import (QApplication, QWidget, QPushButton, QVBoxLayout,
                              QHBoxLayout, QLineEdit, QLabel, QFrame, QCheckBox)
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush

import config
from hardware import gamepad, gamepad_lock, stop_event, _raw_reader, hip_turner

ctrlPressed = False
window      = None

_css_path = os.path.join(os.path.dirname(__file__), "style.css")
_palette  = {m.group(1): m.group(2)
             for m in re.finditer(r'--([\w-]+):\s*(#[0-9a-fA-F]+)', open(_css_path).read())}


def _make_label_row(text, tip):
    """Return (QHBoxLayout, QLabel) with the label and a hoverable ? help icon."""
    lbl = QLabel(text)
    lbl.setObjectName("sectionLabel")
    hint = QLabel("?")
    hint.setObjectName("helpIcon")
    hint.setToolTip(tip)
    hint.setCursor(QtCore.Qt.CursorShape.WhatsThisCursor)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(lbl)
    row.addWidget(hint)
    row.addStretch()
    return row, lbl


class ToggleSwitch(QCheckBox):
    """Pill-style toggle with sliding knob; label text reflects state."""
    _W, _H = 56, 30

    def __init__(self, name, on_suffix="  enabled", off_suffix="  disabled", parent=None):
        super().__init__(parent)
        self._name      = name
        self._on_text   = name + on_suffix
        self._off_text  = name + off_suffix
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.stateChanged.connect(lambda _: self.update())

    def sizeHint(self):
        fm = self.fontMetrics()
        longer = max(self._on_text, self._off_text, key=len)
        tw = fm.horizontalAdvance(longer)
        h  = max(self._H + 10, fm.height() + 12)
        return QtCore.QSize(self._W + 16 + tw, h)

    def minimumSizeHint(self):
        return self.sizeHint()

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self._W, self._H
        ty   = (self.height() - h) // 2
        r    = h / 2
        if not self.isEnabled():
            track_fill   = QColor(_palette["toggle-disabled-track"])
            track_border = QColor(_palette["toggle-disabled-border"])
            knob_col     = QColor(_palette["toggle-disabled-knob"])
            knob_x       = 4.0 if not self.isChecked() else float(w - h + 4)
            text_col     = QColor(_palette["toggle-disabled-text"])
            label        = self._on_text if self.isChecked() else self._off_text
        elif self.isChecked():
            track_fill   = QColor(_palette["toggle-on-track"])
            track_border = QColor(_palette["toggle-on-border"])
            knob_col     = QColor(_palette["toggle-on-knob"])
            knob_x       = float(w - h + 4)
            text_col     = QColor(_palette["toggle-on-text"])
            label        = self._on_text
        else:
            track_fill   = QColor(_palette["toggle-off-track"])
            track_border = QColor(_palette["toggle-off-border"])
            knob_col     = QColor(_palette["toggle-off-knob"])
            knob_x       = 4.0
            text_col     = QColor(_palette["toggle-off-text"])
            label        = self._off_text
        p.setBrush(QBrush(track_fill))
        p.setPen(QPen(track_border, 2))
        p.drawRoundedRect(QtCore.QRectF(1, ty, w, h), r, r)
        ks = h - 8
        p.setBrush(QBrush(knob_col))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(QtCore.QRectF(knob_x, ty + 4, ks, ks))
        p.setPen(text_col)
        p.setFont(self.font())
        p.drawText(QtCore.QRect(w + 16, 0, self.width() - w - 16, self.height()),
                   QtCore.Qt.AlignmentFlag.AlignVCenter, label)
        p.end()


class MainWindow(QWidget):

    _loopStopped = QtCore.pyqtSignal()
    _hotkeyPressed = QtCore.pyqtSignal()
    _START_LABEL = "▶ Start tracking"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.setWindowTitle("Maratron Locomotion")
        self.setMinimumWidth(600)

        # --- Stylesheet ---
        _css_path = os.path.join(os.path.dirname(__file__), "style.css")
        _qt_css = re.sub(r':root\s*\{[^}]*\}', '', open(_css_path).read())
        self.setStyleSheet(_qt_css)

        # --- Widgets ---
        self.startJoy = QPushButton(self._START_LABEL)
        self.startJoy.setObjectName("startBtn")
        self.startJoy.setProperty("running", False)
        self.startJoy.clicked.connect(self.toggleAll)
        self._running = False
        self._loopStopped.connect(self._onLoopStopped)
        self._hotkeyPressed.connect(self.toggleAll)

        self.keyLabel = QLabel("Hotkey: Ctrl + `")
        self.keyLabel.setObjectName("hotkeyLabel")
        self.keyLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        # --- Left column: Mouse ---
        self.mouseCheck = ToggleSwitch("Mouse tracking")
        self.mouseCheck.setObjectName("mainToggle")
        self.mouseCheck.setChecked(config.mouseEnabled)
        self.mouseCheck.stateChanged.connect(self.onMouseCheckChanged)

        self.invertCheck = ToggleSwitch("Invert Y", "  on", "  off")
        self.invertCheck.setObjectName("subToggle")
        self.invertCheck.setChecked(config.invertY)
        self.invertCheck.stateChanged.connect(self.onInvertYChanged)
        self.invertCheck.setToolTip("Reverses the treadmill walking direction on the joystick Y axis.")

        senseLabelRow, senseLabel = _make_label_row("SENSITIVITY",
            "How strongly mouse Y movement translates to the left joystick.\n"
            "Higher = faster virtual movement per physical step on the treadmill.")

        self.senseLine = QLineEdit(str(config.sensitivity))
        self.senseLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.senseLine.textChanged.connect(self.setSensitivity)

        senseDecBtn = QPushButton("−")
        senseDecBtn.setObjectName("stepBtn")
        senseDecBtn.clicked.connect(self.decreaseSensitivity)

        senseIncBtn = QPushButton("+")
        senseIncBtn.setObjectName("stepBtn")
        senseIncBtn.clicked.connect(self.increaseSensitivity)

        senseRow = QHBoxLayout()
        senseRow.setSpacing(8)
        senseRow.addWidget(senseDecBtn)
        senseRow.addWidget(self.senseLine)
        senseRow.addWidget(senseIncBtn)

        pollLabelRow, pollLabel = _make_label_row("POLLING RATE  (/sec)",
            "How many times per second mouse input is sampled and sent to the gamepad.\n"
            "Higher = smoother response, lower = less CPU usage.")

        self.pollRateLine = QLineEdit(str(config.pollRate))
        self.pollRateLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.pollRateLine.textChanged.connect(self.setPollingRate)

        pollDecBtn = QPushButton("−")
        pollDecBtn.setObjectName("stepBtn")
        pollDecBtn.clicked.connect(self.decreasePollRate)

        pollIncBtn = QPushButton("+")
        pollIncBtn.setObjectName("stepBtn")
        pollIncBtn.clicked.connect(self.increasePollRate)

        pollRow = QHBoxLayout()
        pollRow.setSpacing(8)
        pollRow.addWidget(pollDecBtn)
        pollRow.addWidget(self.pollRateLine)
        pollRow.addWidget(pollIncBtn)

        self.mouseSubGroup = QFrame()
        mouseSubLayout = QVBoxLayout(self.mouseSubGroup)
        mouseSubLayout.setContentsMargins(0, 4, 0, 0)
        mouseSubLayout.setSpacing(10)
        mouseSubLayout.addWidget(self.invertCheck)
        mouseSubLayout.addSpacing(4)
        mouseSubLayout.addLayout(senseLabelRow)
        mouseSubLayout.addLayout(senseRow)
        mouseSubLayout.addSpacing(4)
        mouseSubLayout.addLayout(pollLabelRow)
        mouseSubLayout.addLayout(pollRow)
        self.mouseSubGroup.setEnabled(config.mouseEnabled)

        leftCol = QVBoxLayout()
        leftCol.setSpacing(10)
        leftCol.addWidget(self.mouseCheck)
        leftCol.addWidget(self.mouseSubGroup)
        leftCol.addStretch()

        # --- Separator ---
        separator = QFrame()
        separator.setObjectName("separator")

        # --- Right column: Hip ---
        self.hipCheck = ToggleSwitch("Hip turns tracking")
        self.hipCheck.setObjectName("mainToggle")
        self.hipCheck.setChecked(config.hipEnabled)
        self.hipCheck.stateChanged.connect(self.onHipCheckChanged)
        self.hipCheck.setToolTip("Enables snap turns triggered by fast hip yaw twists detected via SlimeVR VMC output.")

        snapLabelRow, snapLabel = _make_label_row("SNAP THRESHOLD  (\u00b0/s)",
            "Minimum hip rotation speed (degrees/second) needed to trigger a snap turn.\n"
            "Lower = more sensitive to small twists.")

        self.snapLine = QLineEdit(str(config.snapThreshold))
        self.snapLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.snapLine.textChanged.connect(self.setSnapThreshold)

        snapDecBtn = QPushButton("−")
        snapDecBtn.setObjectName("stepBtn")
        snapDecBtn.clicked.connect(self.decreaseSnapThreshold)

        snapIncBtn = QPushButton("+")
        snapIncBtn.setObjectName("stepBtn")
        snapIncBtn.clicked.connect(self.increaseSnapThreshold)

        snapRow = QHBoxLayout()
        snapRow.setSpacing(8)
        snapRow.addWidget(snapDecBtn)
        snapRow.addWidget(self.snapLine)
        snapRow.addWidget(snapIncBtn)

        retDelayLabelRow, retDelayLabel = _make_label_row("RETURN DELAY  (ms)",
            "After a snap, opposite-direction snaps are blocked for this duration.\n"
            "Lets you return your hip to rest position at any speed without triggering\n"
            "an unwanted reverse snap. Same-direction snaps remain active throughout.")

        self.retDelayLine = QLineEdit(str(config.snapReturnDelay))
        self.retDelayLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.retDelayLine.textChanged.connect(self.setReturnDelay)

        retDelayDecBtn = QPushButton("−")
        retDelayDecBtn.setObjectName("stepBtn")
        retDelayDecBtn.clicked.connect(self.decreaseReturnDelay)

        retDelayIncBtn = QPushButton("+")
        retDelayIncBtn.setObjectName("stepBtn")
        retDelayIncBtn.clicked.connect(self.increaseReturnDelay)

        retDelayRow = QHBoxLayout()
        retDelayRow.setSpacing(8)
        retDelayRow.addWidget(retDelayDecBtn)
        retDelayRow.addWidget(self.retDelayLine)
        retDelayRow.addWidget(retDelayIncBtn)

        vmcPortLabelRow, vmcPortLabel2 = _make_label_row("VMC PORT OUT",
            "UDP port on which SlimeVR broadcasts VMC/OSC tracking data.\n"
            "Must match the VMC Output port configured in SlimeVR settings.")

        self.vmcPortLine = QLineEdit(str(config.vmcPort))
        self.vmcPortLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.vmcPortLine.textChanged.connect(self.setVmcPort)

        self.hipSubGroup = QFrame()
        hipSubLayout = QVBoxLayout(self.hipSubGroup)
        hipSubLayout.setContentsMargins(0, 4, 0, 0)
        hipSubLayout.setSpacing(10)
        hipSubLayout.addSpacing(4)
        hipSubLayout.addLayout(snapLabelRow)
        hipSubLayout.addLayout(snapRow)
        hipSubLayout.addSpacing(4)
        hipSubLayout.addLayout(retDelayLabelRow)
        hipSubLayout.addLayout(retDelayRow)
        hipSubLayout.addSpacing(4)
        hipSubLayout.addLayout(vmcPortLabelRow)
        hipSubLayout.addWidget(self.vmcPortLine)
        self.hipSubGroup.setEnabled(config.hipEnabled)

        rightCol = QVBoxLayout()
        rightCol.setSpacing(10)
        rightCol.addWidget(self.hipCheck)
        rightCol.addWidget(self.hipSubGroup)
        rightCol.addStretch()

        # --- Two-column row ---
        colsRow = QHBoxLayout()
        colsRow.setSpacing(0)
        colsRow.addLayout(leftCol, stretch=1)
        colsRow.addSpacing(16)
        colsRow.addWidget(separator)
        colsRow.addSpacing(16)
        colsRow.addLayout(rightCol, stretch=1)

        # --- Main layout ---
        layout = QVBoxLayout()
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        layout.addWidget(self.startJoy)
        layout.addWidget(self.keyLabel)
        layout.addSpacing(12)
        layout.addLayout(colsRow)

        self.setLayout(layout)
        self.show()
        
    def setPollingRate(a, b):
        if b != "":
            config.pollRate = int(b)
            config._save_config()

    def decreasePollRate(self):
        config.pollRate = max(1, config.pollRate - 5)
        self.pollRateLine.setText(str(config.pollRate))
        config._save_config()
        print(f"[Mouse] Poll rate: {config.pollRate}/s")

    def increasePollRate(self):
        config.pollRate += 5
        self.pollRateLine.setText(str(config.pollRate))
        config._save_config()
        print(f"[Mouse] Poll rate: {config.pollRate}/s")

    def setSensitivity(a, b):
        if b != "":
            config.sensitivity = int(b)
            config._save_config()

    def decreaseSensitivity(self):
        config.sensitivity = max(1, config.sensitivity - 10)
        self.senseLine.setText(str(config.sensitivity))
        config._save_config()
        print(f"[Mouse] Sensitivity: {config.sensitivity}")

    def increaseSensitivity(self):
        config.sensitivity += 10
        self.senseLine.setText(str(config.sensitivity))
        config._save_config()
        print(f"[Mouse] Sensitivity: {config.sensitivity}")

    def onMouseCheckChanged(self, state):
        config.mouseEnabled = bool(state)
        self.mouseSubGroup.setEnabled(config.mouseEnabled)
        config._save_config()
        if self._running or hip_turner._running:  # tracking session is active
            if config.mouseEnabled and not self._running:
                stop_event.clear()
                self._running = True
                threading.Thread(target=self._pollLoop, daemon=True).start()
                print("[Mouse] Tracking started")
                self._updateStartAllBtn()
            elif not config.mouseEnabled and self._running:
                stop_event.set()  # _onLoopStopped will update button

    def onInvertYChanged(self, state):
        config.invertY = bool(state)
        config._save_config()
        print(f"[Mouse] Invert Y: {'on' if config.invertY else 'off'}")

    def onHipCheckChanged(self, state):
        config.hipEnabled = bool(state)
        self.hipSubGroup.setEnabled(config.hipEnabled)
        config._save_config()
        if self._running or hip_turner._running:  # tracking session is active
            if config.hipEnabled and not hip_turner._running:
                hip_turner.start()
                self._updateStartAllBtn()
            elif not config.hipEnabled and hip_turner._running:
                hip_turner.stop()
                self._updateStartAllBtn()

    def onSnapKbChanged(self, state):
        config.snapUseKeyboard = bool(state)
        config._save_config()
        print(f"[Hip] Keyboard snaps: {'on' if config.snapUseKeyboard else 'off'}")

    def setSnapThreshold(a, b):
        if b != "":
            config.snapThreshold = int(b)
            config._save_config()

    def decreaseSnapThreshold(self):
        config.snapThreshold = max(10, config.snapThreshold - 10)
        self.snapLine.setText(str(config.snapThreshold))
        config._save_config()
        print(f"[Hip] Snap threshold: {config.snapThreshold} °/s")

    def increaseSnapThreshold(self):
        config.snapThreshold += 10
        self.snapLine.setText(str(config.snapThreshold))
        config._save_config()
        print(f"[Hip] Snap threshold: {config.snapThreshold} °/s")

    def setReturnDelay(a, b):
        if b != "" and b.isdigit():
            config.snapReturnDelay = int(b)
            config._save_config()

    def decreaseReturnDelay(self):
        config.snapReturnDelay = max(0, config.snapReturnDelay - 100)
        self.retDelayLine.setText(str(config.snapReturnDelay))
        config._save_config()
        print(f"[Hip] Return delay: {config.snapReturnDelay} ms")

    def increaseReturnDelay(self):
        config.snapReturnDelay += 100
        self.retDelayLine.setText(str(config.snapReturnDelay))
        config._save_config()
        print(f"[Hip] Return delay: {config.snapReturnDelay} ms")

    def setVmcPort(a, b):
        if b != "" and b.isdigit():
            config.vmcPort = int(b)
            config._save_config()

    def closeEvent(self, event):
        if hip_turner._running:
            hip_turner.stop()
        config._save_config()
        print("[Gamepad] Virtual controller disconnected")
        event.accept()
            
    # def setKey(a, b):
    #     global quitKey
    #     global keyToggle
    #     if not keyToggle:
    #         a.keyLabel.setText("PRESS ANY KEY")
    #         a.setKeyButton.setText("Confirm?")
    #         print("Listening...")
    #         keyToggle = True
    #     else:
    #         a.keyLabel.setText("Stop Key: " + str(quitKey))
    #         a.setKeyButton.setText("Set Stop Key")
    #         print("Confirmed")
    #         keyToggle = False
        
        
    def toggleAll(self):
        any_running = self._running or hip_turner._running
        if any_running:
            if self._running:
                stop_event.set()
                self._running = False
            if hip_turner._running:
                hip_turner.stop()
            self._updateStartAllBtn()
        else:
            if self.mouseCheck.isChecked():
                stop_event.clear()
                self._running = True
                threading.Thread(target=self._pollLoop, daemon=True).start()
                print("[Mouse] Tracking started")
            if self.hipCheck.isChecked():
                hip_turner.start()
            self._updateStartAllBtn()

    def _updateStartAllBtn(self):
        any_running = self._running or hip_turner._running
        self.startJoy.setText("Stop" if any_running else self._START_LABEL)
        self.startJoy.setProperty("running", any_running)
        self.startJoy.style().unpolish(self.startJoy)
        self.startJoy.style().polish(self.startJoy)

    def _onLoopStopped(self):
        self._running = False
        self._updateStartAllBtn()

    def _pollLoop(self):
        mousey        = 0
        mousey1       = 0
        mousey2       = 0
        _last_log_t   = 0.0
        _LOG_INTERVAL = 0.3  # seconds between joystick log lines
        _raw_reader.consume_delta()  # flush any pre-accumulated movement
        while not stop_event.is_set():
            mousey2 = mousey1

            raw_delta = _raw_reader.consume_delta()  # hardware Y counts since last poll
            direction = config.sensitivity if config.invertY else -(config.sensitivity)
            mousey1 = raw_delta * direction

            mousey = max(-32768, min(32767, int((mousey1 + mousey2) / 2)))  # average and clamp
            now = time.monotonic()
            if mousey != 0 and (config.DEBUG or now - _last_log_t >= _LOG_INTERVAL):
                print("Joystick y:", mousey)
                _last_log_t = now

            with gamepad_lock:
                gamepad.left_joystick(x_value=0, y_value=mousey)
                _current_left_y = mousey
                gamepad.update()
            time.sleep(1 / config.pollRate)

        # release joystick when stopped
        with gamepad_lock:
            gamepad.left_joystick(x_value=0, y_value=0)
            gamepad.update()
        self._loopStopped.emit()
        print("[Mouse] Tracking stopped")
        
def onPress(key):
    global ctrlPressed
    # print(f"[DEBUG] key={key!r}  type={type(key).__name__}  char={getattr(key, 'char', None)!r}  vk={getattr(key, 'vk', None)!r}  ctrlPressed={ctrlPressed}")
    if key in (Key.ctrl, Key.ctrl_l, Key.ctrl_r):
        ctrlPressed = True
        return
    k = getattr(key, 'char', None)
    vk = getattr(key, 'vk', None)
    if ctrlPressed and (k == '`' or vk == 192) and window is not None:
        # print("[DEBUG] Hotkey matched — toggling")
        window._hotkeyPressed.emit()

def onRelease(key):
    global ctrlPressed
    if key in (Key.ctrl, Key.ctrl_l, Key.ctrl_r):
        ctrlPressed = False

listener = Listener(on_press=onPress, on_release=onRelease)
listener.start()

app = QApplication([])
window = MainWindow()
signal.signal(signal.SIGINT, lambda *_: app.quit())
# Let Python process signals even while Qt is running
_sig_timer = QtCore.QTimer()
_sig_timer.timeout.connect(lambda: None)
_sig_timer.start(200)
app.exec()

