"""LGI dependency self-check (runbook step 10).

Run:  D:/LGI/venv312/Scripts/python.exe D:/LGI/dep_probe.py
Exit: 0 = all green (or graceful-degradation modules absent per LGI.md s11),
      1 = any hard failure listed above.
"""
from __future__ import annotations

import importlib
import sys

MODULES: list[str] = [
    # requirements.txt (in manifest order)
    "PyQt6", "faster_whisper", "pyaudio", "numpy", "sounddevice", "kokoro",
    "mss", "cv2", "dxcam", "pytesseract", "pygetwindow", "pyperclip",
    "keyboard", "requests",
    # v1.2 perception stack
    "mediapipe", "ultralytics", "socketio",
    # CUDA runtime (probed with version + device below)
    "torch",
]


def main() -> int:
    fails = 0
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
            version = getattr(mod, "__version__", "?")
            print(f"OK   {name:<16} {version}")
        except Exception as exc:  # noqa: BLE001 - probe must survive anything
            text = f"{type(exc).__name__}: {str(exc)[:110]}"
            if name == "dxcam" and "COMError" in type(exc).__name__:
                # Desktop Duplication only exists in an interactive desktop
                # session: under ssh dxcam raises COMError on import, which is
                # expected; screen_capture.py degrades to mss (LGI.md s11).
                print(f"SOFT {name:<16} {text}  (expected under ssh; verify on desktop)")
            else:
                fails += 1
                print(f"FAIL {name:<16} {text}")

    try:
        import torch

        cuda = torch.cuda.is_available()
        device = torch.cuda.get_device_name(0) if cuda else "CPU-ONLY"
        print(f"TORCH version={torch.__version__} cuda={cuda} device={device}")
        if not cuda:
            fails += 1
    except Exception as exc:  # noqa: BLE001
        fails += 1
        print(f"FAIL torch-cuda     {type(exc).__name__}: {str(exc)[:110]}")

    print("DEPPROBE:", "OK" if fails == 0 else f"{fails} FAILURE(S)")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())