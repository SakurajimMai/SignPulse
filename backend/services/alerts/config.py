from __future__ import annotations

import threading
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeVar

from filelock import FileLock

from backend.core.config import get_settings
from backend.utils.atomic_io import read_json_safe, write_json_atomic

from .rules import ALERT_RULES, LEGACY_RULE_MIGRATIONS, RULE_IDS

SCHEMA_VERSION = 2
RECENT_LIMIT = 40
STATE_KEY_LIMIT = 512
_state_lock = threading.RLock()
_T = TypeVar("_T")


def _alerts_file() -> Path:
    return Path(get_settings().resolve_base_dir()) / ".alerts_config.json"


def _alerts_lock() -> FileLock:
    path = _alerts_file().with_suffix(".json.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=10)


def default_rules() -> dict[str, dict[str, Any]]:
    return {
        item["id"]: {
            "enabled": bool(item["default_enabled"]),
            "email_enabled": bool(item["default_email_enabled"]),
            "telegram_enabled": bool(item["default_telegram_enabled"]),
            "cooldown_minutes": int(item["default_cooldown_minutes"]),
        }
        for item in ALERT_RULES
    }


def _normalize_rules(raw: Any) -> dict[str, dict[str, Any]]:
    merged = default_rules()
    if not isinstance(raw, dict):
        return merged
    for rule_id, item in raw.items():
        if rule_id not in RULE_IDS or not isinstance(item, dict):
            continue
        cooldown = item.get("cooldown_minutes", merged[rule_id]["cooldown_minutes"])
        try:
            cooldown_n = int(cooldown)
        except (TypeError, ValueError):
            cooldown_n = merged[rule_id]["cooldown_minutes"]
        merged[rule_id] = {
            "enabled": bool(item.get("enabled", merged[rule_id]["enabled"])),
            "email_enabled": bool(
                item.get("email_enabled", merged[rule_id]["email_enabled"])
            ),
            "telegram_enabled": bool(
                item.get("telegram_enabled", merged[rule_id]["telegram_enabled"])
            ),
            "cooldown_minutes": max(1, min(cooldown_n, 24 * 60)),
        }
    return merged


def _migrated_rule_ids(rule_id: str) -> tuple[str, ...]:
    if rule_id in RULE_IDS:
        return (rule_id,)
    return LEGACY_RULE_MIGRATIONS.get(rule_id, ())


def _migrate_delivery_map(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    migrated = deepcopy(raw)
    for key, value in raw.items():
        text = str(key)
        parts = text.split(":")
        if len(parts) != 2:
            continue
        old_rule_id, digest = parts
        targets = _migrated_rule_ids(old_rule_id)
        for target in targets:
            # Schema v1 only delivered SMTP mail, so its channel-less state must
            # not suppress the first Telegram delivery after upgrading.
            migrated.setdefault(f"{target}:{digest}:email", deepcopy(value))
        migrated.pop(text, None)
    return migrated


def migrate_alerts_state(raw: Any) -> dict[str, Any]:
    """Upgrade legacy aggregate rules and per-event delivery state once."""
    if not isinstance(raw, dict):
        raw = {}
    migrated = deepcopy(raw)
    try:
        version = int(migrated.get("schema_version") or 1)
    except (TypeError, ValueError):
        version = 1
    if version >= SCHEMA_VERSION:
        return migrated

    source_rules = raw.get("rules") if isinstance(raw.get("rules"), dict) else {}
    rules = dict(source_rules)
    for legacy_id, target_ids in LEGACY_RULE_MIGRATIONS.items():
        legacy = source_rules.get(legacy_id)
        if not isinstance(legacy, dict):
            continue
        inherited = {
            key: deepcopy(legacy[key])
            for key in (
                "enabled",
                "email_enabled",
                "telegram_enabled",
                "cooldown_minutes",
            )
            if key in legacy
        }
        for target_id in target_ids:
            rules.setdefault(target_id, deepcopy(inherited))
    migrated["rules"] = rules
    for field in ("last_sent", "last_attempt", "in_flight"):
        migrated[field] = _migrate_delivery_map(raw.get(field))
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


def _bounded_timestamps(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    items = [
        (str(key), str(value))
        for key, value in raw.items()
        if str(key).strip() and str(value or "").strip()
    ]
    items.sort(key=lambda item: item[1], reverse=True)
    return dict(items[:STATE_KEY_LIMIT])


def _bounded_in_flight(raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    items: list[tuple[str, dict[str, str]]] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        token = str(value.get("token") or "").strip()
        at = str(value.get("at") or "").strip()
        if token and at:
            items.append((str(key), {"token": token, "at": at}))
    items.sort(key=lambda item: item[1]["at"], reverse=True)
    return dict(items[:STATE_KEY_LIMIT])


def _normalize_state(raw: Any) -> dict[str, Any]:
    raw = migrate_alerts_state(raw)
    recent = raw.get("recent") if isinstance(raw.get("recent"), list) else []
    return {
        "schema_version": SCHEMA_VERSION,
        "rules": _normalize_rules(raw.get("rules")),
        "last_sent": _bounded_timestamps(raw.get("last_sent")),
        "last_attempt": _bounded_timestamps(raw.get("last_attempt")),
        "in_flight": _bounded_in_flight(raw.get("in_flight")),
        "recent": recent[-RECENT_LIMIT:],
    }


def _load_alerts_state_unlocked() -> dict[str, Any]:
    raw = read_json_safe(_alerts_file(), {})
    state = _normalize_state(raw)
    try:
        version = int(raw.get("schema_version") or 1) if isinstance(raw, dict) else 1
    except (TypeError, ValueError):
        version = 1
    if version < SCHEMA_VERSION:
        write_json_atomic(_alerts_file(), state)
    return state


def load_alerts_state() -> dict[str, Any]:
    with _state_lock:
        with _alerts_lock():
            return _load_alerts_state_unlocked()


def _save_alerts_state_unlocked(state: dict[str, Any]) -> None:
    write_json_atomic(_alerts_file(), _normalize_state(state))


def save_alerts_state(state: dict[str, Any]) -> None:
    with _state_lock:
        with _alerts_lock():
            _save_alerts_state_unlocked(state)


def mutate_alerts_state(mutator: Callable[[dict[str, Any]], _T]) -> _T:
    """Atomically apply one cross-process read/modify/write state transaction."""
    with _state_lock:
        with _alerts_lock():
            state = _load_alerts_state_unlocked()
            result = mutator(state)
            _save_alerts_state_unlocked(state)
            return result


def _as_rules_map(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        mapped: dict[str, Any] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("id"):
                mapped[str(item["id"])] = item
        return mapped
    if isinstance(raw, dict):
        return raw
    return {}


def save_alert_rules(updates: dict[str, Any] | list[Any]) -> dict[str, Any]:
    incoming: Any = updates
    if isinstance(updates, dict) and "rules" in updates:
        incoming = updates.get("rules")
    incoming = _as_rules_map(incoming)

    expanded: dict[str, Any] = {}
    for legacy_id, target_ids in LEGACY_RULE_MIGRATIONS.items():
        item = incoming.get(legacy_id)
        if not isinstance(item, dict):
            continue
        for target_id in target_ids:
            expanded.setdefault(target_id, item)
    for rule_id, item in incoming.items():
        if rule_id in RULE_IDS:
            expanded[rule_id] = item

    def _save(state: dict[str, Any]) -> dict[str, Any]:
        current = state["rules"]
        for rule_id, item in expanded.items():
            if rule_id not in RULE_IDS or not isinstance(item, dict):
                continue
            current[rule_id] = {
                **current[rule_id],
                **{
                    key: item[key]
                    for key in (
                        "enabled",
                        "email_enabled",
                        "telegram_enabled",
                        "cooldown_minutes",
                    )
                    if key in item
                },
            }
        state["rules"] = _normalize_rules(current)
        return public_alerts(state)

    return mutate_alerts_state(_save)


def public_alerts(state: dict[str, Any] | None = None) -> dict[str, Any]:
    current = state if state is not None else load_alerts_state()
    rules = current["rules"]
    items = []
    for spec in ALERT_RULES:
        stored = rules.get(spec["id"]) or {}
        items.append(
            {
                "id": spec["id"],
                "group": spec["group"],
                "title": spec.get("title") or spec["id"],
                "severity": spec["severity"],
                "enabled": bool(stored.get("enabled", spec["default_enabled"])),
                "email_enabled": bool(
                    stored.get("email_enabled", spec["default_email_enabled"])
                ),
                "telegram_enabled": bool(
                    stored.get(
                        "telegram_enabled", spec["default_telegram_enabled"]
                    )
                ),
                "cooldown_minutes": int(
                    stored.get("cooldown_minutes", spec["default_cooldown_minutes"])
                ),
            }
        )
    return {
        "rules": items,
        "recent": list(current.get("recent") or [])[-RECENT_LIMIT:],
    }
