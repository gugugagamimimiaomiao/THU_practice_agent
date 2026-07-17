"""Optional, evidence-preserving live transport lookup for the action workspace.

The endpoint is intentionally opt-in: a user-entered hotel address is only sent
to AMap when an administrator configures ``AMAP_WEB_SERVICE_KEY`` and the user
presses the explicit query button.  Without a key, callers receive a useful
manual-map fallback rather than fabricated route data.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


AMAP_BASE = "https://restapi.amap.com/v3"


def _request(path: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"{AMAP_BASE}{path}?{urlencode(params)}"
    with urlopen(url, timeout=8) as response:  # nosec B310 - fixed AMap host
        payload = json.loads(response.read().decode("utf-8"))
    if str(payload.get("status")) != "1":
        raise ValueError(str(payload.get("info") or "地图服务未返回有效结果"))
    return payload


def _geocode(key: str, address: str, city: str) -> dict[str, Any] | None:
    payload = _request("/geocode/geo", {"key": key, "address": address, "city": city})
    items = payload.get("geocodes") or []
    if not items or not items[0].get("location"):
        return None
    return items[0]


def _minutes(seconds: Any) -> int | None:
    try:
        return max(1, round(int(seconds) / 60))
    except (TypeError, ValueError):
        return None


def _distance_km(metres: Any) -> float | None:
    try:
        return round(int(metres) / 1000, 1)
    except (TypeError, ValueError):
        return None


def _transit_summary(key: str, origin: str, destination: str, city: str) -> dict[str, Any] | None:
    payload = _request("/direction/transit/integrated", {
        "key": key, "origin": origin, "destination": destination, "city": city, "cityd": city,
        "strategy": "0",  # shortest time; this is a stated selection criterion, not a hidden guess
    })
    transits = (payload.get("route") or {}).get("transits") or []
    if not transits:
        return None
    transit = transits[0]
    lines: list[str] = []
    for segment in transit.get("segments") or []:
        for line in ((segment.get("bus") or {}).get("buslines") or []):
            name = str(line.get("name") or "").strip()
            if name and name not in lines:
                lines.append(name)
    return {
        "minutes": _minutes(transit.get("duration")), "distance_km": _distance_km(transit.get("distance")),
        "cost_yuan": transit.get("cost"), "lines": lines[:5],
    }


def _driving_summary(key: str, origin: str, destination: str) -> dict[str, Any] | None:
    payload = _request("/direction/driving", {"key": key, "origin": origin, "destination": destination, "strategy": "0"})
    paths = (payload.get("route") or {}).get("paths") or []
    if not paths:
        return None
    path = paths[0]
    return {"minutes": _minutes(path.get("duration")), "distance_km": _distance_km(path.get("distance")), "tolls_yuan": path.get("tolls")}


def _walking_summary(key: str, origin: str, destination: str) -> dict[str, Any] | None:
    payload = _request("/direction/walking", {"key": key, "origin": origin, "destination": destination})
    paths = ((payload.get("route") or {}).get("paths") or [])
    if not paths:
        return None
    path = paths[0]
    return {"minutes": _minutes(path.get("duration")), "distance_km": _distance_km(path.get("distance"))}


def _decision(transit: dict[str, Any] | None, driving: dict[str, Any] | None, walking: dict[str, Any] | None) -> str:
    walk_minutes = (walking or {}).get("minutes")
    transit_minutes = (transit or {}).get("minutes")
    drive_minutes = (driving or {}).get("minutes")
    if walk_minutes is not None and walk_minutes <= 15:
        return "优先步行：距离较近，便于保留抵达缓冲。"
    if transit_minutes is not None and (drive_minutes is None or transit_minutes <= drive_minutes + 15):
        lines = "、".join((transit or {}).get("lines") or [])
        return f"优先公共交通：预计 {transit_minutes} 分钟" + (f"，可核验线路 {lines}" if lines else "") + "。"
    if drive_minutes is not None:
        return f"优先出租车/网约车：预计 {drive_minutes} 分钟；预约访谈前仍应预留堵车缓冲。"
    return "地图未给出可比较的路线；请手动核验。"


def query_routes(*, hotel: str, sites: list[dict[str, Any]], city: str) -> dict[str, Any]:
    key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    if not key:
        return {
            "configured": False,
            "message": "未配置 AMAP_WEB_SERVICE_KEY，无法自动查询实时公交/地铁与驾车路线。可点击地图核验并粘贴结果；系统不会猜测线路。",
            "routes": [],
        }
    hotel = str(hotel or "").strip()
    if not hotel:
        raise ValueError("请先填写酒店位置")
    if not sites:
        raise ValueError("请至少选择一个当地外联地点")
    origin = _geocode(key, hotel, city)
    if not origin:
        return {"configured": True, "message": "未能定位酒店位置，请改为更完整的酒店名称或地址。", "routes": []}
    routes: list[dict[str, Any]] = []
    errors: list[str] = []
    for site in sites[:5]:
        name = str(site.get("name") or "").strip()
        if not name:
            continue
        try:
            destination = _geocode(key, name, city)
            if not destination:
                errors.append(f"未定位：{name}")
                continue
            transit = _transit_summary(key, origin["location"], destination["location"], city)
            driving = _driving_summary(key, origin["location"], destination["location"])
            walking = _walking_summary(key, origin["location"], destination["location"])
            routes.append({
                "site": name, "resolved_address": destination.get("formatted_address") or name,
                "transit": transit, "driving": driving, "walking": walking,
                "recommendation": _decision(transit, driving, walking),
            })
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{name}：{exc}")
    message = "已查询实时地图线路。" if routes else "地图服务未返回可用路线。"
    if errors:
        message += " 未完成：" + "；".join(errors[:3])
    return {"configured": True, "message": message, "routes": routes}
