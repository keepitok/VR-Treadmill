import ctypes
import ctypes.wintypes
import time
import threading
import json
import os
import sys
from pynput.keyboard import Key, Listener, Controller as _KbController
from pynput.mouse import Controller
from pythonosc import dispatcher, osc_server
from PyQt6 import QtCore
from PyQt6.QtWidgets import QApplication, QWidget, QPushButton, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel, QFrame, QCheckBox
from PyQt6.QtGui import QFont, QPainter, QColor, QPen, QBrush
import vgamepad as vg

DEBUG = "--debug" in sys.argv

# -------------------------------------------------------------------
# Config persistence
# -------------------------------------------------------------------
_CONFIG_PATH = os.path.join(os.environ["APPDATA"], "Maratron TreadMouse", "config.json")
_DEFAULTS = {"sensitivity": 35, "pollRate": 30, "invertY": False,
             "snapThreshold": 120, "snapDuration": 600, "vmcPort": 39539,
             "mouseEnabled": True, "hipEnabled": False, "snapUseKeyboard": False}

def _load_config():
    try:
        with open(_CONFIG_PATH) as f:
            return {**_DEFAULTS, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return _DEFAULTS.copy()

def _save_config():
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump({"sensitivity": sensitivity, "pollRate": pollRate, "invertY": invertY,
                   "snapThreshold": snapThreshold, "snapDuration": snapDuration, "vmcPort": vmcPort,
                   "mouseEnabled": mouseEnabled, "hipEnabled": hipEnabled,
                   "snapUseKeyboard": snapUseKeyboard}, f, indent=2)

gamepad = vg.VX360Gamepad()
gamepad_lock = threading.Lock()
mouse = Controller()
stop_event = threading.Event()
ctrlPressed = False
window = None

# -------------------------------------------------------------------
_cfg = _load_config()
sensitivity   = _cfg["sensitivity"]
pollRate      = _cfg["pollRate"]
invertY       = _cfg["invertY"]
snapThreshold = _cfg["snapThreshold"]
snapDuration  = _cfg["snapDuration"]
vmcPort       = _cfg["vmcPort"]
mouseEnabled     = _cfg["mouseEnabled"]
hipEnabled       = _cfg["hipEnabled"]
snapUseKeyboard  = _cfg["snapUseKeyboard"]
snapKeyLeft      = 'q'
snapKeyRight     = 'e'
# -------------------------------------------------------------------

# -------------------------------------------------------------------
# Win32 Raw Mouse Input — reads hardware delta Y directly,
# bypassing cursor position, screen edges, and Windows acceleration.
# -------------------------------------------------------------------
class _RawMouseReader:
    _WM_INPUT        = 0x00FF
    _WM_DESTROY      = 0x0002
    _RIDEV_INPUTSINK = 0x00000100
    _RID_INPUT       = 0x10000003

    def __init__(self):
        self._delta_y = 0
        self._lock    = threading.Lock()
        self._hwnd    = None
        self._ready   = threading.Event()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self._ready.wait(timeout=5)

    def consume_delta(self):
        """Return total accumulated Y delta since last call and reset it."""
        with self._lock:
            val = self._delta_y
            self._delta_y = 0
        return val

    def _run(self):
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.DefWindowProcW.restype  = ctypes.c_longlong
        user32.DefWindowProcW.argtypes = [
            ctypes.wintypes.HWND, ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong,
            ctypes.wintypes.HWND, ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

        def _wnd_proc(hwnd, msg, wparam, lparam):
            if msg == self._WM_INPUT:
                self._handle_raw(hwnd, lparam)
                return 0
            if msg == self._WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._cb = WNDPROC(_wnd_proc)  # keep reference alive

        class WNDCLASSEX(ctypes.Structure):
            _fields_ = [("cbSize",        ctypes.wintypes.UINT),
                        ("style",         ctypes.wintypes.UINT),
                        ("lpfnWndProc",   WNDPROC),
                        ("cbClsExtra",    ctypes.c_int),
                        ("cbWndExtra",    ctypes.c_int),
                        ("hInstance",     ctypes.wintypes.HANDLE),
                        ("hIcon",         ctypes.wintypes.HANDLE),
                        ("hCursor",       ctypes.wintypes.HANDLE),
                        ("hbrBackground", ctypes.wintypes.HANDLE),
                        ("lpszMenuName",  ctypes.wintypes.LPCWSTR),
                        ("lpszClassName", ctypes.wintypes.LPCWSTR),
                        ("hIconSm",       ctypes.wintypes.HANDLE)]

        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEX()
        wc.cbSize        = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc   = self._cb
        wc.hInstance     = hinstance
        wc.lpszClassName = "TreadmillRawInput"
        user32.RegisterClassExW(ctypes.byref(wc))

        WS_POPUP = 0x80000000  # hidden window — no taskbar entry, no title bar
        hwnd = user32.CreateWindowExW(
            0, "TreadmillRawInput", "", WS_POPUP,
            0, 0, 0, 0,
            None, None, hinstance, None)
        self._hwnd = hwnd

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [("usUsagePage", ctypes.wintypes.USHORT),
                        ("usUsage",     ctypes.wintypes.USHORT),
                        ("dwFlags",     ctypes.wintypes.DWORD),
                        ("hwndTarget",  ctypes.wintypes.HWND)]

        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01              # Generic Desktop Controls
        rid.usUsage     = 0x02              # Mouse
        rid.dwFlags     = self._RIDEV_INPUTSINK  # receive input even without focus
        rid.hwndTarget  = hwnd
        reg_ok = user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid))

        self._ready.set()

        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_raw(self, hwnd, lparam):
        user32 = ctypes.windll.user32

        class RAWINPUTHEADER(ctypes.Structure):
            _fields_ = [("dwType",  ctypes.wintypes.DWORD),
                        ("dwSize",  ctypes.wintypes.DWORD),
                        ("hDevice", ctypes.wintypes.HANDLE),
                        ("wParam",  ctypes.wintypes.WPARAM)]

        class RAWMOUSE(ctypes.Structure):
            _fields_ = [("usFlags",           ctypes.wintypes.USHORT),
                        ("usButtonFlags",      ctypes.wintypes.USHORT),
                        ("usButtonData",       ctypes.wintypes.USHORT),
                        ("ulRawButtons",       ctypes.wintypes.ULONG),
                        ("lLastX",             ctypes.c_long),
                        ("lLastY",             ctypes.c_long),
                        ("ulExtraInformation", ctypes.wintypes.ULONG)]

        class RAWINPUT(ctypes.Structure):
            _fields_ = [("header", RAWINPUTHEADER),
                        ("mouse",  RAWMOUSE)]

        size = ctypes.wintypes.UINT(ctypes.sizeof(RAWINPUT))
        ri   = RAWINPUT()
        res  = user32.GetRawInputData(
            ctypes.wintypes.HANDLE(lparam),
            self._RID_INPUT,
            ctypes.byref(ri),
            ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER))

        if res == ctypes.wintypes.UINT(-1).value:
            return  # error reading data
        if ri.header.dwType == 0:                   # RIM_TYPEMOUSE
            if (ri.mouse.usFlags & 0x0001) == 0:    # MOUSE_MOVE_RELATIVE
                with self._lock:
                    self._delta_y += ri.mouse.lLastY

_raw_reader = _RawMouseReader()

# -------------------------------------------------------------------
# Hip Snap-Turn — listens for SlimeVR VMC/OSC output, detects fast
# hip yaw twists, and fires brief right-joystick pulses on the gamepad.
# -------------------------------------------------------------------
import math as _math

class _ReusableOSCServer(osc_server.ThreadingOSCUDPServer):
    allow_reuse_address = True

class HipSnapTurner:
    _COOLDOWN      = 0.6   # seconds between snaps
    _STATUS_EVERY  = 5.0   # seconds between periodic yaw prints
    _STALL_AFTER   = 5.0   # seconds without data before warning

    def __init__(self):
        self._running        = False
        self._server         = None
        self._last_snap      = 0.0
        self._prev_yaw       = None
        self._prev_time      = None
        self._connected      = False
        self._last_packet    = 0.0
        self._last_status    = 0.0
        self._stall_warned   = False

    def start(self):
        if self._running:
            return
        disp = dispatcher.Dispatcher()
        disp.map("/VMC/Ext/Bone/Pos", self._on_bone)
        self._server       = _ReusableOSCServer(("0.0.0.0", vmcPort), disp)
        self._running      = True
        self._connected    = False
        self._stall_warned = False
        self._prev_yaw     = None
        self._prev_time    = None
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        print(f"[Hip] Listening on UDP {vmcPort}...")

    def stop(self):
        if not self._running:
            return
        self._server.shutdown()
        self._server    = None
        self._running   = False
        self._connected = False
        self._prev_yaw  = None
        self._prev_time = None
        print("[Hip] Stopped")

    def _watchdog(self):
        """Prints a stall warning if data stops arriving after first connection."""
        while self._running:
            time.sleep(1.0)
            if self._connected and self._running:
                gap = time.monotonic() - self._last_packet
                if gap > self._STALL_AFTER and not self._stall_warned:
                    print(f"[Hip] No data for {self._STALL_AFTER:.0f}s — "
                          f"is SlimeVR VMC output enabled on port {vmcPort}?")
                    self._stall_warned = True
                elif gap <= self._STALL_AFTER:
                    self._stall_warned = False

    def _on_bone(self, address, *args):
        # VMC /VMC/Ext/Bone/Pos: bone_name, px, py, pz, qx, qy, qz, qw
        if len(args) < 8 or args[0] != "Hips":
            return
        now = time.monotonic()
        self._last_packet = now

        if not self._connected:
            self._connected = True
            print("[Hip] Connected — receiving data from SlimeVR")

        qx, qy, qz, qw = args[4], args[5], args[6], args[7]
        yaw = _math.degrees(_math.atan2(2.0 * (qw * qy + qx * qz),
                                        1.0 - 2.0 * (qy * qy + qz * qz)))

        rate = 0.0
        if self._prev_yaw is not None and self._prev_time is not None:
            dt = now - self._prev_time
            if dt > 0:
                delta = (yaw - self._prev_yaw + 180.0) % 360.0 - 180.0
                rate  = delta / dt  # °/s
                if (abs(rate) > snapThreshold
                        and (now - self._last_snap) > self._COOLDOWN):
                    self._do_snap(1 if rate > 0 else -1)

        # Throttled status print
        if DEBUG and now - self._last_status >= self._STATUS_EVERY:
            self._last_status = now
            print(f"[Hip] yaw: {yaw:+.1f}°  rate: {rate:.0f} °/s")

        self._prev_yaw  = yaw
        self._prev_time = now

    def _do_snap(self, sign):
        self._last_snap = time.monotonic()
        direction = 'right' if sign > 0 else 'left'
        print(f"[Hip] snap {direction}")

        if snapUseKeyboard:
            key = snapKeyRight if sign > 0 else snapKeyLeft
            def _kb_pulse():
                _kb.press(key)
                time.sleep(snapDuration / 1000.0)
                _kb.release(key)
            threading.Thread(target=_kb_pulse, daemon=True).start()
        else:
            axis = 32767 if sign > 0 else -32768
            hold = snapDuration / 1000.0
            def _pulse():
                _STEPS = 12
                _RAMP  = 0.06
                step_t = _RAMP / _STEPS
                for i in range(1, _STEPS + 1):
                    with gamepad_lock:
                        gamepad.right_joystick(x_value=int(axis * i / _STEPS), y_value=0)
                        gamepad.update()
                    time.sleep(step_t)
                time.sleep(hold)
                for i in range(_STEPS - 1, -1, -1):
                    with gamepad_lock:
                        gamepad.right_joystick(x_value=int(axis * i / _STEPS), y_value=0)
                        gamepad.update()
                    time.sleep(step_t)
            threading.Thread(target=_pulse, daemon=True).start()

_kb = _KbController()

hip_turner = HipSnapTurner()
_current_left_y = 0  # shared: lets snap pulse read current walk Y


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
            track_fill   = QColor("#1a1a30")
            track_border = QColor("#2a2a48")
            knob_col     = QColor("#2e2e4e")
            knob_x       = 4.0 if not self.isChecked() else float(w - h + 4)
            text_col     = QColor("#35355a")
            label        = self._on_text if self.isChecked() else self._off_text
        elif self.isChecked():
            track_fill   = QColor("#1a5c33")
            track_border = QColor("#2ecc71")
            knob_col     = QColor("#ffffff")
            knob_x       = float(w - h + 4)
            text_col     = QColor("#e0e0e0")
            label        = self._on_text
        else:
            track_fill   = QColor("#252545")
            track_border = QColor("#4a4a7a")
            knob_col     = QColor("#6a6a8a")
            knob_x       = 4.0
            text_col     = QColor("#6a6a8a")
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

        self.setWindowTitle("Maratron TreadMouse")
        self.setMinimumWidth(600)

        # --- Fonts ---
        labelFont = QFont("Segoe UI", 13)
        labelFont.setWeight(QFont.Weight.Medium)
        inputFont = QFont("Segoe UI", 20)
        btnFont = QFont("Segoe UI", 22)
        btnFont.setWeight(QFont.Weight.Bold)
        startFont = QFont("Segoe UI", 28)
        startFont.setWeight(QFont.Weight.Bold)
        hotkeyFont = QFont("Segoe UI", 11)

        # --- Stylesheet ---
        self.setStyleSheet("""
            QWidget {
                background-color: #1a1a2e;
                color: #e0e0e0;
            }
            QLabel#hotkeyLabel {
                color: #7a7a9a;
                padding: 0px 4px;
            }
            QLabel {
                color: #a0a8c0;
                padding: 2px 4px;
            }
            QPushButton {
                background-color: #2d2d4e;
                color: #c8d0f0;
                border: 2px solid #4a4a7a;
                border-radius: 10px;
                padding: 10px 18px;
            }
            QPushButton:hover {
                background-color: #3a3a66;
                border-color: #7070c0;
                color: #ffffff;
            }
            QPushButton:pressed {
                background-color: #252545;
                border-color: #5050a0;
            }
            QPushButton#startBtn {
                background-color: #1a6b3a;
                color: #ffffff;
                border: 2px solid #2ecc71;
                border-radius: 12px;
                padding: 16px 24px;
            }
            QPushButton#startBtn:hover {
                background-color: #21854a;
                border-color: #58d68d;
            }
            QPushButton#startBtn[running=true] {
                background-color: #7b1a1a;
                border-color: #e74c3c;
            }
            QPushButton#startBtn[running=true]:hover {
                background-color: #962222;
                border-color: #f1948a;
            }
            QCheckBox {
                color: #c8d0f0;
                spacing: 14px;
                padding: 6px 2px;
            }
            QCheckBox:hover {
                color: #ffffff;
            }
            QCheckBox::indicator {
                width: 52px;
                height: 28px;
                border-radius: 14px;
                background-color: #252545;
                border: 2px solid #4a4a7a;
            }
            QCheckBox::indicator:hover {
                border-color: #7070c0;
            }
            QCheckBox::indicator:checked {
                background-color: #1a4a6b;
                border-color: #3498db;
            }
            QLineEdit {
                background-color: #12122a;
                color: #e8eaf6;
                border: 2px solid #3a3a6a;
                border-radius: 8px;
                padding: 8px 14px;
                selection-background-color: #4a4aaa;
            }
            QLineEdit:focus {
                border-color: #6060c0;
                background-color: #16163a;
            }
            QLineEdit:disabled {
                background-color: #0e0e20;
                color: #3a3a58;
                border-color: #1e1e3a;
            }
            QPushButton:disabled {
                background-color: #1c1c36;
                color: #3a3a58;
                border-color: #1e1e3a;
            }
            QLabel:disabled {
                color: #3a3a58;
            }
            QCheckBox:disabled {
                color: #3a3a58;
            }
            QCheckBox::indicator:disabled {
                background-color: #1c1c36;
                border-color: #1e1e3a;
            }
        """)

        # --- Widgets ---
        self.startJoy = QPushButton(self._START_LABEL)
        self.startJoy.setObjectName("startBtn")
        self.startJoy.setFont(startFont)
        self.startJoy.setMinimumHeight(90)
        self.startJoy.setProperty("running", False)
        self.startJoy.clicked.connect(self.toggleAll)
        self._running = False
        self._loopStopped.connect(self._onLoopStopped)
        self._hotkeyPressed.connect(self.toggleAll)

        self.keyLabel = QLabel("Hotkey: Ctrl + `")
        self.keyLabel.setObjectName("hotkeyLabel")
        self.keyLabel.setFont(hotkeyFont)
        self.keyLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        toggleFont = QFont("Segoe UI", 16)
        toggleFont.setWeight(QFont.Weight.Bold)

        # --- Left column: Mouse ---
        self.mouseCheck = ToggleSwitch("Mouse tracking")
        self.mouseCheck.setFont(toggleFont)
        self.mouseCheck.setChecked(mouseEnabled)
        self.mouseCheck.stateChanged.connect(self.onMouseCheckChanged)

        self.invertCheck = ToggleSwitch("Invert Y", "  on", "  off")
        self.invertCheck.setFont(labelFont)
        self.invertCheck.setChecked(invertY)
        self.invertCheck.stateChanged.connect(self.onInvertYChanged)

        senseLabel = QLabel("SENSITIVITY")
        senseLabel.setFont(labelFont)

        self.senseLine = QLineEdit(str(sensitivity))
        self.senseLine.setFont(inputFont)
        self.senseLine.setMinimumHeight(56)
        self.senseLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.senseLine.textChanged.connect(self.setSensitivity)

        senseDecBtn = QPushButton("−")
        senseDecBtn.setFont(btnFont)
        senseDecBtn.setMinimumHeight(56)
        senseDecBtn.setMinimumWidth(60)
        senseDecBtn.clicked.connect(self.decreaseSensitivity)

        senseIncBtn = QPushButton("+")
        senseIncBtn.setFont(btnFont)
        senseIncBtn.setMinimumHeight(56)
        senseIncBtn.setMinimumWidth(60)
        senseIncBtn.clicked.connect(self.increaseSensitivity)

        senseRow = QHBoxLayout()
        senseRow.setSpacing(8)
        senseRow.addWidget(senseDecBtn)
        senseRow.addWidget(self.senseLine)
        senseRow.addWidget(senseIncBtn)

        pollLabel = QLabel("POLLING RATE  (/sec)")
        pollLabel.setFont(labelFont)

        self.pollRateLine = QLineEdit(str(pollRate))
        self.pollRateLine.setFont(inputFont)
        self.pollRateLine.setMinimumHeight(56)
        self.pollRateLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.pollRateLine.textChanged.connect(self.setPollingRate)

        pollDecBtn = QPushButton("−")
        pollDecBtn.setFont(btnFont)
        pollDecBtn.setMinimumHeight(56)
        pollDecBtn.setMinimumWidth(60)
        pollDecBtn.clicked.connect(self.decreasePollRate)

        pollIncBtn = QPushButton("+")
        pollIncBtn.setFont(btnFont)
        pollIncBtn.setMinimumHeight(56)
        pollIncBtn.setMinimumWidth(60)
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
        mouseSubLayout.addWidget(senseLabel)
        mouseSubLayout.addLayout(senseRow)
        mouseSubLayout.addSpacing(4)
        mouseSubLayout.addWidget(pollLabel)
        mouseSubLayout.addLayout(pollRow)
        self.mouseSubGroup.setEnabled(mouseEnabled)

        leftCol = QVBoxLayout()
        leftCol.setSpacing(10)
        leftCol.addWidget(self.mouseCheck)
        leftCol.addWidget(self.mouseSubGroup)
        leftCol.addStretch()

        # --- Separator ---
        separator = QFrame()
        separator.setFixedWidth(2)
        separator.setStyleSheet("background-color: #3a3a6a;")

        # --- Right column: Hip ---
        self.hipCheck = ToggleSwitch("Hip turns tracking")
        self.hipCheck.setFont(toggleFont)
        self.hipCheck.setChecked(hipEnabled)
        self.hipCheck.stateChanged.connect(self.onHipCheckChanged)

        snapLabel = QLabel("SNAP THRESHOLD  (°/s)")
        snapLabel.setFont(labelFont)

        self.snapLine = QLineEdit(str(snapThreshold))
        self.snapLine.setFont(inputFont)
        self.snapLine.setMinimumHeight(56)
        self.snapLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.snapLine.textChanged.connect(self.setSnapThreshold)

        snapDecBtn = QPushButton("−")
        snapDecBtn.setFont(btnFont)
        snapDecBtn.setMinimumHeight(56)
        snapDecBtn.setMinimumWidth(60)
        snapDecBtn.clicked.connect(self.decreaseSnapThreshold)

        snapIncBtn = QPushButton("+")
        snapIncBtn.setFont(btnFont)
        snapIncBtn.setMinimumHeight(56)
        snapIncBtn.setMinimumWidth(60)
        snapIncBtn.clicked.connect(self.increaseSnapThreshold)

        snapRow = QHBoxLayout()
        snapRow.setSpacing(8)
        snapRow.addWidget(snapDecBtn)
        snapRow.addWidget(self.snapLine)
        snapRow.addWidget(snapIncBtn)

        snapDurLabel = QLabel("SNAP DURATION  (ms)")
        snapDurLabel.setFont(labelFont)

        self.snapDurLine = QLineEdit(str(snapDuration))
        self.snapDurLine.setFont(inputFont)
        self.snapDurLine.setMinimumHeight(56)
        self.snapDurLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.snapDurLine.textChanged.connect(self.setSnapDuration)

        snapDurDecBtn = QPushButton("−")
        snapDurDecBtn.setFont(btnFont)
        snapDurDecBtn.setMinimumHeight(56)
        snapDurDecBtn.setMinimumWidth(60)
        snapDurDecBtn.clicked.connect(self.decreaseSnapDuration)

        snapDurIncBtn = QPushButton("+")
        snapDurIncBtn.setFont(btnFont)
        snapDurIncBtn.setMinimumHeight(56)
        snapDurIncBtn.setMinimumWidth(60)
        snapDurIncBtn.clicked.connect(self.increaseSnapDuration)

        snapDurRow = QHBoxLayout()
        snapDurRow.setSpacing(8)
        snapDurRow.addWidget(snapDurDecBtn)
        snapDurRow.addWidget(self.snapDurLine)
        snapDurRow.addWidget(snapDurIncBtn)

        vmcPortLabel = QLabel("VMC PORT OUT")
        vmcPortLabel.setFont(labelFont)

        self.vmcPortLine = QLineEdit(str(vmcPort))
        self.vmcPortLine.setFont(inputFont)
        self.vmcPortLine.setMinimumHeight(56)
        self.vmcPortLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.vmcPortLine.textChanged.connect(self.setVmcPort)

        self.snapKbCheck = ToggleSwitch("Snap via keyboard  Q/E", "  (active)", "  (gamepad)")
        self.snapKbCheck.setFont(labelFont)
        self.snapKbCheck.setChecked(snapUseKeyboard)
        self.snapKbCheck.stateChanged.connect(self.onSnapKbChanged)

        self.hipSubGroup = QFrame()
        hipSubLayout = QVBoxLayout(self.hipSubGroup)
        hipSubLayout.setContentsMargins(0, 4, 0, 0)
        hipSubLayout.setSpacing(10)
        hipSubLayout.addWidget(self.snapKbCheck)
        hipSubLayout.addSpacing(4)
        hipSubLayout.addWidget(snapLabel)
        hipSubLayout.addLayout(snapRow)
        hipSubLayout.addSpacing(4)
        hipSubLayout.addWidget(snapDurLabel)
        hipSubLayout.addLayout(snapDurRow)
        hipSubLayout.addSpacing(4)
        hipSubLayout.addWidget(vmcPortLabel)
        hipSubLayout.addWidget(self.vmcPortLine)
        self.hipSubGroup.setEnabled(hipEnabled)

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
        global pollRate
        if b != "":
            pollRate = int(b)
            _save_config()
            print("Poll rate:", b)

    def decreasePollRate(self):
        global pollRate
        pollRate = max(1, pollRate - 5)
        self.pollRateLine.setText(str(pollRate))
        _save_config()
        print("Poll rate:", pollRate)

    def increasePollRate(self):
        global pollRate
        pollRate += 5
        self.pollRateLine.setText(str(pollRate))
        _save_config()
        print("Poll rate:", pollRate)

    def setSensitivity(a, b):
        global sensitivity
        if b != "":
            sensitivity = int(b)
            _save_config()
            print("Sensitivity:", b)

    def decreaseSensitivity(self):
        global sensitivity
        sensitivity = max(1, sensitivity - 10)
        self.senseLine.setText(str(sensitivity))
        _save_config()
        print("Sensitivity:", sensitivity)

    def increaseSensitivity(self):
        global sensitivity
        sensitivity += 10
        self.senseLine.setText(str(sensitivity))
        _save_config()
        print("Sensitivity:", sensitivity)

    def onMouseCheckChanged(self, state):
        global mouseEnabled
        mouseEnabled = bool(state)
        self.mouseSubGroup.setEnabled(mouseEnabled)
        _save_config()

    def onInvertYChanged(self, state):
        global invertY
        invertY = bool(state)
        _save_config()
        print("Invert Y:", invertY)

    def onHipCheckChanged(self, state):
        global hipEnabled
        hipEnabled = bool(state)
        self.hipSubGroup.setEnabled(hipEnabled)
        _save_config()

    def onSnapKbChanged(self, state):
        global snapUseKeyboard
        snapUseKeyboard = bool(state)
        _save_config()
        print("Snap via keyboard:", snapUseKeyboard)

    def setSnapThreshold(a, b):
        global snapThreshold
        if b != "":
            snapThreshold = int(b)
            _save_config()
            print("Snap threshold:", b)

    def decreaseSnapThreshold(self):
        global snapThreshold
        snapThreshold = max(10, snapThreshold - 10)
        self.snapLine.setText(str(snapThreshold))
        _save_config()
        print("Snap threshold:", snapThreshold)

    def increaseSnapThreshold(self):
        global snapThreshold
        snapThreshold += 10
        self.snapLine.setText(str(snapThreshold))
        _save_config()
        print("Snap threshold:", snapThreshold)

    def setSnapDuration(a, b):
        global snapDuration
        if b != "":
            snapDuration = int(b)
            _save_config()
            print("Snap duration:", b)

    def decreaseSnapDuration(self):
        global snapDuration
        snapDuration = max(50, snapDuration - 50)
        self.snapDurLine.setText(str(snapDuration))
        _save_config()
        print("Snap duration:", snapDuration)

    def increaseSnapDuration(self):
        global snapDuration
        snapDuration += 50
        self.snapDurLine.setText(str(snapDuration))
        _save_config()
        print("Snap duration:", snapDuration)

    def setVmcPort(a, b):
        global vmcPort
        if b != "" and b.isdigit():
            vmcPort = int(b)
            _save_config()
            print("VMC port:", vmcPort)

    def closeEvent(self, event):
        if hip_turner._running:
            hip_turner.stop()
        _save_config()
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
        mousey  = 0
        mousey1 = 0
        mousey2 = 0
        _raw_reader.consume_delta()  # flush any pre-accumulated movement
        while not stop_event.is_set():
            mousey2 = mousey1

            raw_delta = _raw_reader.consume_delta()  # hardware Y counts since last poll
            direction = sensitivity if invertY else -(sensitivity)
            mousey1 = raw_delta * direction

            mousey = max(-32768, min(32767, int((mousey1 + mousey2) / 2)))  # average and clamp
            if mousey != 0:
                print("Joystick y:", mousey)

            with gamepad_lock:
                gamepad.left_joystick(x_value=0, y_value=mousey)
                _current_left_y = mousey
                gamepad.update()
            time.sleep(1 / pollRate)

        # release joystick when stopped
        with gamepad_lock:
            gamepad.left_joystick(x_value=0, y_value=0)
            gamepad.update()
        self._loopStopped.emit()
        print("Loop stopped")
        
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
app.exec()

