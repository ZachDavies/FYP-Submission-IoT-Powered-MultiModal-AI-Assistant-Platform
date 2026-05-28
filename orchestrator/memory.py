import json
import os
from datetime import datetime, timezone
from typing import Optional

MEMORY_PATH = os.path.join(os.path.dirname(__file__), "memory.json")

def _load() -> dict:
    if not os.path.exists(MEMORY_PATH):
        return {"macros": {}, "facts": {}}
    try:
        with open(MEMORY_PATH) as f:
            data = json.load(f)
        data.setdefault("macros", {})
        data.setdefault("facts", {})
        return data
    except Exception as e:
        print(f"[memory] Load error: {e}")
        return {"macros": {}, "facts": {}}

def _save(data: dict):
    try:
        with open(MEMORY_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[memory] Save error: {e}")

def upsert_macro(name: str, description: str, commands: list) -> str: # update or insert into macros
    data = _load()
    now = datetime.now(timezone.utc).isoformat()
    existing = data["macros"].get(name)
    data["macros"][name] = {
        "description": description,
        "commands": commands,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    _save(data)
    action = "updated" if existing else "saved"
    print(f"[memory] Macro {action}: \"{name}\"")
    return action

def delete_macro(name: str) -> bool:
    data = _load()
    if name in data["macros"]:
        del data["macros"][name]
        _save(data)
        print(f"[memory] Macro deleted: \"{name}\"")
        return True
    return False

def get_macro(name: str) -> Optional[dict]:
    return _load()["macros"].get(name)

def list_macros() -> dict:
    return _load()["macros"]

def upsert_fact(key: str, value) -> str:
    data = _load()
    now = datetime.now(timezone.utc).isoformat()
    existing = data["facts"].get(key)
    data["facts"][key] = {"value": value, "updated_at": now}
    _save(data)
    action = "updated" if existing else "saved"
    print(f"[memory] Fact {action}: {key} = {value!r}")
    return action

def get_fact(key: str):
    return _load()["facts"].get(key, {}).get("value")

def delete_fact(key: str) -> bool:
    data = _load()
    if key in data["facts"]:
        del data["facts"][key]
        _save(data)
        return True
    return False

def build_memory_context() -> str:
    data = _load()
    lines = []

    if data["macros"]:
        lines.append("Saved macros (custom voice commands you have learned):")
        for name, macro in data["macros"].items():
            cmds_str = json.dumps(macro["commands"], separators=(",", ":"))
            lines.append(f'  - "{name}": {macro["description"]} → {cmds_str}')
    else:
        lines.append("Saved macros: none yet.")

    if data["facts"]:
        lines.append("Remembered facts about the user/home:")
        for key, entry in data["facts"].items():
            lines.append(f'  - {key}: {entry["value"]!r}')

    return "\n".join(lines)