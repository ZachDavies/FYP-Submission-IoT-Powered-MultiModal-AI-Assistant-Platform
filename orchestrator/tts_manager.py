import subprocess, struct, threading, queue, tempfile, os, sys
import numpy as np

KOKORO_WORKER = os.path.join(os.path.dirname(__file__), "kokoro_worker.py")
SAMPLE_RATE   = 24000

class TTSManager:
    def __init__(self, voice: str = "af_sarah"):
        self.voice      = voice
        self._lock      = threading.Lock()
        self._interrupt = threading.Event()
        self._worker    = None
        self._mpv_proc  = None
        self._seq       = 0
        self._active_seq = -1
        self._cache = {}
        self._start_worker()
        self.precache(["Yes?"])

    def _start_worker(self):
        self._worker = subprocess.Popen(
            [sys.executable, KOKORO_WORKER, self.voice],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        # Wait for READY
        self._worker.stdout.readline()
        print("[tts] Kokoro worker ready.")


    def speak(self, text: str):
            print(f"[tts] TTSManager.speak: {text}")
            self.interrupt()
            with self._lock:
                self._interrupt.clear()
                seq = self._seq
                self._seq += 1
                self._active_seq = seq
            t = threading.Thread(target=self._synth_and_play, args=(text, seq), daemon=True)
            t.start()



    def interrupt(self):
        self._interrupt.set()
        self._stop_mpv()


    def wait(self):
        import time
        while self._active_seq >= 0:
            time.sleep(0.05)


    def precache(self, phrases): # Pre Synthesize common phrases to reduce latency.
            for phrase in phrases:
                if phrase in self._cache:
                    continue
                try:
                    line = (phrase + "\n").encode()
                    with self._lock:
                        self._worker.stdin.write(line)
                        self._worker.stdin.flush()
                    header = self._worker.stdout.read(4)
                    if len(header) < 4:
                        continue
                    n_bytes = struct.unpack("<I", header)[0]
                    if n_bytes == 0:
                        continue
                    pcm_bytes = self._worker.stdout.read(n_bytes)
                    self._cache[phrase] = pcm_bytes
                    print(f"[tts] Cached '{phrase}' ({len(pcm_bytes)} bytes)")
                except Exception as e:
                    print(f"[tts] Cache error for '{phrase}': {e}")

    def _synth_and_play(self, text: str, seq: int):
            try:
                # Try cache first for instant playback
                pcm_bytes = self._cache.get(text)
                if pcm_bytes is None:
                    # Send to worker
                    line = (text + "\n").encode()
                    with self._lock:
                        self._worker.stdin.write(line)
                        self._worker.stdin.flush()


                    # Read length prefix (4 bytes)
                    header = self._worker.stdout.read(4)
                    if len(header) < 4:
                        return
                    n_bytes = struct.unpack("<I", header)[0]


                    if n_bytes == 0:
                        return


                    pcm_bytes = self._worker.stdout.read(n_bytes)


                # Discard if interrupted or superseded
                if self._interrupt.is_set() or seq != self._active_seq:
                    return


                self._play_pcm(pcm_bytes)


            except Exception as e:
                print(f"[tts] synth error: {e}", file=sys.stderr)
            finally:
                if seq == self._active_seq:
                    self._active_seq = -1


    def _play_pcm(self, pcm_bytes: bytes):
        if self._interrupt.is_set():
            return

        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
            f.write(pcm_bytes)
            tmp = f.name

        try:
            if self._interrupt.is_set():
                return
            proc = subprocess.Popen([
                "mpv",
                "--no-video",
                "--really-quiet",
                "--demuxer=rawaudio",
                "--demuxer-rawaudio-rate=24000",
                "--demuxer-rawaudio-channels=1",
                "--demuxer-rawaudio-format=floatle",
                tmp,
            ])
            with self._lock:
                self._mpv_proc = proc
            if self._interrupt.is_set():
                proc.terminate()
            proc.wait()
        finally:
            os.unlink(tmp)
            with self._lock:
                if self._mpv_proc is proc:
                    self._mpv_proc = None


    def _stop_mpv(self):
        with self._lock:
            proc = self._mpv_proc
        if proc and proc.poll() is None:
            proc.terminate()


    def shutdown(self):
        self._interrupt.set()
        self._stop_mpv()
        if self._worker and self._worker.poll() is None:
            try:
                self._worker.stdin.flush()
                self._worker.stdin.close()
            except Exception:
                pass
            try:
                self._worker.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._worker.kill()
        self._worker = None