import ctypes
import ctypes.wintypes
import math as _math
import threading
import time

import vgamepad as vg
from pynput.keyboard import Controller as _KbController
from pythonosc import dispatcher, osc_server

import config

gamepad      = vg.VX360Gamepad()
print("[Gamepad] Virtual Xbox 360 controller connected")
gamepad_lock = threading.Lock()
stop_event   = threading.Event()
_kb          = _KbController()

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
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid))

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


# -------------------------------------------------------------------
# Hip Snap-Turn — listens for SlimeVR VMC/OSC output, detects fast
# hip yaw twists, and fires brief right-joystick pulses on the gamepad.
# -------------------------------------------------------------------
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
        self._last_snap_sign = 0
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
        self._server       = _ReusableOSCServer(("0.0.0.0", config.vmcPort), disp)
        self._running      = True
        self._connected    = False
        self._stall_warned = False
        self._prev_yaw     = None
        self._prev_time    = None
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        print(f"[Hip] Listening on UDP {config.vmcPort}...")

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
                          f"is SlimeVR VMC output enabled on port {config.vmcPort}?")
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
                if abs(rate) > config.snapThreshold:
                    sign    = 1 if rate > 0 else -1
                    elapsed = now - self._last_snap
                    return_blocked = (
                        elapsed < config.snapReturnDelay / 1000.0
                        and self._last_snap_sign != 0
                        and sign != self._last_snap_sign
                    )
                    if not return_blocked and elapsed > self._COOLDOWN:
                        self._do_snap(sign)

        # Throttled status print
        if config.DEBUG and now - self._last_status >= self._STATUS_EVERY:
            self._last_status = now
            print(f"[Hip] yaw: {yaw:+.1f}°  rate: {rate:.0f} °/s")

        self._prev_yaw  = yaw
        self._prev_time = now

    def _do_snap(self, sign):
        self._last_snap      = time.monotonic()
        self._last_snap_sign = sign
        direction = 'right' if sign > 0 else 'left'
        print(f"[Hip] snap {direction}")

        if config.snapUseKeyboard:
            key = config.snapKeyRight if sign > 0 else config.snapKeyLeft
            def _kb_pulse():
                _kb.press(key)
                time.sleep(config.snapDuration / 1000.0)
                _kb.release(key)
            threading.Thread(target=_kb_pulse, daemon=True).start()
        else:
            axis = 32767 if sign > 0 else -32768
            hold = config.snapDuration / 1000.0
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


_raw_reader = _RawMouseReader()
hip_turner  = HipSnapTurner()
