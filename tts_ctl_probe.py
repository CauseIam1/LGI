"""
LGI — Looking Glass Interface
tts_ctl_probe.py: v1.6.3 playback-control verification (runs ON the X17).

Exercises the exact calls the HUD buttons make against the real KokoroTTS:
  ⏸/▶ pause/resume hold semantics (offset capture + remainder replay),
  ⏹ Cancel (stop(): current utterance + queued, and pause-hold clearing),
  🔇/🔊 Voice gating (set_enabled(False) → speak() drops before synthesis).
Prints a PASS/FAIL matrix; exit code 0 only when all checks pass.
"""

from __future__ import annotations

import time

from audio_tts import KokoroTTS

_BASE = "Playback control probe sentence number {n}. The quick brown fox jumps over the lazy dog. "


def _long(n: int) -> str:
    """Tripled sentence; each copy stamped with the case number."""
    return (_BASE * 3).format(n=n)


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(("PASS  " if cond else "FAIL  ") + name, flush=True)
        if not cond:
            failures.append(name)

    states: list[str] = []
    tts = KokoroTTS(log=print, status_callback=states.append)
    tts.start()

    def wait_speaking(timeout: float = 90.0) -> bool:
        deadline = time.time() + timeout
        while not tts.is_speaking and time.time() < deadline:
            time.sleep(0.2)
        return tts.is_speaking

    def wait_idle(timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while tts.is_speaking and time.time() < deadline:
            time.sleep(0.2)
        return not tts.is_speaking

    # ── ⏸/▶ pause/resume mid-playback ──────────────────────────────────── #
    ok = tts.speak(_long(1))
    check("speak() accepted long utterance", ok)
    check("engine began speaking", wait_speaking())
    # ⏸ must land MID-CHUNK (audible playback), not in the silent synthesis
    # gap: give the first chunk time to dispatch before engaging the hold.
    time.sleep(3.0)
    tts.pause()
    # the 20ms poll gate must engage within a couple of poll ticks
    time.sleep(0.6)
    check("pause hold engaged mid-chunk", tts.is_speaking and "paused" in states)
    tts.resume()
    time.sleep(1.0)
    check("resume released the hold (speaking re-notified)",
          tts.is_speaking and states.count("speaking") >= 2)

    # ── ⏹ Cancel: current + queued, then confirm the queue really drained ─ #
    tts.speak("Queued utterance one.")
    tts.speak("Queued utterance two.")
    tts.stop()
    check("cancel cleared the speaking session", wait_idle())
    time.sleep(1.0)
    check("queued items did not resurrect after cancel", not tts.is_speaking)

    # ── ⏹ Cancel while a pause hold is active must not deadlock ─────────── #
    tts.speak(_long(2))
    check("engine speaking (pause-hold cancel case)", wait_speaking())
    tts.pause()
    time.sleep(0.5)
    tts.stop()
    check("cancel cleared the pause hold", wait_idle())

    # ── 🔇/🔊 Voice master gate ────────────────────────────────────────── #
    tts.set_enabled(False)
    check("speak() gated when voice disabled", tts.speak("nope") is False)
    check("enabled property reflects the gate", tts.enabled is False)
    tts.set_enabled(True)
    check("speak() accepted after re-enable", tts.speak("Voice re-enabled.") is True)
    tts.stop()
    wait_idle()

    tts.shutdown()
    print("states:", states, flush=True)
    print("RESULT:", "ALL PASS" if not failures
          else f"{len(failures)} FAILURES: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())