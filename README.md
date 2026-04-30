# Maratron TreadMouse

Allows you to use mouse movement for forward/backward locomotion in VR — no gamepad required.

**Mouse → Virtual Gamepad's left joystick → SteamVR binding → in-game locomotion**  
**Phone on hip → owoTrack → SlimeVR → Virtual Gamepad's right joystick → SteamVR snap turn**

Designed for use with the [Maratron](https://www.youtube.com/watch?v=EzYy1MZocXU) manual treadmill VR setup (instead of REWASD).

Forked from [Fer Sler's project](https://github.com/fer-sler/VR-Treadmill), updated UI and added persistent settings.

---

## Requirements

* Windows, SteamVR
* Ideally a gaming mouse with high DPI, mounted on/under the treadmill's belt
* Python 3 and the following packages:

```
pip install pynput vgamepad PyQt6 python-osc
```

`vgamepad` will prompt you to install [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases) if needed — a Windows driver that creates virtual Xbox/DS4 controllers the OS treats as real hardware.

---

## Usage

In my tests I followed these steps **in order** each session:

**1. Start Virtual Desktop**  
Launch Virtual Desktop on your PC and in your headset. Connect as normal.

**2. Run the script**  
```
python treadmill.py
```  
This registers a virtual Xbox 360 gamepad over USB. The Maratron TreadMouse window will open — don't click Start yet.

**3. Start SteamVR**  
Once SteamVR loads, the virtual gamepad should appear as a connected/active controller in the SteamVR device icons.

**4. Configure controller bindings in SteamVR**  
Go to **Settings → Controllers → Manage Controller Bindings**, select **Gamepad**, and edit the binding for your game. Map the **left joystick** to locomotion/movement and the **right joystick** to snap turn.

**5. Begin treadmill tracking**  
Click **▶ Start** in the Maratron TreadMouse window. 

**6. Start your game**

---

## Hip Snap Turns (optional)

Mount an Android phone on your hip, install [owoTrack](https://play.google.com/store/apps/details?id=org.owoTrack.app), and run [SlimeVR Server](https://github.com/SlimeVR/SlimeVR-Server/releases). The phone acts as a hip IMU tracker.

Additional setup steps (do these once):

1. In SlimeVR Server, assign the phone tracker to the **Hip** slot.
2. Go to **SlimeVR Settings → OSC/VMC**, enable **VMC output**, and set **Port Out** to **39539**.
3. In the Maratron TreadMouse window, click **Hip Tracking: OFF** to toggle it on. The script will start listening for VMC data on UDP port 39540.

A fast clockwise hip twist fires a **snap turn right**; anti-clockwise fires **snap turn left**. Adjust **Snap Threshold** to taste — lower values are more sensitive.


---

## Tested Game Bindings

### Half-Life 2: VR Mod

The following SteamVR **Gamepad** binding configuration was verified to work for both mouse locomotion and hip snap turns:

| Thumbstick | Use as | Variable | Value |
|---|---|---|---|
| Left | Joystick | Position | Move |
| Right | D-Pad | Mode | Touch |
| Right | D-Pad | East | Turn Right |
| Right | D-Pad | West | Turn Left |

---

## UI Controls

| Control | Description |
|---|---|
| **▶ Start / ⏹ Stop** | Toggle tracking |
| **Ctrl + `** | Hotkey to toggle tracking |
| **Sensitivity** | Controls in-game walking speed |
| **Polling Rate** | Mouse reads per second — higher is smoother |
| **Invert Y** | Flip forward/backward direction |
| **Hip Tracking** | Toggle hip snap-turn tracking (requires SlimeVR + owoTrack) |
| **Snap Threshold** | Minimum yaw rate (°/s) to trigger a snap turn |

Settings are saved automatically.

---

## Troubleshooting

**SteamVR stops seeing the virtual gamepad mid-session (e.g. after switching from game to virtual desktop and back)**  
Close SteamVR and Steam desktop apps, then relaunch the game. 

