import os
from typing import Dict

# Audio settings
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
BLOCK_DURATION = float(os.getenv("BLOCK_DURATION", "5.0"))

# Ollama
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL_NAME = os.getenv("MODEL_NAME", "gemma4:e4b")

# MQTT
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.106")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

MQTT_USER = os.getenv("MQTT_USER", "DVES_USER")
MQTT_PASS = os.getenv("MQTT_PASS", "12345") # CHANGE THIS TO YOUR ACTUAL PASSWORD

# Device mapping LLM device_id to MQTT topic
DEVICE_TOPIC_MAP: Dict[str, str] = {
    "power_strip_socket1": "cmnd/tasmota_639D72/POWER1",
    "power_strip_socket2": "cmnd/tasmota_639D72/POWER2",
    "power_strip_socket3": "cmnd/tasmota_639D72/POWER3",
    "power_strip_usb":     "cmnd/tasmota_639D72/POWER4",
    "power_strip_all":     "cmnd/tasmota_639D72/POWER0",
    "zigbee_plug": "zigbee2mqtt/Plug1/set",
    "minid_relay": "home/minid/set",
    "downlight1": "zigbee2mqtt/DownLight1/set",
    "downlight2": "zigbee2mqtt/DownLight2/set",
    "downlight3": "zigbee2mqtt/DownLight3/set",
    "downlight4": "zigbee2mqtt/DownLight4/set",
}

AUDIT_JSONL_PATH = os.getenv("AUDIT_JSONL_PATH", "audit_log.jsonl")

MQTT_AUDIT_TOPIC = "audit/actions"

KNOWN_DEVICES = list(DEVICE_TOPIC_MAP.keys())

SENSOR_DATA: dict = {
    "motion_sensor": {
        "name": "Motion Sensor",
        "state_topic": "zigbee2mqtt/Motion1",
        "occupancy": False,
        "battery": None,
        "linkquality": None,
    },
    "temp_sensor": {
        "name": "Temp/Humidity Sensor",
        "state_topic": "zigbee2mqtt/TempSensor",
        "temperature": None,
        "humidity": None,
        "battery": None,
    },
}

DEVICES: dict = {
    "power_strip_socket1": {
        "name": "Power Strip Socket 1",
        "state_topic": "stat/tasmota_639D72/POWER1",
        "power": "off",
        "tasmota": True,
    },
    "power_strip_socket2": {
        "name": "Power Strip Socket 2",
        "state_topic": "stat/tasmota_639D72/POWER2",
        "power": "off",
        "tasmota": True,
    },
    "power_strip_socket3": {
        "name": "Power Strip Socket 3",
        "state_topic": "stat/tasmota_639D72/POWER3",
        "power": "off",
        "tasmota": True,
    },
    "power_strip_usb": {
        "name": "Power Strip USB Hub",
        "state_topic": "stat/tasmota_639D72/POWER4",
        "power": "off",
        "tasmota": True,
    },
    "power_strip_all": {
        "name": "Power Strip (All)",
        "state_topic": "stat/tasmota_639D72/POWER0",
        "power": "unknown",
        "tasmota": True,
    },
    "zigbee_plug": {
        "name": "Smart Plug",
        "state_topic": "zigbee2mqtt/Plug1",
        "power": "off",
        "z2m": True,
    },
    "minid_relay": {
        "name": "Relay",
        "state_topic": "homeassistant/switch/sonoff_10028841e2_1/state",
        "power": "off",
        "ha_relay": True, 
    },
    "downlight1": {
    "name": "Downlight 1",
    "state_topic": "zigbee2mqtt/DownLight1",
    "power": "off",
    "z2m": True,
    },
    "downlight2": {
        "name": "Downlight 2",
        "state_topic": "zigbee2mqtt/DownLight2",
        "power": "off",
        "z2m": True,
    },
    "downlight3": {
        "name": "Downlight 3",
        "state_topic": "zigbee2mqtt/DownLight3",
        "power": "off",
        "z2m": True,
    },
    "downlight4": {
        "name": "Downlight 4",
        "state_topic": "zigbee2mqtt/DownLight4",
        "power": "off",
        "z2m": True,
    },
}
