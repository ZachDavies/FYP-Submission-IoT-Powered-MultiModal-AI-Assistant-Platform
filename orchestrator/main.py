import json
import requests
import time
import paho.mqtt.client as mqtt
import numpy as np
import speech_recognition as sr
import subprocess
import sys, os
import tempfile
import base64
import threading
import pyaudio
import queue
from audit import Auditor
from faster_whisper import WhisperModel
from timer import TimerManager
from flask import Flask, request, jsonify, render_template_string
from openwakeword.model import Model as WakeWordModel
from memory import upsert_macro, delete_macro, get_macro, list_macros, upsert_fact, delete_fact, build_memory_context
sys.path.insert(0, os.path.dirname(__file__))
from config import OLLAMA_URL, MODEL_NAME, MQTT_BROKER, MQTT_PORT, DEVICE_TOPIC_MAP, AUDIT_JSONL_PATH, DEVICES, MQTT_USER, MQTT_PASS, SENSOR_DATA
from tts_manager import TTSManager
from timing import PipelineTimer, TIMER_LOG_PATH


_tts_manager: TTSManager | None = None
_current_timer: PipelineTimer | None = None


# openWakeWord setup
_OWW_MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "wakeword-models", "olivia.onnx")
oww_model = WakeWordModel(
    wakeword_models=[_OWW_MODEL_PATH],
    enable_speex_noise_suppression=False,
    vad_threshold=0.5,  # Silero VAD guard skips non-speech frames
    inference_framework="onnx",
)

_OWW_THRESHOLD = 0.15
_OWW_CHUNK    = 1280   # 80 ms @ 16kHz minimum efficient frame for openWakeWord
_PYAUDIO_RATE = 16000
_PYAUDIO_CHANNELS = 1
_PYAUDIO_FORMAT = pyaudio.paInt16

_FOLLOWUP_TIMEOUT = 10.0

# STT setup
stt_model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
recognizer = sr.Recognizer()
recognizer.pause_threshold = 0.5
recognizer.non_speaking_duration = 0.4
recognizer.dynamic_energy_threshold = False
recognizer.energy_threshold = 300

STATE_CACHE_PATH = os.path.join(os.path.dirname(__file__), "device_state_cache.json")
SENSOR_CACHE_PATH = os.path.join(os.path.dirname(__file__), "sensor_state_cache.json")

print(f"[oww] model path: {_OWW_MODEL_PATH}")
print(f"[oww] exists: {os.path.exists(_OWW_MODEL_PATH)}")

# TTS setup Kokoro/Piper
_VOICES_DIR = os.path.join(os.path.dirname(__file__), "..", "piper-voices")
TTS_ENGINE: str = "piper" # option selector (kokoro/piper)
WAKE_WORDS = {"olivia", "olivia,", "olivia."}

_kokoro_ready = threading.Event()  
_tts_interrupt = threading.Event() 
_wake_event    = threading.Event() 
_last_wake_ts  = 0.0              
_wake_audio_queue: queue.Queue = queue.Queue()
_wake_stream_active = threading.Event()
_wake_stream_active.set()

def _audio_capture_loop():
    pa = pyaudio.PyAudio()
    stream = pa.open(
        rate=_PYAUDIO_RATE,
        channels=_PYAUDIO_CHANNELS,
        format=_PYAUDIO_FORMAT,
        input=True,
        frames_per_buffer=_OWW_CHUNK,
    )
    try:
        while True:
            pcm = stream.read(_OWW_CHUNK, exception_on_overflow=False)
            _wake_audio_queue.put(pcm)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

def listen_for_wake_word():
    global _last_wake_ts

    print("\n[idle] Waiting for 'Olivia' (openWakeWord)...")
    oww_model.reset()
    while True:
        pcm = _wake_audio_queue.get()
        audio_int16 = np.frombuffer(pcm, dtype=np.int16)
        prediction = oww_model.predict(audio_int16)
        for model_name, score in prediction.items():
            if score > 0.03:
                print(f"[oww] {model_name} = {score:.4f}")
            if score >= _OWW_THRESHOLD:
                now = time.time()
                # ignore wakes that fire within 3.0s of the previous one
                if now - _last_wake_ts < 3.0:
                    continue

                _last_wake_ts = now
                print(f"[wake] triggered '{model_name}' score={score:.3f}")
                oww_model.reset()
                while not _wake_audio_queue.empty():
                    _wake_audio_queue.get_nowait()
                global _current_timer
                _current_timer = PipelineTimer()
                _current_timer.stamp("wake_detected")
                return
        
def calibrate_mic():
    print("[startup] Calibrating microphone noise level...")
    settle_chunks = int(1.5 * _PYAUDIO_RATE / _OWW_CHUNK)
    energies = []
    for _ in range(settle_chunks):
        pcm = _wake_audio_queue.get()
        audio_int16 = np.frombuffer(pcm, dtype=np.int16)
        energies.append(np.abs(audio_int16).mean())
    recognizer.energy_threshold = max(300, float(np.mean(energies)) * 3)
    print(f"[startup] Energy threshold set to {recognizer.energy_threshold:.1f}")

def record_once():
    print("\nListening...")
    while not _wake_audio_queue.empty():
        _wake_audio_queue.get_nowait()

    if _current_timer:
        _current_timer.stamp("record_start")

    frames = []
    silence_chunks = 0
    max_silence = int(1.0 * _PYAUDIO_RATE / _OWW_CHUNK)  
    max_chunks   = int(12.0 * _PYAUDIO_RATE / _OWW_CHUNK) 

    for _ in range(max_chunks):
        if _wake_event.is_set():
            return None  # interrupted by wake word
        pcm = _wake_audio_queue.get()
        frames.append(pcm)
        # Simple energy check for silence detection
        audio_int16 = np.frombuffer(pcm, dtype=np.int16)
        energy = np.abs(audio_int16).mean()
        if energy < recognizer.energy_threshold * 0.5:
            silence_chunks += 1
            if silence_chunks >= max_silence and len(frames) > 20:
                break
        else:
            silence_chunks = 0

    if not frames:
        return None
    raw = b"".join(frames)
    if _current_timer:
        _current_timer.stamp("record_end")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def stt_transcribe(audio):
    if _current_timer:
        _current_timer.stamp("asr_start")
    segments, _ = stt_model.transcribe(
        audio, 
        language="en", 
        vad_filter=True, 
        vad_parameters={"min_silence_duration_ms": 100}, 
        beam_size=2, 
        best_of=2, 
        condition_on_previous_text=False, 
        initial_prompt=(
            "Smart home voice commands. Devices: living room light, bedroom light, "
            "fan, power strip, socket one, socket two, socket three, socket four, Mini-D relay, pi. "
            "USB hub, U-S-B hub. Commands: turn on, turn off, all, except, and, besides, but not, set to."
            "Temperature, Humidity, Battery, Motion Sensor, Sensors, Sensor, Environment Status, Status, State"
            "Olivia"
        ),
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        )
    text = " ".join(seg.text for seg in segments)
    if _current_timer:
        _current_timer.stamp("asr_end")
    
    # common Whisper hallucinations
    text = text.strip()
    hallucinations = ["Thank you.", "Thank you", "Thanks for watching.", "thanks.", "you", ".", ""]
    if text in hallucinations:
        return ""
        
    return text

def listen_for_followup() -> str | None:
    if _tts_manager is not None:
        _tts_manager.wait()

    if _wake_event.is_set():
        return None

    print("[conv] Listening for follow-up...")

    # Drain stale frames from TTS playback
    while not _wake_audio_queue.empty():
        _wake_audio_queue.get_nowait()

    frames = []
    silence_chunks = 0
    timeout_chunks  = int(_FOLLOWUP_TIMEOUT * _PYAUDIO_RATE / _OWW_CHUNK)
    max_silence     = int(1.5 * _PYAUDIO_RATE / _OWW_CHUNK) 
    max_chunks      = int(20.0 * _PYAUDIO_RATE / _OWW_CHUNK) 
    heard_speech    = False

    for i in range(max(timeout_chunks, max_chunks)):
        if _wake_event.is_set():
            print("[conv] Wake word fired during follow-up, aborting.")
            return None

        try:
            pcm = _wake_audio_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        audio_int16 = np.frombuffer(pcm, dtype=np.int16)
        energy = np.abs(audio_int16).mean()

        if energy >= recognizer.energy_threshold * 0.5:
            heard_speech = True
            silence_chunks = 0
            frames.append(pcm)
        else:
            if heard_speech:
                frames.append(pcm) 
                silence_chunks += 1
                if silence_chunks >= max_silence:
                    break
            elif i >= timeout_chunks:
                return None

    if not frames or not heard_speech:
        return None

    if _wake_event.is_set():
        return None

    raw = b"".join(frames)
    audio_np = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    global _current_timer
    _current_timer = PipelineTimer()
    _current_timer.stamp("record_end")

    text = stt_transcribe(audio_np)
    if text:
        print(f"User (follow-up): {text}")
        acked = timer_manager.acknowledge_firing()
        for label in acked:
            print(f"[timer] Acknowledged via follow-up speech: {label}")
    return text if text else None

_mpv_proc: subprocess.Popen | None = None
_mpv_lock = threading.Lock()

def tts_stop():
    global _mpv_proc
    with _mpv_lock:
        proc = _mpv_proc
        _mpv_proc = None
    if proc and proc.poll() is None:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()

def tts_say(text: str):
    print(f"Assistant: {text}")
    if _current_timer:
        _current_timer.stamp("tts_start")
    if TTS_ENGINE == "kokoro" and _tts_manager is not None:
        _tts_manager.speak(text)
        _tts_manager.wait()
        if _current_timer:
            _current_timer.stamp("tts_play_end")
    elif TTS_ENGINE == "piper":
        tts_say_piper(text)
    else:
        tts_stop()

def tts_say_piper(text: str):
    if _current_timer:
        _current_timer.stamp("tts_start")
    global _mpv_proc
    tts_stop()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    piper_model = os.path.join(_VOICES_DIR, "en_US-lessac-medium.onnx")
    subprocess.run(
        ["piper", "--model", piper_model, "--output_file", tmp_path],
        input=text.encode(),
        check=True
    )

    with _mpv_lock:
        _mpv_proc = subprocess.Popen(
            ["mpv", "--no-video", "--really-quiet", tmp_path],
            stdin=subprocess.DEVNULL
        )
    _mpv_proc.wait()

    if _current_timer:
        _current_timer.stamp("tts_play_end")

    try:
        os.unlink(tmp_path)
    except OSError:
        pass

def load_state_cache():
    # Seed DEVICES with last-known states from disk before MQTT responds.
    if not os.path.exists(STATE_CACHE_PATH):
        return
    try:
        with open(STATE_CACHE_PATH) as f:
            cache = json.load(f)
        for device_id, state in cache.items():
                if device_id in DEVICES:
                    if isinstance(state, dict):
                        DEVICES[device_id]["power"] = state.get("power", DEVICES[device_id]["power"])
                        for field in ("brightness", "color_temp", "color_xy"):
                            if field in state:
                                DEVICES[device_id][field] = state[field]
                    else:
                        DEVICES[device_id]["power"] = state
    except Exception as e:
        print(f"[startup] Could not load state cache: {e}")

def save_state_cache():
    # save current in-memory states to disk after every command.
    cache = {}
    for did, dev in DEVICES.items():
        entry = {"power": dev["power"]}
        for field in ("brightness", "color_temp", "color_xy"):
            if dev.get(field) is not None:
                entry[field] = dev[field]
        cache[did] = entry
    try:
        with open(STATE_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[cache] Could not save: {e}")

def request_device_states(wait_seconds: float = 1.5):
    queried = []
    for device_id, dev in DEVICES.items():
        if dev.get("tasmota") and dev.get("state_topic"):  # skip power_strip_all
            cmd_topic = DEVICE_TOPIC_MAP.get(device_id)
            if cmd_topic:
                mqtt_client.publish(cmd_topic, "", qos=1)
                queried.append(device_id)
    print(f"[startup] Queried live state for: {queried}")
    time.sleep(wait_seconds)  # let on_state_message populate DEVICES
    print(f"[startup] Device states after query: { {d: v['power'] for d, v in DEVICES.items()} }")

def load_sensor_cache():
    if not os.path.exists(SENSOR_CACHE_PATH):
        return
    try:
        with open(SENSOR_CACHE_PATH) as f:
            cache = json.load(f)
        for sid, vals in cache.items():
            if sid in SENSOR_DATA:
                SENSOR_DATA[sid].update(vals)
        print(f"[startup] Loaded sensor cache: {cache}")
    except Exception as e:
        print(f"[startup] Could not load sensor cache: {e}")

def save_sensor_cache():
    cache = {}
    for sid, s in SENSOR_DATA.items():
        cache[sid] = {k: v for k, v in s.items() if k not in ("state_topic", "name")}
    with open(SENSOR_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

# MQTT client
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)


def on_state_message(client, userdata, msg):
    payload_raw = msg.payload.decode("utf-8").strip()

    for key, sensor in SENSOR_DATA.items():
        if sensor["state_topic"] == msg.topic:
            try:
                data = json.loads(payload_raw)
                if "occupancy" in data:
                    sensor["occupancy"] = data["occupancy"]
                if "temperature" in data:
                    sensor["temperature"] = data["temperature"]
                if "humidity" in data:
                    sensor["humidity"] = data["humidity"]
                if "battery" in data:
                    sensor["battery"] = data["battery"]
                if "linkquality" in data:
                    sensor["linkquality"] = data["linkquality"]
                print(f"[sensor] {key}: {data}")
                save_sensor_cache()
            except json.JSONDecodeError:
                pass
            return

    # Existing device state handler
    for key, dev in DEVICES.items():
        if dev["state_topic"] == msg.topic:
            if dev.get("tasmota"):
                dev["power"] = "on" if payload_raw.upper() == "ON" else "off"
            elif dev.get("z2m"):
                try:
                    data = json.loads(payload_raw)
                    state = data.get("state", "").upper()
                    if state in ("ON", "OFF"):
                        dev["power"] = "on" if state == "ON" else "off"
                    if "brightness" in data:
                        dev["brightness"] = data["brightness"]
                    if "color_temp" in data:
                        dev["color_temp"] = data["color_temp"]
                    if "color" in data and isinstance(data["color"], dict):
                        xy = data["color"]
                        if "x" in xy and "y" in xy:
                            dev["color_xy"] = {"x": xy["x"], "y": xy["y"]}
                except json.JSONDecodeError:
                    pass
            elif dev.get("ha_relay"):
                dev["power"] = "on" if payload_raw.lower() == "on" else "off"
            else:
                try:
                    data = json.loads(payload_raw)
                    dev["power"] = data.get("power", dev["power"])
                except json.JSONDecodeError:
                    pass
            save_state_cache()
            break

mqtt_client.on_message = on_state_message
def on_connect(client, userdata, flags, reason_code, properties):
    for dev in DEVICES.values():
        if dev.get("state_topic"):
            client.subscribe(dev["state_topic"])
    # Subscribe to read-only sensors
    for sensor in SENSOR_DATA.values():
        client.subscribe(sensor["state_topic"])
mqtt_client.on_connect = on_connect
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)

for dev in DEVICES.values():
    if dev.get("state_topic"):
        mqtt_client.subscribe(dev["state_topic"])
mqtt_client.loop_start()
load_state_cache()
load_sensor_cache()
request_device_states(wait_seconds=1.5)
auditor = Auditor(mqtt_client)
timer_manager = TimerManager(tts_fn=tts_say)

def send_iot_command(device_id: str, action: str, interaction: dict | None = None):
    topic = DEVICE_TOPIC_MAP.get(device_id)
    if not topic:
        print(f"Unknown device_id: {device_id}")
        return

    dev = DEVICES.get(device_id, {})
    if dev.get("tasmota"):
        payload = action.upper()
    elif dev.get("z2m"):
        if isinstance(action, dict):
         # for rgb lights to be able to send more complex command such as rgb values and modes alongside power on/off command, e.g. {"power": "on", "mode": "color", "color": {"r": 255, "g": 0, "b": 0}}
            payload = json.dumps(action)
            # mirror power state if state key present
            state_val = action.get("state", "").upper()
            if state_val in {"ON", "OFF"} and device_id in DEVICES:
                DEVICES[device_id]["power"] = "on" if state_val == "ON" else "off"
        else:
            payload = json.dumps({"state": action.upper()})
    elif dev.get("ha_relay"): 
        payload = "on" # for mini-d relay due to inching settings.
    else:
        payload = json.dumps({"power": action})

    mqtt_client.publish(topic, payload, qos=1, retain=False)
    print(f"MQTT: {topic} <- {payload}")

    if device_id in DEVICES:
        DEVICES[device_id]["power"] = action

    if device_id == "power_strip_all":
        for sock in ("power_strip_socket1", "power_strip_socket2",
                     "power_strip_socket3", "power_strip_usb"):
            DEVICES[sock]["power"] = action

    save_state_cache()

    if interaction is not None:
        auditor.add_mqtt_action(interaction, topic, payload)

def _fmt_sensor(sid, s):
    if "occupancy" in s:
        bat = f"{s['battery']}%" if s["battery"] is not None else "unknown"
        return f"- {sid} (motion): occupancy={s['occupancy']}, battery={bat}"
    else:
        temp = f"{s['temperature']}°C" if s["temperature"] is not None else "unavailable"
        hum  = f"{s['humidity']}%"     if s["humidity"]    is not None else "unavailable"
        bat  = f"{s['battery']}%"      if s["battery"]     is not None else "unknown"
        return f"- {sid} (climate): temp={temp}, humidity={hum}, battery={bat}"

def call_ollama(
    user_text: str,
    image_b64_list: list[str] | None = None,
    extra_context: str | None = None,
    history: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Streaming version, sends sentence chunks as they arrive."""
    state_lines = "\n".join(
        f"- {device_id}: {info['power']}"
        for device_id, info in DEVICES.items()
    )
    sensor_lines = "\n".join(_fmt_sensor(sid, s) for sid, s in SENSOR_DATA.items())
    memory_context = build_memory_context()
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    timer_context = timer_manager.build_context()

    system_prompt = f"""You are a home assistant that controls IoT devices via JSON commands.

If the user speaks in a language other than English, respond to them in that same 
language. Device commands must still be emitted as JSON in the standard format 
regardless of language. Only the natural-language confirmation should be translated.

You speak your responses aloud via text-to-speech, so your natural language must be plain prose only.

FORMATTING RULES (mandatory):
- Never use markdown: no **, *, #, -, bullet points, numbered lists, or backticks.
- Never use symbols like asterisks, underscores, or hyphens for emphasis or structure.
- Write device names in plain English: say "living room light" not "living_room_light".
- Keep responses short and conversational, as if speaking out loud.

Current Time:
{time_str}

Current Memories:
{memory_context}

Current device states:
{state_lines}

Current sensor readings:
{sensor_lines}

Current timers and alarms:
{timer_context}

Rules:
- You are a multimodal AI assistant that can understand text, speech, and images. Always try to be helpful and answer any user question, even if it is not related to smart home or IoT control. When the user’s request involves controlling devices, respond as a friendly voice assistant and also output the appropriate JSON IoT commands.
- If the user asks about device states, answer using the above information.
- If the user asks a general question, respond with a normal short answer.
- If the user wants to control one or more devices:
  1. First, write a brief confirmation in natural language.
  2. For each device the user wants to change, output ONE JSON object per device on its own line:
     {{"type": "iot_command", "device": "<device_id>", "action": "on|off"}}
  3. IMPORTANT: Only output a JSON command for a device if its action would actually change its current state.
     If a device is already in the requested state, do NOT output a JSON command for it.
     Instead, mention in your natural language response that it is already on/off.
  4. If ALL requested devices are already in the desired state, output NO JSON at all.
  5. EXCEPT RULE (critical): If the user says "everything except X" or "all but X" or
     "turn off all except X", the devices named after "except" or "but" must be
     completely excluded — do NOT output any JSON command for them under any circumstances,
     even if they are currently on or off.
     - Never use power_strip_all if any individual socket is excluded, because
       power_strip_all affects every socket with a single command.
       Instead, emit individual commands for power_strip_socket1, power_strip_socket2,
       power_strip_socket3, and power_strip_usb as needed, skipping the excluded ones.
     - Example: "turn off everything except socket 1 and socket 3"
         WRONG: {{"type": "iot_command", "device": "power_strip_all", "action": "off"}}
         RIGHT: {{"type": "iot_command", "device": "power_strip_socket2", "action": "off"}}
                {{"type": "iot_command", "device": "power_strip_usb", "action": "off"}}
                (plus any other non-excluded devices that need changing)
     - Example: "turn on all lights except downlight2"
         WRONG: emit command for downlight2
         RIGHT: emit commands only for downlight1, downlight3, downlight4

The device_id must be one of:
power_strip_socket1, power_strip_socket2, power_strip_socket3,
power_strip_usb, power_strip_all, zigbee_plug, minid_relay, 
downlight1, downlight2, downlight3, downlight4.
- Use power_strip_all to turn the entire strip on or off at once.
- Use power_strip_socket1/2/3 for individual outlets.
- Use power_strip_usb for the USB hub.
- The zigbee plug should always be referred to as smart plug in natural language.
- minid_relay is a MOMENTARY relay (inching mode). It has no real on/off state. You should always refer to it in speech as just relay.
  ALWAYS emit the command for minid_relay regardless of its listed state.
  Use action "on" whether the user wants to turn the Pi on OR off — the relay pulses either way.
  Never skip minid_relay due to the "already in that state" rule.
- downlight1, downlight2, downlight3, downlight4 are RGB+CCT Zigbee downlights (10W).
  They support the following parameters in the optional "params" field:

  brightness: integer 0–254 (0 = off, 254 = full brightness)

  color_temp: integer 150–500 mired (color temperature, white light only)
    150 = cool/daylight, 350 = neutral, 500 = warm/candlelight

  color: CIE xy object for colored light {{"x": <float>, "y": <float>}}
    x controls hue axis: 0.0 = saturated color, ~0.33 = white/neutral
    Higher x shifts toward red, lower x toward blue/green.
    y controls the green-yellow axis.
    Common xy values:
      Red:    {{"x": 0.70, "y": 0.30}}
      Orange: {{"x": 0.60, "y": 0.38}}
      Yellow: {{"x": 0.50, "y": 0.45}}
      Green:  {{"x": 0.17, "y": 0.70}}
      Cyan:   {{"x": 0.15, "y": 0.35}}
      Blue:   {{"x": 0.14, "y": 0.08}}
      Purple: {{"x": 0.25, "y": 0.10}}
      Pink:   {{"x": 0.40, "y": 0.20}}
      White:  {{"x": 0.33, "y": 0.33}}
  For these, you MAY add an optional "params" field. Examples:
  {{"type": "iot_command", "device": "downlight1", "action": "on", "params": {{"brightness": 200, "color_temp": 350}}}}
  {{"type": "iot_command", "device": "downlight2", "action": "on", "params": {{"color": {{"x": 0.7, "y": 0.28}}}}}}

    Examples:
    {{"type": "iot_command", "device": "downlight1", "action": "on", "params": {{"brightness": 200, "color_temp": 350}}}}
    {{"type": "iot_command", "device": "downlight2", "action": "on", "params": {{"brightness": 180, "rgb": [255, 0, 0]}}}}
    {{"type": "iot_command", "device": "downlight3", "action": "on", "params": {{"brightness": 128, "color": {{"x": 0.14, "y": 0.08}}}}}}

- If the user says "dim", use brightness ~80. "Bright" = 254. "Warm" = color_temp ~450. "Cool/daylight" = ~200.
- If only turning on/off with no specific lighting preference, omit "params".
Do not add any extra fields to the JSON.
Do not wrap the JSON in markdown code fences like ```json.
Each JSON object must be valid and on its own line.

Example — if downlight1 is already off and user says "turn off all lights":
The light has been turned off. The downlight was already off.
{{"type": "iot_command", "device": "downlight2", "action": "off"}}
{{"type": "iot_command", "device": "downlight3", "action": "off"}}
{{"type": "iot_command", "device": "downlight4", "action": "off"}}

Example — "turn off all but usb hub" when lights are already off:
Both lights are already off, and the usb hub remains on as requested.

- MEMORY COMMANDS — use these JSON types when the user teaches you something or
  asks you to remember/forget. Output on its own line alongside or instead of iot_commands.

  To save or overwrite a macro:
  {{"type": "memory_upsert_macro", "name": "<macro name>", "description": "<plain English description>", "commands": [<list of iot_command objects without the type key>]}}

  To delete a macro:
  {{"type": "memory_delete_macro", "name": "<macro name>"}}

  To save a fact about the user or home:
  {{"type": "memory_upsert_fact", "key": "<short snake_case key>", "value": "<value>"}}

  To delete a fact (you MUST use the exact key shown in the "Remembered facts" section above):
  {{"type": "memory_delete_fact", "key": "<exact key from Remembered facts>"}}

  When a user says something like "remember that party mode means X", "from now on X means Y",
  or "forget X", output the appropriate memory JSON. Confirm in natural language what you saved.

  When a user triggers a saved macro by name (e.g. "party mode!"), expand it:
  look up the macro in the saved macros list above and emit the stored commands as
  individual iot_command JSON lines. Do NOT emit a memory command, just the device commands.

- TIMER COMMANDS — use these when the user wants to set, list, or cancel a timer or alarm.
  Output on its own line.

  To set a countdown timer (seconds, minutes, hours):
  {{"type": "timer_set", "duration_secs": <float>, "label": "<human label>"}}

  To set a clock alarm (specific time of day):
  {{"type": "alarm_set", "hour": <0-23>, "minute": <0-59>, "label": "<human label>"}}

  To cancel a timer by its short ID (first 8 chars shown in context):
  {{"type": "timer_cancel_id", "timer_id": "<full or short id>"}}

  To cancel timer(s) by label (fuzzy match):
  {{"type": "timer_cancel_label", "label": "<label or keyword>"}}

  Examples:
  User: "set a 10 minute timer"
  → {{"type": "timer_set", "duration_secs": 600, "label": "10 minute timer"}}

  User: "set an alarm for 7:30 AM"
  → {{"type": "alarm_set", "hour": 7, "minute": 30, "label": "Morning alarm"}}

  User: "cancel the pasta timer"
  → {{"type": "timer_cancel_label", "label": "pasta"}}

  User: "how long is left on my timer?"
  → No JSON needed; answer using the active timers context above.

  Always confirm in natural language what timer was set, including when it will go off.
  For timers: say "Your 10 minute timer is set."
  For alarms: say "Your alarm is set for 7:30 AM."

    To run an IoT command after a delay with NO beep (silent trigger):
  {{"type": "timer_then_iot", "duration_secs": float, "label": "human label",
   "iot_command": {{"device": "deviceid", "action": "on/off", "params": {{...optional...}}}}}}

  Use timer_then_iot instead of timer_set whenever the user asks to
  turn something on or off after a delay, e.g.:
  - "turn off the lights in 20 seconds"
  - "turn on the fan in 5 minutes"
  - "switch off the plug after 10 minutes"

  Example:
  User: "turn off all lights in 20 seconds"
  → {{"type": "timer_then_iot", "duration_secs": 20, "label": "turn off downlight1", "iot_command": {{"device": "downlight1", "action": "off"}}}}
  → {{"type": "timer_then_iot", "duration_secs": 20, "label": "turn off downlight2", "iot_command": {{"device": "downlight2", "action": "off"}}}}
  ... one line per device

  
  """

    user_message: dict = {"role": "user", "content": user_text}
    if image_b64_list:
        user_message["images"] = image_b64_list

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append(user_message)

    body = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": True,
        "keep_alive": -1,
        "options": {"temperature": 0.1},
    }

    if _current_timer:
        _current_timer.stamp("llm_start")

    resp = requests.post(OLLAMA_URL, json=body, stream=True, timeout=60)
    resp.raise_for_status()

    full_reply = ""
    _llm_first_token_stamped = False
    for line in resp.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        token = chunk.get("message", {}).get("content", "")
        if token and not _llm_first_token_stamped:
            if _current_timer:
                _current_timer.stamp("llm_first_token")
            _llm_first_token_stamped = True    

        full_reply += token
        if chunk.get("done"):
            break

    if _current_timer:
        _current_timer.stamp("llm_end")

    updated_history = (history or []) + [
        user_message,
        {"role": "assistant", "content": full_reply},
    ]
    return full_reply, updated_history

def handle_response(text: str, interaction: dict | None = None):
    clean_text = text.replace("```json", "").replace("```", "").strip()
    lines = clean_text.splitlines()

    parsed_cmds = []

    for line in lines:
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                cmd = json.loads(line)
                
                # Record every action regardless of type
                if interaction is not None:
                    auditor.add_action(interaction, cmd)

                if cmd.get("type") == "iot_command":
                    device = cmd.get("device")
                    action = cmd.get("action")
                    params = cmd.get("params", {})
                    if device and action in {"on", "off"}:
                        parsed_cmds.append(cmd)
                        # Build rich payload for z2m lights
                        if params:
                            rich_action = {"state": action.upper(), **params}
                            send_iot_command(device, rich_action, interaction)
                        else:
                            send_iot_command(device, action, interaction)

                elif cmd.get("type") == "memory_upsert_macro":
                    name = cmd.get("name", "").strip().lower()
                    description = cmd.get("description", "")
                    commands = cmd.get("commands", [])
                    if name and commands:
                        action = upsert_macro(name, description, commands)
                        print(f"[memory] macro '{name}' {action}")

                elif cmd.get("type") == "memory_delete_macro":
                    name = cmd.get("name", "").strip().lower()
                    if name:
                        deleted = delete_macro(name)
                        print(f"[memory] macro '{name}' {'deleted' if deleted else 'not found'}")

                elif cmd.get("type") == "memory_upsert_fact":
                    key = cmd.get("key", "").strip().lower()
                    value = cmd.get("value")
                    if key and value is not None:
                        action = upsert_fact(key, value)
                        print(f"[memory] fact '{key}' {action}")

                elif cmd.get("type") == "memory_delete_fact":
                    key = cmd.get("key", "").strip().lower()
                    if key:
                        deleted = delete_fact(key)
                        print(f"[memory] fact '{key}' {'deleted' if deleted else 'not found'}")

                elif cmd.get("type") == "timer_set":
                    duration = float(cmd.get("duration_secs", 0))
                    label = cmd.get("label", "Timer")
                    if duration > 0:
                        entry = timer_manager.set_timer(duration, label)
                        print(f"[timer] Set: '{label}' for {duration}s (id={entry.timer_id[:8]})")

                elif cmd.get("type") == "alarm_set":
                    hour = int(cmd.get("hour", 0))
                    minute = int(cmd.get("minute", 0))
                    label = cmd.get("label", "Alarm")
                    entry = timer_manager.set_alarm(hour, minute, label)
                    if entry:
                        print(f"[timer] Alarm: '{label}' at {hour:02d}:{minute:02d} (id={entry.timer_id[:8]})")

                elif cmd.get("type") == "timer_cancel_id":
                    tid = cmd.get("timer_id", "")
                    # Support short (8-char) prefix match
                    with timer_manager._lock:
                        full_id = next(
                            (k for k in timer_manager._timers if k.startswith(tid)), None
                        )
                    if full_id and timer_manager.cancel_timer(full_id):
                        print(f"[timer] Cancelled timer id={tid}")
                    else:
                        print(f"[timer] Timer not found: {tid}")

                elif cmd.get("type") == "timer_cancel_label":
                    label = cmd.get("label", "")
                    cancelled = timer_manager.cancel_by_label(label)
                    if cancelled:
                        print(f"[timer] Cancelled: {cancelled}")
                    else:
                        print(f"[timer] No timers matched label: {label}")

                elif cmd.get("type") == "timer_then_iot":
                    duration = float(cmd.get("duration_secs", 0))
                    label    = cmd.get("label", "Deferred command")
                    iot_cmd  = cmd.get("iot_command")

                    if duration > 0 and iot_cmd:
                        def make_callback(c):
                            def _fire():
                                send_iot_command(c["device"], c["action"], interaction=None)
                            return _fire

                        timer_manager.set_timer(duration, label, on_fire=make_callback(iot_cmd))
                        print(f"[timer] Deferred IoT: {iot_cmd} after {duration}s")

            except Exception as e:
                print(f"Failed to parse JSON command: {e}")

    # Audit: store all parsed commands
    if interaction is not None:
        auditor.add_parsed_command(interaction, parsed_cmds if parsed_cmds else None)
        actions = interaction.get("actions", [])
        if actions:
            # Build a readable summary from every recorded action
            summary_parts = [f'{a["label"]}: {a["detail"]}' for a in actions]
            auditor.set_explanation(
                interaction,
                f"Interpreted \"{interaction['stt_text']}\" as: {'; '.join(summary_parts)}."
            )
        else:
            auditor.set_explanation(
                interaction,
                f"Natural language response only. No commands executed."
            )

    # Speak only natural language lines (skip all JSON lines)
    natural = "\n".join(
        l for l in lines
        if not (l.strip().startswith("{") and l.strip().endswith("}"))
    ).strip()

    if natural:
        tts_say(natural)

    return {
        "parsed_cmds": parsed_cmds,
        "natural": natural,
    }

def process_text_command(
    text: str,
    image_b64_list: list[str] | None = None,
    extra_context: str | None = None,
    history: list[dict] | None = None,
    input_source: str | None = None,
) -> tuple[dict, list[dict]]:
    interaction = auditor.new_interaction(stt_text=text, input_source=input_source)
    response, updated_history = call_ollama(
        text,
        image_b64_list=image_b64_list,
        extra_context=extra_context,
        history=history,
    )
    auditor.add_llm_output(interaction, response)
    print(f"Raw LLM response:\n{response}")

    result = handle_response(response, interaction)
    parsed_cmds = result["parsed_cmds"]

    if not interaction.get("explanation"):
        auditor.set_explanation(
            interaction,
            f"Natural language response only. No commands executed."
        )

    auditor.emit(interaction)
    if _current_timer:
        _current_timer.utterance_text = text
        _current_timer.log()

    return result, updated_history

def main_loop():

    def _wake_listener_loop():
        while True:
            listen_for_wake_word()
            _tts_interrupt.set()

            # Interrupt TTS, worker stays alive, only stops mpv
            if _tts_manager is not None:
                _tts_manager.interrupt()
            else:
                tts_stop()  # fallback

            _wake_event.set()

            while _wake_event.is_set():
                time.sleep(0.05)
            time.sleep(0.5)

    # start the persistent listener
    threading.Thread(target=_wake_listener_loop, daemon=True).start()


    while True:
        _wake_event.wait()
        _wake_event.clear()
        _tts_interrupt.clear()

        acked = timer_manager.acknowledge_firing()
        for label in acked:
            print(f"[timer] Acknowledged via wake word: {label}")

        tts_say("Yes?")

        audio = record_once()
        if audio is None:
            continue
        text = stt_transcribe(audio)
        if not text:
            tts_say("Sorry, I didn't catch that.")
            continue

        print(f"User: {text}")
        if text.lower().strip() in ("quit", "quit.", "exit", "exit.", "stop", "stop."):
            tts_say("Goodbye.")
            continue

        result, conv_history = process_text_command(text, input_source="voice_wakeword")

        while True:
            if _wake_event.is_set():
                break  # wake word fired
            followup = listen_for_followup()
            if not followup:
                break  # timeout because no follow-up
            if followup.lower().strip() in ("quit", "quit.", "exit", "exit.", "stop", "stop."):
                tts_say("Goodbye.")
                break
            result, conv_history = process_text_command(followup, history=conv_history, input_source="voice_followup")


# Webui

app = Flask(__name__)

_CHAT_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Olivia Orchestrator</title>
  <style>
    body { font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 1rem; }
    #chat { border: 1px solid #ccc; padding: 0.5rem; height: 400px; overflow-y: auto; margin-bottom: 0.5rem; }
    .msg-user { text-align: right; color: #0b7285; margin: 0.25rem 0; }
    .msg-assistant { text-align: left; color: #343a40; margin: 0.25rem 0; }
    .msg-meta { font-size: 0.8rem; color: #868e96; }
    #input-row { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; }
    #prompt { flex: 1; }
    button { padding: 0.3rem 0.6rem; }
  </style>
</head>
<body>
  <h1>Olivia Orchestrator</h1>
  <div id="chat"></div>

  <form id="prompt-form">
    <div id="input-row">
      <textarea id="prompt" name="prompt" rows="2" placeholder="Type your prompt..."></textarea>
      <button type="submit">Send</button>
      <button type="button" id="mic-btn">Mic</button>
    </div>
    <div>
      <input type="file" id="file" name="file">
      <span class="msg-meta">Optional: attach an image or other file</span>
    </div>
  </form>
  <div id="tts-toggle" style="display:flex;align-items:center;gap:8px;margin-top:8px;">
  <span style="font-size:13px;color:#aaa;">Voice:</span>
  <button id="btn-kokoro" onclick="setEngine('kokoro')"
    style="padding:4px 12px;border-radius:12px;border:1px solid #555;cursor:pointer;font-size:13px;">
    Kokoro
  </button>
  <button id="btn-piper" onclick="setEngine('piper')"
    style="padding:4px 12px;border-radius:12px;border:1px solid #555;cursor:pointer;font-size:13px;">
    Piper
  </button>
</div>

<script>
    const chatEl = document.getElementById('chat');
    const formEl = document.getElementById('prompt-form');
    const promptEl = document.getElementById('prompt');
    const fileEl = document.getElementById('file');
    const micBtn = document.getElementById('mic-btn');

    // Holds the most recent pasted image (if any)
    let pastedImageBlob = null;

    function appendMessage(role, text) {
        const div = document.createElement('div');
        div.className = role === 'user' ? 'msg-user' : 'msg-assistant';
        div.textContent = (role === 'user' ? 'You: ' : 'Olivia: ') + text;
        chatEl.appendChild(div);
        chatEl.scrollTop = chatEl.scrollHeight;
    }

    // Enter = send, Shift+Enter = newline
    promptEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
        if (e.shiftKey) {
            // allow newline
            return;
        }
        e.preventDefault();
        formEl.requestSubmit();
        }
    });

    // Paste handler: capture image from clipboard while focus is in the textarea
    promptEl.addEventListener('paste', (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;

        for (const item of items) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
            const blob = item.getAsFile();
            if (blob) {
            pastedImageBlob = blob;
            appendMessage('user', '[pasted image]');
            // Optional: clear text, or leave as-is if you want text+image
            // promptEl.value = '';
            e.preventDefault(); // avoid weird text from the paste
            }
            break;
        }
        }
    });

    formEl.addEventListener('submit', async (e) => {
        e.preventDefault();
        const text = promptEl.value.trim();

        // Nothing to send
        if (!text && !fileEl.files.length && !pastedImageBlob) return;

        appendMessage('user', text || (pastedImageBlob ? '[image]' : '[file only]'));
        const formData = new FormData();
        formData.append('prompt', text);

        if (pastedImageBlob) {
        // Give it a reasonable default filename
        formData.append('file', pastedImageBlob, 'pasted-image.png');
        } else if (fileEl.files.length > 0) {
        formData.append('file', fileEl.files[0]);
        }

        // Reset inputs for next turn
        promptEl.value = '';
        fileEl.value = '';
        pastedImageBlob = null;

        const resp = await fetch('/prompt', {
        method: 'POST',
        body: formData
        });
        const data = await resp.json();
        appendMessage('assistant', data.assistant_text || '(no response)');
    });

    micBtn.addEventListener('click', async () => {
        micBtn.disabled = true;
        micBtn.textContent = 'Listening...';
        try {
        const resp = await fetch('/voice', { method: 'POST' });
        const data = await resp.json();
        if (data.user_text) {
            appendMessage('user', data.user_text);
        }
        if (data.assistant_text) {
            appendMessage('assistant', data.assistant_text);
        }
        } catch (e) {
        appendMessage('assistant', 'Voice error: ' + e.toString());
        } finally {
        micBtn.disabled = false;
        micBtn.textContent = 'Mic';
        }
    });

    function setEngine(engine) {
    fetch('/tts_engine', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({engine: engine})
    })
    .then(r => r.json())
    .then(d => updateEngineUI(d.engine));
    }

    function updateEngineUI(engine) {
    const kokoro = document.getElementById('btn-kokoro');
    const piper  = document.getElementById('btn-piper');

    const base =
        'padding:4px 12px;border-radius:12px;border:1px solid #555;' +
        'cursor:pointer;font-size:13px;margin-left:4px;';

    const active   = base + 'background:#4a9eff;color:#fff;border-color:#4a9eff;';
    const inactive = base + 'background:transparent;color:#ccc;border-color:#555;';

    kokoro.style.cssText = (engine === 'kokoro') ? active : inactive;
    piper.style.cssText  = (engine === 'piper')  ? active : inactive;
    }

    // Sync UI with current engine on page load
    fetch('/tts_engine').then(r => r.json()).then(d => updateEngineUI(d.engine));
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index_gui():
    return render_template_string(_CHAT_HTML)


@app.route("/prompt", methods=["POST"])
def prompt_route():

    tts_stop() # web ui text interrupts any ongoing speech

    # Dismiss any firing alarms when user types in the web UI
    acked = timer_manager.acknowledge_firing()
    for label in acked:
        print(f"[timer] Acknowledged via web UI: {label}")
    prompt_text = (request.form.get("prompt") or "").strip()
    file = request.files.get("file")

    image_b64_list: list[str] | None = None
    extra_context: str | None = None

    if file and file.filename:
        data = file.read()
        mimetype = file.mimetype or ""

        if mimetype.startswith("image/"):
            # Encode image to base64 for Ollama vision models
            img_b64 = base64.b64encode(data).decode("utf-8")
            image_b64_list = [img_b64]
        else:
            # For simple text files, include a short snippet
            try:
                text = data.decode("utf-8", errors="ignore")
                snippet = text[:4000]
                extra_context = f"User attached file '{file.filename}' (type {mimetype}). Contents (truncated):\n{snippet}"
            except Exception:
                extra_context = f"User attached a non-text file '{file.filename}' of type {mimetype}."

    if not prompt_text and not extra_context and not image_b64_list:
        return jsonify({"assistant_text": "Nothing to send."})

    result, _ = process_text_command(
        prompt_text or "[File only]",
        image_b64_list=image_b64_list,
        extra_context=extra_context,
        input_source="webui",
    )

    return jsonify({
        "assistant_text": result["natural"],
    })

@app.route("/voice", methods=["POST"])
def voice_route():
    tts_stop()
    audio = record_once()
    if audio is None:
        return jsonify({"assistant_text": "Audio error, please try again."})
    text = stt_transcribe(audio)
    if not text:
        return jsonify({"assistant_text": "Sorry, I didn't catch that."})
    print(f"[gui voice] User: {text}")
    result, _ = process_text_command(text, input_source="voice")
    return jsonify({
        "user_text": text,
        "assistant_text": result["natural"],
    })

@app.route("/tts_engine", methods=["GET"])
def get_tts_engine():
    return jsonify({"engine": TTS_ENGINE})

_tts_init_lock = threading.Lock()

@app.route("/tts_engine", methods=["POST"])
def set_tts_engine():
    global TTS_ENGINE, _tts_manager
    data = request.get_json(force=True)
    engine = data.get("engine", "").lower()
    if engine not in ("kokoro", "piper"):
        return jsonify({"error": "engine must be 'kokoro' or 'piper'"}), 400

    TTS_ENGINE = engine
    print(f"[tts] Engine switched to: {TTS_ENGINE}")

    if engine == "kokoro":
        if _tts_manager is None:
            def rewarm():
                global _tts_manager
                with _tts_init_lock:
                    if _tts_manager is not None:
                        _tts_manager.shutdown()
                    _tts_manager = TTSManager(voice="af_sarah")
            threading.Thread(target=rewarm, daemon=True).start()
    else:
        if _tts_manager is not None:
            _tts_manager.shutdown()
            _tts_manager = None

    return jsonify({"engine": TTS_ENGINE})

def run_gui():
    app.run(host="127.0.0.1", port=8001, debug=False, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=_audio_capture_loop, daemon=True).start()
    gui_thread = threading.Thread(target=run_gui, daemon=True)
    gui_thread.start()
    calibrate_mic()

    def _warmup():
        global _tts_manager
        if TTS_ENGINE == "kokoro":
            _tts_manager = TTSManager(voice="af_sarah")
        _kokoro_ready.set()

    threading.Thread(target=_warmup, daemon=True).start()

    print("[startup] Waiting for TTS engine...")
    _kokoro_ready.wait()
    print("[startup] TTS ready, entering main loop.")

    main_loop()