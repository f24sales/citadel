"""Add-only discovery: retain existing service records, even while offline."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "providers"))
from atomic_io import atomic_write_json


def previous_services(path: Path) -> dict:
    if not path.exists():
        return {"http_services": [], "other_ports": []}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or any(not isinstance(value.get(key), list)
                                          for key in ("http_services", "other_ports")):
        raise ValueError("Invalid existing services.json; Add aborted")
    for row in value["http_services"] + value["other_ports"]:
        if not isinstance(row, dict) or not isinstance(row.get("port"), int):
            raise ValueError("Invalid existing service; Add aborted")
    pending = value.get("pending_add_ports", [])
    if not isinstance(pending, list) or any(type(port) is not int for port in pending):
        raise ValueError("Invalid pending Add ports; Add aborted")
    return value


def merge_services(path: Path, new: dict) -> dict:
    old = previous_services(path)
    known = {row["port"] for key in ("http_services", "other_ports") for row in old[key]}
    added = sorted(set(old.get("pending_add_ports", [])) | {
        row["port"] for row in new["http_services"] if row["port"] not in known})
    result = {**old, "generated_at": new["generated_at"],
              "https_only": old.get("https_only", new["https_only"]),
              "added_ports": added, "pending_add_ports": added}
    for key in ("http_services", "other_ports"):
        result[key] = old[key] + [row for row in new[key] if row["port"] not in known]
        result[key].sort(key=lambda row: row["port"])
    return result


if __name__ == "__main__":
    listeners, services = map(Path, sys.argv[1:])
    old = previous_services(services)
    known = {row["port"] for key in ("http_services", "other_ports") for row in old[key]}
    rows = json.loads(listeners.read_text())
    atomic_write_json(listeners, [row for row in rows if row["port"] not in known])
    print(len(old.get("pending_add_ports", [])))
