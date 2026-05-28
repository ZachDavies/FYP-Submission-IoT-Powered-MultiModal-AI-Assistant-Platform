import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import os

TIMER_LOG_PATH = os.getenv("TIMER_LOG_PATH", "timer_log.jsonl")

@dataclass
class PipelineTimer:
    """
    Holds wall-clock timestamps (seconds, from time.perf_counter) for every
    stage of one voice interaction.  Call .stamp(stage) at each boundary.

    Stages (in order):
        wake_detected   – OWW threshold crossed, wake word confirmed
        record_start    – record_once() begins draining stale frames
        record_end      – record_once() returns the numpy audio array
        asr_start       – stt_transcribe() called
        asr_end         – stt_transcribe() returns text
        llm_start       – requests.post() called to Ollama
        llm_first_token – first non-empty token received from the stream
        llm_end         – streaming loop exits (done=True)
        tts_start       – tts_say() called with the natural-language string
        tts_first_byte  – Piper: piper subprocess returns / Kokoro: PCM written to tmp
        tts_play_end    – mpv process exits (audio finished)
    """

    interaction_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    utterance_text: str = ""

    wake_detected:   Optional[float] = None
    record_start:    Optional[float] = None
    record_end:      Optional[float] = None
    asr_start:       Optional[float] = None
    asr_end:         Optional[float] = None
    llm_start:       Optional[float] = None
    llm_first_token: Optional[float] = None
    llm_end:         Optional[float] = None
    tts_start:       Optional[float] = None
    tts_play_end:    Optional[float] = None

    def stamp(self, stage: str) -> None:
        """Record the current perf_counter time for the named stage."""
        if hasattr(self, stage):
            setattr(self, stage, time.perf_counter())
        else:
            raise ValueError(f"Unknown stage: {stage!r}")

    def _diff_ms(self, a: str, b: str) -> Optional[float]:
        ta = getattr(self, a)
        tb = getattr(self, b)
        if ta is None or tb is None:
            return None
        return round((tb - ta) * 1000, 1)

    @property
    def recording_ms(self) -> Optional[float]:
        return self._diff_ms("record_start", "record_end")

    @property
    def asr_ms(self) -> Optional[float]:
        return self._diff_ms("asr_start", "asr_end")

    @property
    def llm_ttft_ms(self) -> Optional[float]:
        return self._diff_ms("llm_start", "llm_first_token")

    @property
    def llm_total_ms(self) -> Optional[float]:
        return self._diff_ms("llm_start", "llm_end")

    @property
    def end_to_end_ms(self) -> Optional[float]:
        return self._diff_ms("record_end", "tts_play_end")
    
    @property
    def tts_total_ms(self) -> Optional[float]:
        return self._diff_ms("tts_start", "tts_play_end")

    @property
    def wake_to_play_end_ms(self) -> Optional[float]:
        return self._diff_ms("wake_detected", "tts_play_end")

    def to_dict(self) -> dict:
        return {
            "interaction_id": self.interaction_id,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "utterance_text": self.utterance_text,
            "recording_ms":       self.recording_ms,
            "asr_ms":             self.asr_ms,
            "llm_ttft_ms":        self.llm_ttft_ms,
            "llm_total_ms":       self.llm_total_ms,
            "tts_total_ms":       self.tts_total_ms,
            "end_to_end_ms":      self.end_to_end_ms,
            "wake_to_play_end_ms": self.wake_to_play_end_ms,
            "raw_offsets_ms": _relative_offsets(self),
        }

    def log(self, path: str = TIMER_LOG_PATH) -> None:
        record = self.to_dict()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _print_summary(record)


def _relative_offsets(t: PipelineTimer) -> dict:
    stages = [
        "wake_detected", "record_start", "record_end",
        "asr_start", "asr_end",
        "llm_start", "llm_first_token", "llm_end",
        "tts_start", "tts_play_end",
    ]
    values = {s: getattr(t, s) for s in stages}
    origin = next((v for v in values.values() if v is not None), None)
    if origin is None:
        return {}
    return {
        s: round((v - origin) * 1000, 1) if v is not None else None
        for s, v in values.items()
    }


def _print_summary(r: dict) -> None:
    def _fmt(v):
        return f"{v:.0f} ms" if v is not None else "—"

    print(
        f"[timing] id={r['interaction_id'][:8]}  "
        f"rec={_fmt(r['recording_ms'])}  "
        f"asr={_fmt(r['asr_ms'])}  "
        f"llm_ttft={_fmt(r['llm_ttft_ms'])}  "
        f"llm={_fmt(r['llm_total_ms'])}  "
        f"tts={_fmt(r['tts_total_ms'])}  "
        f"e2e={_fmt(r['end_to_end_ms'])}"
    )
