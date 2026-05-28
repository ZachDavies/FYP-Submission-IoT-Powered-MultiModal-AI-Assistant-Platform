import json
import threading
import queue
from flask import Flask, render_template, Response
import os
import paho.mqtt.client as mqtt
import signal
import sys
from sys import path as syspath
syspath.insert(0, "../orchestrator")
from config import DEVICES, MQTT_USER, MQTT_PASS, SENSOR_DATA

SENSOR_CACHE_PATH = os.path.join(os.path.dirname(__file__), "../orchestrator/sensor_state_cache.json")
DEVICE_CACHE_PATH = os.path.join(os.path.dirname(__file__), "../orchestrator/device_state_cache.json")

REAL_DEVICES    = {k: v for k, v in DEVICES.items() if (v.get("tasmota") or v.get("z2m") or v.get("ha_relay")) and v.get("state_topic")}

app = Flask(__name__)

ACTIONS = []
MAX_ACTIONS = 50

MQTT_HOST = "192.168.1.106"
MQTT_PORT = 1883

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.username_pw_set(MQTT_USER, MQTT_PASS)

# SSE subscriber queues — one per connected browser tab
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()

def load_sensor_cache():
    if not os.path.exists(SENSOR_CACHE_PATH):
        return
    try:
        with open(SENSOR_CACHE_PATH) as f:
            cache = json.load(f)
        for sid, vals in cache.items():
            if sid in SENSOR_DATA:
                SENSOR_DATA[sid].update(vals)
        print(f"[dashboard] Loaded sensor cache: {cache}")
    except Exception as e:
        print(f"[dashboard] Could not load sensor cache: {e}")

def load_device_cache():
    if not os.path.exists(DEVICE_CACHE_PATH):
        return
    try:
        with open(DEVICE_CACHE_PATH) as f:
            cache = json.load(f)
        for device_id, state in cache.items():
            if device_id in DEVICES:
                if isinstance(state, dict):
                    for field in ("power", "brightness", "color_temp", "color_xy"):
                        if field in state:
                            DEVICES[device_id][field] = state[field]
                else:
                    DEVICES[device_id]["power"] = state
        print(f"[dashboard] Loaded device cache: {cache}")
    except Exception as e:
        print(f"[dashboard] Could not load device cache: {e}")

def push_event(event_type: str, data: dict):
    """Broadcast an SSE event to all connected clients."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _subscribers_lock:
        for q in _subscribers:
            q.put(msg)

def on_connect(client, userdata, flags, reason_code, properties):
    for dev in DEVICES.values():
        if dev.get("state_topic"):
            client.subscribe(dev["state_topic"])
    for sensor in SENSOR_DATA.values():
        client.subscribe(sensor["state_topic"])
    client.subscribe("audit/actions")
    
def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8").strip()

    for key, sensor in SENSOR_DATA.items():
        if sensor["state_topic"] == msg.topic:
            try:
                data = json.loads(payload)
                sensor.update({k: data[k] for k in data if k in sensor})
                push_event("sensor", {"id": key, **sensor})
            except json.JSONDecodeError:
                pass
            return

    if msg.topic == "audit/actions":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        ACTIONS.append(data)
        if len(ACTIONS) > MAX_ACTIONS:
            del ACTIONS[0]
        push_event("audit", data)
        return

    for key, dev in DEVICES.items():
        if dev.get("state_topic") == msg.topic:
            if dev.get("tasmota"):
                dev["power"] = "on" if payload.upper() == "ON" else "off"
            elif dev.get("z2m"):
                try:
                    data = json.loads(payload)
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
                    return
            elif dev.get("ha_relay"):
                dev["power"] = "on" if payload.lower() == "on" else "off"
            else:
                try:
                    data = json.loads(payload)
                    dev["power"] = data.get("power", dev["power"])
                except json.JSONDecodeError:
                    return
            push_event("device", {"id": key, "power": dev["power"]})
            break

client.on_connect = on_connect
client.on_message = on_message

def mqtt_thread():
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_forever()

@app.route("/")
def index():
    return render_template(
        "index.html",
        real_devices=REAL_DEVICES,
        sensor_data=SENSOR_DATA,
        actions=ACTIONS,
    )

@app.route("/stream")
def stream():
    """SSE endpoint — each browser tab gets its own queue."""
    q = queue.Queue()
    with _subscribers_lock:
        _subscribers.append(q)

    def event_stream():
        try:
            while True:
                msg = q.get()  # blocks until data arrives
                yield msg
        except GeneratorExit:
            with _subscribers_lock:
                _subscribers.remove(q)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _shutdown(sig, frame):
    print("\n[dashboard] Shutting down...")
    client.disconnect()
    sys.exit(0)

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

if __name__ == "__main__":
    load_device_cache()
    load_sensor_cache() 
    t = threading.Thread(target=mqtt_thread, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False) # use_reloader=False is important — reloader forks the process and breaks the MQTT thread