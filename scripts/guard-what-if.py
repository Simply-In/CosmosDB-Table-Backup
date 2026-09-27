#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

BLOCKED_CHANGE_TYPES = {"Delete", "Unsupported"}
PUBLIC_PATH_FRAGMENTS = (
    "publicnetworkaccess",
    "allowblobpublicaccess",
    "allowsharedkeyaccess",
    "defaultaction",
)


def normalized(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).lower()


def public_access_relaxed(path: str, before: object, after: object) -> bool:
    path = path.lower()
    if not any(fragment in path for fragment in PUBLIC_PATH_FRAGMENTS):
        return False
    after_value = normalized(after)
    if "defaultaction" in path:
        return after_value == "allow" and normalized(before) != "allow"
    if "allow" in path:
        return after_value == "true" and normalized(before) != "true"
    return after_value == "enabled" and normalized(before) != "enabled"


def load_changes(path: Path) -> list[dict[str, object]]:
    payload = path.read_text(encoding="utf-8-sig").strip().lstrip("\ufeff").strip()
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError as error:
        code_points = ", ".join(f"U+{ord(char):04X}" for char in payload[:16])
        raise SystemExit(
            "What-if output is not JSON "
            f"({len(payload)} characters; initial code points: {code_points or '<empty>'}): {error}"
        ) from error
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("changes", data.get("properties", {}).get("changes", []))
    raise SystemExit("What-if JSON must be an object or array")


def delta_failures(resource_id: str, delta: dict[str, object]) -> list[str]:
    path = str(delta.get("path", ""))
    before = delta.get("before")
    after = delta.get("after")
    failures = []
    if public_access_relaxed(path, before, after):
        failures.append(f"Public access relaxation at {resource_id}: {path}")
    if "securityrules" in path.lower() and normalized(after) in {"allow", "*", "internet"}:
        failures.append(f"Potential NSG relaxation at {resource_id}: {path}")
    return failures


def change_failures(change: dict[str, object], allow_rbac: bool) -> list[str]:
    resource_id = str(change.get("resourceId", "<unknown>"))
    change_type = str(change.get("changeType", "Unknown"))
    resource_type = resource_id.lower()
    is_role_assignment = "roleassignments" in resource_type
    reviewed_unsupported_rbac = change_type == "Unsupported" and is_role_assignment and allow_rbac
    failures = []
    if change_type in BLOCKED_CHANGE_TYPES and not reviewed_unsupported_rbac:
        failures.append(f"{change_type}: {resource_id}")
    if change_type == "Create" and "/virtualnetworkpeerings/" in resource_type:
        failures.append(f"VNet peering creation: {resource_id}")
    if change_type in {"Create", "Modify"} and is_role_assignment and not allow_rbac:
        failures.append(f"RBAC change requires explicit review: {resource_id}")
    for delta in change.get("delta") or []:
        failures.extend(delta_failures(resource_id, delta))
    return failures


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: {Path(sys.argv[0]).name} WHAT_IF_JSON")
    changes = load_changes(Path(sys.argv[1]))
    allow_rbac = os.getenv("ALLOW_ROLE_ASSIGNMENT_CHANGES") == "1"
    failures = [failure for change in changes for failure in change_failures(change, allow_rbac)]
    for failure in failures:
        print(f"BLOCKED: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"What-if guard passed for {len(changes)} resource changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
