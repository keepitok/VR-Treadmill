import ctypes
import ctypes.wintypes
import time
import threading
import json
import os
from pynput.keyboard import Key, Listener
from pynput.mouse import Controller
from PyQt6 import QtCore
from PyQt6.QtWidgets import QApplication, QWidget, QPushButton, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel
from PyQt6.QtGui import QFont
import vgamepad as vg

# -------------------------------------------------------------------
# Config persistence
# -------------------------------------------------------------------
_CONFIG_PATH = os.path.join(os.environ["APPDATA"], "Maratron TreadMouse", "config.json")
_DEFAULTS = {"sensitivity": 35, "pollRate": 30, "invertY": False}

def _load_config():
    try:
        with open(_CONFIG_PATH) as f:
            return {**_DEFAULTS, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return _DEFAULTS.copy()

def _save_config():
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump({"sensitivity": sensitivity, "pollRate": pollRate, "invertY": invertY}, f, indent=2)

gamepad = vg.VX360Gamepad()
mouse = Controller()
stop_event = threading.Event()
ctrlPressed = False
window = None

# -------------------------------------------------------------------
_cfg = _load_config()
sensitivity = _cfg["sensitivity"]
pollRate    = _cfg["pollRate"]
invertY     = _cfg["invertY"]
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

class MainWindow(QWidget):

    _loopStopped = QtCore.pyqtSignal()
    _hotkeyPressed = QtCore.pyqtSignal()

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
            QPushButton#invertBtn[checked=true] {
                background-color: #1a4a6b;
                border-color: #3498db;
                color: #aedcf8;
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
        """)

        # --- Widgets ---
        self.startJoy = QPushButton("▶  Start")
        self.startJoy.setObjectName("startBtn")
        self.startJoy.setFont(startFont)
        self.startJoy.setMinimumHeight(90)
        self.startJoy.setProperty("running", False)
        self.startJoy.clicked.connect(self.toggleRun)
        self._running = False
        self._loopStopped.connect(self._onLoopStopped)
        self._hotkeyPressed.connect(self.toggleRun)

        self.keyLabel = QLabel("Hotkey: Ctrl + `")
        self.keyLabel.setObjectName("hotkeyLabel")
        self.keyLabel.setFont(hotkeyFont)
        self.keyLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        senseLabel = QLabel("SENSITIVITY")
        senseLabel.setFont(labelFont)

        self.senseLine = QLineEdit(str(sensitivity))
        self.senseLine.setFont(inputFont)
        self.senseLine.setMinimumHeight(64)
        self.senseLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.senseLine.textChanged.connect(self.setSensitivity)

        senseDecBtn = QPushButton("−")
        senseDecBtn.setFont(btnFont)
        senseDecBtn.setMinimumHeight(64)
        senseDecBtn.setMinimumWidth(80)
        senseDecBtn.clicked.connect(self.decreaseSensitivity)

        senseIncBtn = QPushButton("+")
        senseIncBtn.setFont(btnFont)
        senseIncBtn.setMinimumHeight(64)
        senseIncBtn.setMinimumWidth(80)
        senseIncBtn.clicked.connect(self.increaseSensitivity)

        senseRow = QHBoxLayout()
        senseRow.setSpacing(10)
        senseRow.addWidget(senseDecBtn)
        senseRow.addWidget(self.senseLine)
        senseRow.addWidget(senseIncBtn)

        pollLabel = QLabel("POLLING RATE  (/sec)")
        pollLabel.setFont(labelFont)

        self.pollRateLine = QLineEdit(str(pollRate))
        self.pollRateLine.setFont(inputFont)
        self.pollRateLine.setMinimumHeight(64)
        self.pollRateLine.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.pollRateLine.textChanged.connect(self.setPollingRate)

        pollDecBtn = QPushButton("−")
        pollDecBtn.setFont(btnFont)
        pollDecBtn.setMinimumHeight(64)
        pollDecBtn.setMinimumWidth(80)
        pollDecBtn.clicked.connect(self.decreasePollRate)
        pollIncBtn = QPushButton("+")
        pollIncBtn.setFont(btnFont)
        pollIncBtn.setMinimumHeight(64)
        pollIncBtn.setMinimumWidth(80)
        pollIncBtn.clicked.connect(self.increasePollRate)

        pollRow = QHBoxLayout()
        pollRow.setSpacing(10)
        pollRow.addWidget(pollDecBtn)
        pollRow.addWidget(self.pollRateLine)
        pollRow.addWidget(pollIncBtn)

        self.invertBtn = QPushButton("Invert Y: ON" if invertY else "Invert Y: OFF")
        self.invertBtn.setObjectName("invertBtn")
        self.invertBtn.setFont(btnFont)
        self.invertBtn.setMinimumHeight(70)
        self.invertBtn.setCheckable(True)
        self.invertBtn.setChecked(invertY)
        self.invertBtn.setProperty("checked", invertY)
        self.invertBtn.clicked.connect(self.toggleInvertY)

        # --- Layout ---
        layout = QVBoxLayout()
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        layout.addWidget(self.startJoy)
        layout.addWidget(self.keyLabel)
        layout.addSpacing(8)
        layout.addWidget(self.invertBtn)
        layout.addSpacing(8)
        layout.addWidget(senseLabel)
        layout.addLayout(senseRow)
        layout.addSpacing(8)
        layout.addWidget(pollLabel)
        layout.addLayout(pollRow)

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

    def toggleInvertY(self):
        global invertY
        invertY = not invertY
        self.invertBtn.setText("Invert Y: ON" if invertY else "Invert Y: OFF")
        self.invertBtn.setProperty("checked", invertY)
        self.invertBtn.style().unpolish(self.invertBtn)
        self.invertBtn.style().polish(self.invertBtn)
        _save_config()
        print("Invert Y:", invertY)

    def closeEvent(self, event):
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
        
        
    def _onLoopStopped(self):
        self._running = False
        self.startJoy.setText("▶  Start")
        self.startJoy.setProperty("running", False)
        self.startJoy.style().unpolish(self.startJoy)
        self.startJoy.style().polish(self.startJoy)

    def toggleRun(self):
        if self._running:
            stop_event.set()
            self._running = False
            self.startJoy.setText("▶  Start")
            self.startJoy.setProperty("running", False)
        else:
            stop_event.clear()
            self._running = True
            self.startJoy.setText("⏹  Stop")
            self.startJoy.setProperty("running", True)
            t = threading.Thread(target=self._pollLoop, daemon=True)
            t.start()
        self.startJoy.style().unpolish(self.startJoy)
        self.startJoy.style().polish(self.startJoy)

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

            gamepad.left_joystick(x_value=0, y_value=mousey)
            gamepad.update()
            time.sleep(1 / pollRate)

        # release joystick when stopped
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

