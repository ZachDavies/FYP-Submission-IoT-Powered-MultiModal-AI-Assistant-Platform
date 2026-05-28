import json
import uuid
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

MQTT_AUDIT_TOPIC = "audit/actions"

ACTION_TYPE_LABELS = {
    "iot_command":          "IoT Command",
    "timer_set":            "Timer Set",
    "alarm_set":            "Alarm Set",
    "timer_cancel_id":      "Timer Cancelled",
    "timer_cancel_label":   "Timer Cancelled",
    "memory_upsert_macro":  "Macro Saved",
    "memory_delete_macro":  "Macro Deleted",
    "memory_upsert_fact":   "Fact Saved",
    "memory_delete_fact":   "Fact Deleted",
    "timer_then_iot": "Deferred IoT Command",
}


class Auditor:
    def __init__(self, mqtt_client: mqtt.Client, jsonl_path: str = "audit_log.jsonl"):
        self.mqtt_client = mqtt_client
        self.jsonl_path = jsonl_path

    def new_interaction(self, stt_text: str, input_source: str | None = None) -> dict:
        interaction = {
            "interaction_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stt_text": stt_text,
            "input_source": input_source,
            "llm_output": None,
            "parsed_command": None,
            "actions": [],
            "mqtt_actions": [],
            "device_updates": [],
            "explanation": None,
        }
        return interaction

    def add_llm_output(self, interaction: dict, llm_output: str):
        interaction["llm_output"] = llm_output

    def add_parsed_command(self, interaction: dict, cmd: dict | None):
        interaction["parsed_command"] = cmd

    def add_action(self, interaction: dict, cmd: dict):
        action_type = cmd.get("type", "unknown")
        label = ACTION_TYPE_LABELS.get(action_type, action_type)

        if action_type == "iot_command":
            device = cmd.get("device", "?")
            action = cmd.get("action", "?")
            params = cmd.get("params")
            detail = f'{device} → {action}'
            if params:
                detail += f' ({json.dumps(params, separators=(",", ":"))})'

        elif action_type == "timer_set":
            secs = cmd.get("duration_secs", 0)
            mins, s = divmod(int(secs), 60)
            hrs, mins = divmod(mins, 60)
            dur = (f"{hrs}h " if hrs else "") + (f"{mins}m " if mins else "") + (f"{s}s" if s or not mins else "")
            detail = f'"{cmd.get("label", "Timer")}" for {dur.strip()}'

        elif action_type == "alarm_set":
            h = cmd.get("hour", 0)
            m = cmd.get("minute", 0)
            detail = f'"{cmd.get("label", "Alarm")}" at {h:02d}:{m:02d}'

        elif action_type in ("timer_cancel_id", "timer_cancel_label"):
            key = "timer_id" if action_type == "timer_cancel_id" else "label"
            detail = f'"{cmd.get(key, "?")}"'

        elif action_type == "memory_upsert_macro":
            detail = f'"{cmd.get("name", "?")}"'

        elif action_type == "memory_delete_macro":
            detail = f'"{cmd.get("name", "?")}"'

        elif action_type == "memory_upsert_fact":
            detail = f'{cmd.get("key", "?")} = {cmd.get("value", "?")!r}'

        elif action_type == "memory_delete_fact":
            detail = f'key "{cmd.get("key", "?")}"'

        else:
            detail = json.dumps({k: v for k, v in cmd.items() if k != "type"}, separators=(",", ":"))

        interaction["actions"].append({
            "type":      action_type,
            "label":     label,
            "detail":    detail,
            "raw":       cmd,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def add_mqtt_action(self, interaction: dict, topic: str, payload: str):
        interaction["mqtt_actions"].append({
            "topic":     topic,
            "payload":   payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def add_device_update(self, interaction: dict, device_id: str, state: dict):
        interaction["device_updates"].append({
            "device_id": device_id,
            "state":     state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def set_explanation(self, interaction: dict, explanation: str):
        interaction["explanation"] = explanation

    def emit(self, interaction: dict):
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(interaction, ensure_ascii=False) + "\n")
        self.mqtt_client.publish(MQTT_AUDIT_TOPIC, json.dumps(interaction), qos=1)
