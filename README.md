# Maratron TreadMouse

Converts mouse movement into virtual joystick movement for use with the [Maratron](https://www.youtube.com/watch?v=EzYy1MZocXU) manual treadmill VR setup (instead of REWASD).

Forked from [Fer Sler's project](https://github.com/fer-sler/VR-Treadmill), updated UI and added persistent settings.

---

## Requirements

Python 3 and the following packages:

```
pip install pynput vgamepad PyQt6
```

---

## Setup & Usage

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
Go to **Settings → Controllers → Manage Controller Bindings**, select **Gamepad**, and edit the binding for your game. Map the **left joystick** to locomotion/movement.

**5. Start your game**

**6. Begin treadmill tracking**  
Switch to the Maratron TreadMouse window and click **▶ Start** (or use the hotkey **Ctrl + `**). Switch back to your game and walk.  
To pause tracking, switch back to Maratron TreadMouse and click **⏹ Stop**.

---

## UI Controls

| Control | Description |
|---|---|
| **▶ Start / ⏹ Stop** | Toggle mouse-to-joystick tracking |
| **Ctrl + `** | Global hotkey to toggle tracking from anywhere |
| **Sensitivity** | How strongly mouse movement maps to joystick. Use − / + to adjust by 10. |
| **Polling Rate** | How many times per second the mouse position is read. Use − / + to adjust by 5. |
| **Invert Y** | Flip the up/down joystick direction |

Settings (sensitivity, polling rate, invert Y) are saved automatically and restored on next launch.

