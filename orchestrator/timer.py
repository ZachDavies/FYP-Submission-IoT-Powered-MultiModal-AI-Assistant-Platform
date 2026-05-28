import threading
import time
import uuid
import subprocess
import os
import tempfile
import struct, math, wave, io
from datetime import datetime, timedelta
from typing import Callable

BEEP_INTERVAL_SECS = 3
ACK_TIMEOUT_SECS   = 90

def _play_beep():

    sample_rate  = 16000
    beep_dur     = 0.12
    gap_dur      = 0.08
    num_beeps    = 7
    freq         = 880.0
    volume       = 0.7

    frames = bytearray()

    for b in range(num_beeps):
        num_samples = int(sample_rate * beep_dur)
        for i in range(num_samples):
            t = i / sample_rate
            if t < beep_dur * 0.1:
                env = t / (beep_dur * 0.1)
            elif t > beep_dur * 0.8:
                env = 1.0 - (t - beep_dur * 0.8) / (beep_dur * 0.2)
            else:
                env = 1.0
            sample = int(32767 * volume * env * math.sin(2 * math.pi * freq * t))
            frames += struct.pack("<h", sample)

        if b < num_beeps - 1:
            gap_samples = int(sample_rate * gap_dur)
            frames += b"\x00\x00" * gap_samples

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))

    wav_bytes = buf.getvalue()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp = f.name

    try:
        subprocess.run(
            ["mpv", "--no-video", "--really-quiet", tmp],
            stdin=subprocess.DEVNULL,
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

# Data classes

class TimerEntry:
    def __init__(
        self,
        timer_id: str,
        label: str,
        fire_at: float,
        duration_secs: float | None,
        alarm_time_str: str | None,
        tts_cb: Callable[[str], None],
        cancel_fn: Callable[[], None],
        on_fire: Callable[[], None] | None = None,
    ):
        self.timer_id       = timer_id
        self.label          = label
        self.fire_at        = fire_at
        self.duration_secs  = duration_secs
        self.alarm_time_str = alarm_time_str
        self.tts_cb         = tts_cb
        self._cancel_fn     = cancel_fn
        self.cancelled      = False
        self.firing         = False
        self._ack_event     = threading.Event()
        self.on_fire        = on_fire

    def cancel(self):
        self.cancelled = True
        self._ack_event.set()
        self._cancel_fn()

    def acknowledge(self): # Allow user to dismiss alarm/timer 
        self._ack_event.set()

    def seconds_remaining(self) -> float:
        return max(0.0, self.fire_at - time.time())

    def describe(self) -> str:
        remaining = self.seconds_remaining()
        mins, secs = divmod(int(remaining), 60)
        hours, mins = divmod(mins, 60)
        if hours:
            remaining_str = f"{hours}h {mins}m {secs}s"
        elif mins:
            remaining_str = f"{mins}m {secs}s"
        else:
            remaining_str = f"{secs}s"

        if self.alarm_time_str:
            return f"{self.label} (alarm at {self.alarm_time_str}, {remaining_str} remaining)"
        return f"{self.label} ({remaining_str} remaining)"

class TimerManager:

    def __init__(self, tts_fn: Callable[[str], None]):
        self._tts    = tts_fn
        self._lock   = threading.Lock()
        self._timers: dict[str, TimerEntry] = {}

    def set_timer(self, duration_secs: float, label: str = "Timer", on_fire: Callable[[], None] | None = None,) -> TimerEntry:
        fire_at = time.time() + duration_secs
        return self._schedule(label, fire_at, duration_secs=duration_secs, alarm_time_str=None, on_fire=on_fire)

    def set_alarm(self, hour: int, minute: int, label: str = "Alarm") -> TimerEntry | None:
        now    = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        alarm_time_str = f"{hour:02d}:{minute:02d}"
        return self._schedule(label, target.timestamp(), duration_secs=None, alarm_time_str=alarm_time_str)

    def cancel_timer(self, timer_id: str) -> bool:
        with self._lock:
            entry = self._timers.get(timer_id)
        if entry:
            entry.cancel()
            with self._lock:
                self._timers.pop(timer_id, None)
            return True
        return False

    def cancel_by_label(self, label: str) -> list[str]:
        label_lower = label.lower()
        with self._lock:
            matches = [e for e in self._timers.values() if label_lower in e.label.lower()]
        cancelled = []
        for entry in matches:
            entry.cancel()
            with self._lock:
                self._timers.pop(entry.timer_id, None)
            cancelled.append(entry.label)
        return cancelled

    def acknowledge_firing(self) -> list[str]:
        """
        Dismiss all currently-firing (beeping) timers.
        Call this from your wake-word detected / STT received hook.
        Returns list of acknowledged labels.
        """
        with self._lock:
            firing = [e for e in self._timers.values() if e.firing]
        acked = []
        for entry in firing:
            entry.acknowledge()
            acked.append(entry.label)
        return acked

    def has_firing(self) -> bool:
        with self._lock:
            return any(e.firing for e in self._timers.values())

    def list_timers(self) -> list[TimerEntry]:
        with self._lock:
            return [e for e in self._timers.values() if not e.cancelled]

    def build_context(self) -> str:
        entries = self.list_timers()
        if not entries:
            return "Active timers/alarms: none."
        lines = ["Active timers/alarms:"]
        for e in entries:
            lines.append(f"  - [{e.timer_id[:8]}] {e.describe()}")
        return "\n".join(lines)

    def _schedule(
        self,
        label: str,
        fire_at: float,
        duration_secs: float | None,
        alarm_time_str: str | None,
        on_fire: Callable[[], None] | None = None,
    ) -> TimerEntry:
        timer_id = str(uuid.uuid4())
        t = threading.Timer(fire_at - time.time(), self._fire, args=[timer_id])
        t.daemon = True

        entry = TimerEntry(
            timer_id=timer_id,
            label=label,
            fire_at=fire_at,
            duration_secs=duration_secs,
            alarm_time_str=alarm_time_str,
            tts_cb=self._tts,
            cancel_fn=t.cancel,
            on_fire=on_fire,
        )

        with self._lock:
            self._timers[timer_id] = entry

        t.start()
        return entry

    def _fire(self, timer_id: str):
        with self._lock:
            entry = self._timers.get(timer_id)
        if entry is None or entry.cancelled:
            return

        print(f"[timer] Fired: {entry.label}")
        entry.firing = True

        if entry.on_fire is not None:
            try:
                entry.on_fire()   # silent: no beep
                print(f"[timer] on_fire callback executed for '{entry.label}'")
            finally:
                with self._lock:
                    self._timers.pop(timer_id, None)
            return

        def beep_loop():
            deadline = time.time() + ACK_TIMEOUT_SECS
            while not entry._ack_event.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    print(f"[timer] Auto-dismissed '{entry.label}' after {ACK_TIMEOUT_SECS}s")
                    with self._lock:
                        self._timers.pop(timer_id, None)  # Only auto-dismiss removes it
                    break
                entry._ack_event.wait(timeout=min(BEEP_INTERVAL_SECS, remaining))
                if not entry._ack_event.is_set():
                    _play_beep()

        t = threading.Thread(target=beep_loop, daemon=True)
        t.start()


