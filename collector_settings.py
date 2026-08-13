"""Developer-managed, selectable WeChat collector profiles for the local MVP."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from domain import now_iso
from wechat_sources import DEFAULT_ACCOUNTS, LEGACY_DEFAULT_ACCOUNTS, MAX_ACCOUNTS


ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = Path(os.getenv("PRACTICE_XIAODA_COLLECTOR_SETTINGS", ROOT / "data" / "collector_settings.json"))
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PROFILE_ID_RE = re.compile(r"[^a-z0-9_-]+")


def _profile_defaults(profile_id: str = "default", name: str = "默认采集配置") -> dict[str, Any]:
    return {
        "id": profile_id,
        "name": name,
        "accounts": list(DEFAULT_ACCOUNTS),
        "daily_time": "08:15",
        "enabled": False,
        "token": "",
        "cookie": "",
        "collector_path": os.getenv("WECHAT_COLLECTOR_PATH", ""),
        "collector_python": os.getenv("WECHAT_COLLECTOR_PYTHON", ""),
        "last_run_at": "",
        "last_result": "",
        "last_scheduled_date": "",
        "updated_at": "",
    }


def _default_store() -> dict[str, Any]:
    return {"active_profile_id": "default", "profiles": [_profile_defaults()]}


def _normalize_profile(raw: Any, *, fallback_id: str = "default") -> dict[str, Any]:
    result = _profile_defaults(fallback_id)
    if isinstance(raw, dict):
        result.update({key: value for key, value in raw.items() if key in result})
    result["id"] = _profile_id(str(result.get("id") or fallback_id), fallback_id)
    result["name"] = str(result.get("name") or "默认采集配置").strip()[:60] or "默认采集配置"
    result["accounts"] = _clean_accounts(result.get("accounts"))
    result["daily_time"] = result["daily_time"] if TIME_PATTERN.fullmatch(str(result.get("daily_time"))) else "08:15"
    return result


def _profile_id(value: str, fallback: str = "profile") -> str:
    normalized = PROFILE_ID_RE.sub("-", value.strip().lower()).strip("-")
    return normalized[:40] or fallback


def load_store() -> dict[str, Any]:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_store()
    if not isinstance(raw, dict):
        return _default_store()
    # Migrate the original single-profile file transparently.
    if not isinstance(raw.get("profiles"), list):
        profile = _normalize_profile(raw)
        return {"active_profile_id": profile["id"], "profiles": [profile]}
    profiles = []
    seen: set[str] = set()
    for index, item in enumerate(raw["profiles"]):
        profile = _normalize_profile(item, fallback_id=f"profile-{index + 1}")
        if profile["id"] in seen:
            profile["id"] = f"{profile['id']}-{index + 1}"
        seen.add(profile["id"])
        profiles.append(profile)
    if not profiles:
        return _default_store()
    active = str(raw.get("active_profile_id") or profiles[0]["id"])
    if active not in {profile["id"] for profile in profiles}:
        active = profiles[0]["id"]
    return {"active_profile_id": active, "profiles": profiles}


def _active(store: dict[str, Any]) -> dict[str, Any]:
    active_id = store["active_profile_id"]
    return next(profile for profile in store["profiles"] if profile["id"] == active_id)


def load_private() -> dict[str, Any]:
    return _active(load_store())


def _clean_accounts(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    if not isinstance(value, list):
        return list(DEFAULT_ACCOUNTS)
    accounts = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if tuple(accounts) == LEGACY_DEFAULT_ACCOUNTS:
        return list(DEFAULT_ACCOUNTS)
    return accounts[:MAX_ACCOUNTS] or list(DEFAULT_ACCOUNTS)


def save_from_developer(payload: dict[str, Any]) -> dict[str, Any]:
    store = load_store()
    requested_id = str(payload.get("profile_id") or store["active_profile_id"])
    create_new = requested_id == "__new__"
    if create_new:
        name = str(payload.get("profile_name") or "新建采集配置").strip()[:60] or "新建采集配置"
        base_id = _profile_id(name, "profile")
        ids = {profile["id"] for profile in store["profiles"]}
        profile_id = base_id
        suffix = 2
        while profile_id in ids:
            profile_id = f"{base_id}-{suffix}"
            suffix += 1
        current = _profile_defaults(profile_id, name)
        store["profiles"].append(current)
        store["active_profile_id"] = profile_id
    else:
        ids = {profile["id"] for profile in store["profiles"]}
        if requested_id not in ids:
            requested_id = store["active_profile_id"]
        store["active_profile_id"] = requested_id
        current = _active(store)
        name = str(payload.get("profile_name") or current["name"]).strip()[:60]
        if name:
            current["name"] = name
    current["accounts"] = _clean_accounts(payload.get("accounts", current["accounts"]))
    daily_time = str(payload.get("daily_time", current["daily_time"])).strip()
    current["daily_time"] = daily_time if TIME_PATTERN.fullmatch(daily_time) else "08:15"
    current["enabled"] = bool(payload.get("enabled", current["enabled"]))
    current["collector_path"] = str(payload.get("collector_path", current["collector_path"])).strip()
    current["collector_python"] = str(payload.get("collector_python", current["collector_python"])).strip()
    if payload.get("replace_credentials"):
        current["token"] = str(payload.get("token", "")).strip()
        current["cookie"] = str(payload.get("cookie", "")).strip()
    current["updated_at"] = now_iso()
    _write(store)
    return public_status(current, store)


def select_profile(profile_id: str) -> dict[str, Any]:
    store = load_store()
    if profile_id not in {profile["id"] for profile in store["profiles"]}:
        raise ValueError("未找到所选采集配置")
    store["active_profile_id"] = profile_id
    _write(store)
    return public_status(_active(store), store)


def delete_profile(profile_id: str) -> dict[str, Any]:
    store = load_store()
    profiles = store["profiles"]
    if profile_id not in {profile["id"] for profile in profiles}:
        raise ValueError("未找到要删除的采集配置")
    if len(profiles) <= 1:
        raise ValueError("至少保留一份配置；请先新建并保存另一份配置后再删除。")
    store["profiles"] = [profile for profile in profiles if profile["id"] != profile_id]
    if store["active_profile_id"] == profile_id:
        store["active_profile_id"] = store["profiles"][0]["id"]
    _write(store)
    return public_status(_active(store), store)


def _write(store: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="collector-settings-", dir=str(SETTINGS_PATH.parent), text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, SETTINGS_PATH)
        os.chmod(SETTINGS_PATH, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def credentials() -> tuple[str, str]:
    active = load_private()
    return str(active.get("token") or ""), str(active.get("cookie") or "")


def _profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": profile["id"], "name": profile["name"], "accounts": list(profile["accounts"]),
        "enabled": bool(profile["enabled"]), "credential_configured": bool(profile["token"] and profile["cookie"]),
        "daily_time": profile["daily_time"], "updated_at": profile["updated_at"],
    }


def public_status(profile: dict[str, Any] | None = None, store: dict[str, Any] | None = None) -> dict[str, Any]:
    store = store or load_store()
    profile = profile or _active(store)
    return {
        "active_profile_id": store["active_profile_id"],
        "profiles": [_profile_summary(item) for item in store["profiles"]],
        "profile_name": profile["name"],
        "accounts": list(profile["accounts"]),
        "daily_time": profile["daily_time"],
        "enabled": bool(profile["enabled"]),
        "credential_configured": bool(profile["token"] and profile["cookie"]),
        "collector_configured": bool(profile["collector_path"]),
        "collector_path": profile["collector_path"],
        "collector_python": profile["collector_python"],
        "last_run_at": profile["last_run_at"],
        "last_result": profile["last_result"],
        "updated_at": profile["updated_at"],
    }


def record_run(result: str, *, scheduled_date: str = "") -> None:
    store = load_store()
    active = _active(store)
    active["last_run_at"] = now_iso()
    active["last_result"] = result[:600]
    if scheduled_date:
        active["last_scheduled_date"] = scheduled_date
    _write(store)


def clear_credentials() -> dict[str, Any]:
    store = load_store()
    active = _active(store)
    active["token"] = ""
    active["cookie"] = ""
    active["enabled"] = False
    active["updated_at"] = now_iso()
    _write(store)
    return public_status(active, store)
