import json
import os
import sys

DEBUG = "--debug" in sys.argv

_CONFIG_PATH = os.path.join(os.environ["APPDATA"], "Maratron Locomotion", "config.json")
_DEFAULTS = {
    "sensitivity": 35, "pollRate": 30, "invertY": False,
    "snapThreshold": 120, "snapDuration": 400, "snapReturnDelay": 1000,
    "vmcPort": 39539, "mouseEnabled": True, "hipEnabled": False,
    "snapUseKeyboard": False,
}

def _load_config():
    try:
        with open(_CONFIG_PATH) as f:
            return {**_DEFAULTS, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return _DEFAULTS.copy()

def _save_config():
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump({
            "sensitivity": sensitivity, "pollRate": pollRate, "invertY": invertY,
            "snapThreshold": snapThreshold, "snapDuration": snapDuration,
            "snapReturnDelay": snapReturnDelay, "vmcPort": vmcPort,
            "mouseEnabled": mouseEnabled, "hipEnabled": hipEnabled,
            "snapUseKeyboard": snapUseKeyboard,
        }, f, indent=2)

_cfg = _load_config()
sensitivity     = _cfg["sensitivity"]
pollRate        = _cfg["pollRate"]
invertY         = _cfg["invertY"]
snapThreshold   = _cfg["snapThreshold"]
snapDuration    = _cfg["snapDuration"]
snapReturnDelay = _cfg["snapReturnDelay"]
vmcPort         = _cfg["vmcPort"]
mouseEnabled    = _cfg["mouseEnabled"]
hipEnabled      = _cfg["hipEnabled"]
snapUseKeyboard = _cfg["snapUseKeyboard"]
snapKeyLeft     = 'q'
snapKeyRight    = 'e'
