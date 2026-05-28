#!/usr/bin/env python
import sys, os
os.environ["ATEN_CPU_CAPABILITY"] = "default"
os.environ["OMP_NUM_THREADS"]     = "1"
os.environ["MKL_NUM_THREADS"]     = "1"

import struct
import numpy as np
from kokoro import KPipeline

voice  = sys.argv[1] if len(sys.argv) > 1 else "af_sarah"
stderr = open(os.devnull, "w")  # silence warnings on stderr

pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")

# Signal ready to parent
sys.stdout.buffer.write(b"READY\n")
sys.stdout.buffer.flush()

for line in sys.stdin:
    text = line.strip()
    if not text:
        sys.stdout.buffer.write(struct.pack("<I", 0))
        sys.stdout.buffer.flush()
        continue

    chunks = []
    try:
        for _, _, audio in pipeline(text, voice=voice):
            chunks.append(audio)
    except Exception as e:
        print(f"[kokoro_worker] error: {e}", file=sys.stderr)
        sys.stdout.buffer.write(struct.pack("<I", 0))
        sys.stdout.buffer.flush()
        continue

    if chunks:
        pcm = np.concatenate(chunks).astype(np.float32).tobytes()
    else:
        pcm = b""

    sys.stdout.buffer.write(struct.pack("<I", len(pcm)))
    sys.stdout.buffer.write(pcm)
    sys.stdout.buffer.flush()