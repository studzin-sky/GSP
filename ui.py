#!/usr/bin/env python3
"""Local Web UI server for GSP (Garmin Strength Progression).

Uses Python's standard-library http.server + a single self-contained ui.html.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import threading
import traceback
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from garminconnect import Garmin

import progression as gsp

UI_HTML_PATH = gsp.ROOT / "ui.html"

_lock = threading.Lock()
_cached_client: Garmin | None = None
_pending_mfa: tuple[Garmin, Any] | None = None
_explicit_logout: bool = False


def _tokenstore_has_files() -> bool:
    return gsp.TOKENSTORE.is_dir() and any(gsp.TOKENSTORE.iterdir())


def _get_client() -> Garmin | None:
    """Return an authenticated Garmin client if tokens exist, else None."""
    global _cached_client
    if _cached_client is not None:
        return _cached_client
    if _explicit_logout and not _tokenstore_has_files():
        return None

    gsp.TOKENSTORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    email = None if _explicit_logout else (gsp.os.environ.get("GARMIN_EMAIL") or None)
    password = None if _explicit_logout else (gsp.os.environ.get("GARMIN_PASSWORD") or None)

    if not _tokenstore_has_files() and not (email and password):
        return None

    try:
        client = Garmin(email, password, return_on_mfa=True)
        mfa_status, _ = client.login(str(gsp.TOKENSTORE))
        if mfa_status == "needs_mfa":
            return None
        _cached_client = client
        return client
    except Exception:  # noqa: BLE001
        return None


def _build_status(client: Garmin | None) -> dict[str, Any]:
    if client is None:
        return {
            "loggedIn": False,
            "userName": None,
            "displayName": None,
            "devices": [],
            "lastUsedDevice": None,
            "selectedDeviceId": None,
            "defaultDays": gsp.DEFAULT_DAYS,
        }

    devices = []
    try:
        for d in client.get_devices() or []:
            devices.append({
                "deviceId": d.get("deviceId"),
                "displayName": d.get("displayName") or d.get("productDisplayName") or "Garmin Device",
                "productDisplayName": d.get("productDisplayName") or d.get("displayName") or "Garmin Device",
                "deviceStatus": d.get("deviceStatus"),
                "firmware": d.get("currentFirmwareVersion"),
                "imageUrl": d.get("imageUrl"),
            })
    except Exception:  # noqa: BLE001
        devices = []

    last_used = None
    try:
        lu = client.get_device_last_used() or {}
        if lu.get("userDeviceId"):
            last_used = {
                "deviceId": lu.get("userDeviceId"),
                "name": lu.get("lastUsedDeviceName") or "Last Used Watch",
                "imageUrl": lu.get("imageUrl"),
            }
    except Exception:  # noqa: BLE001
        last_used = None

    # Ensure last_used device appears in devices list even if get_devices() omitted it
    if last_used and not any(d["deviceId"] == last_used["deviceId"] for d in devices):
        devices.insert(0, {
            "deviceId": last_used["deviceId"],
            "displayName": last_used["name"],
            "productDisplayName": last_used["name"],
            "deviceStatus": "active",
            "firmware": None,
            "imageUrl": last_used.get("imageUrl"),
        })

    selected_id = (
        (last_used or {}).get("deviceId")
        or (devices[0]["deviceId"] if devices else None)
    )

    return {
        "loggedIn": True,
        "userName": getattr(client, "full_name", None) or getattr(client, "display_name", None) or "Garmin User",
        "displayName": getattr(client, "display_name", None),
        "devices": devices,
        "lastUsedDevice": last_used,
        "selectedDeviceId": selected_id,
        "defaultDays": gsp.DEFAULT_DAYS,
    }


def _handle_login(payload: dict[str, Any]) -> dict[str, Any]:
    global _cached_client, _pending_mfa, _explicit_logout
    email = (payload.get("email") or "").strip()
    password = payload.get("password") or ""
    if not email or not password:
        raise ValueError("Email and password are required.")

    gsp.TOKENSTORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = Garmin(email, password, return_on_mfa=True)
    mfa_status, client_state = client.login()
    if mfa_status == "needs_mfa":
        _pending_mfa = (client, client_state)
        return {"ok": True, "needsMfa": True}

    client.client.dump(str(gsp.TOKENSTORE))
    _cached_client = client
    _pending_mfa = None
    _explicit_logout = False
    return {"ok": True, "needsMfa": False, "status": _build_status(client)}


def _handle_mfa(payload: dict[str, Any]) -> dict[str, Any]:
    global _cached_client, _pending_mfa, _explicit_logout
    code = (payload.get("code") or "").strip()
    if not code:
        raise ValueError("MFA code is required.")
    if _pending_mfa is None:
        raise ValueError("No pending MFA login session. Please log in again.")

    client, client_state = _pending_mfa
    client.resume_login(client_state, code)
    gsp.TOKENSTORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    client.client.dump(str(gsp.TOKENSTORE))
    _cached_client = client
    _pending_mfa = None
    _explicit_logout = False
    return {"ok": True, "status": _build_status(client)}


def _handle_logout() -> dict[str, Any]:
    global _cached_client, _pending_mfa, _explicit_logout
    _cached_client = None
    _pending_mfa = None
    _explicit_logout = True
    if gsp.TOKENSTORE.is_dir():
        for child in gsp.TOKENSTORE.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    return {"ok": True, "status": _build_status(None)}


def _handle_preview(days: int) -> dict[str, Any]:
    client = _get_client()
    if client is None:
        raise PermissionError("Not logged in to Garmin Connect.")

    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    activities = [
        a for a in client.get_activities_by_date(start, activitytype="fitness_equipment")
        if ((a.get("activityType") or {}).get("typeKey")) in gsp.SUPPORTED_TYPES
    ]
    activities.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)

    workouts = [
        w for w in client.get_workouts(limit=200)
        if ((w.get("sportType") or {}).get("sportTypeKey")) in gsp.SUPPORTED_TYPES
    ]

    templates_out: list[dict[str, Any]] = []
    for summary in workouts:
        wid = summary["workoutId"]
        name = summary.get("workoutName") or f"Workout {wid}"
        sport_key = ((summary.get("sportType") or {}).get("sportTypeKey")) or "strength_training"
        workout = client.get_workout_by_id(wid)
        act = gsp.find_latest_activity(summary, activities)

        if act:
            sets = (client.get_activity_exercise_sets(act["activityId"]) or {}).get("exerciseSets") or []
            _new, changes, details = gsp.plan_update_detailed(workout, sets)
            templates_out.append({
                "workoutId": wid,
                "workoutName": name,
                "sportType": sport_key,
                "updatedDate": workout.get("updatedDate"),
                "matchedActivity": {
                    "activityId": act.get("activityId"),
                    "activityName": act.get("activityName"),
                    "startTimeLocal": act.get("startTimeLocal"),
                    "duration": act.get("duration"),
                },
                "hasChanges": details["hasChanges"],
                "steps": details["steps"],
                "extraSetsCount": details["extraSetsCount"],
                "extraExercises": details["extraExercises"],
                "changesText": changes,
            })
        else:
            _new, _changes, details = gsp.plan_update_detailed(workout, [])
            templates_out.append({
                "workoutId": wid,
                "workoutName": name,
                "sportType": sport_key,
                "updatedDate": workout.get("updatedDate"),
                "matchedActivity": None,
                "hasChanges": False,
                "steps": details["steps"],
                "extraSetsCount": 0,
                "extraExercises": [],
                "changesText": [],
            })

    return {
        "days": days,
        "startDate": start,
        "activitiesCount": len(activities),
        "templates": templates_out,
    }


def _handle_update(payload: dict[str, Any]) -> dict[str, Any]:
    client = _get_client()
    if client is None:
        raise PermissionError("Not logged in to Garmin Connect.")

    push = bool(payload.get("push", True))
    raw_device_id = payload.get("deviceId")
    device_id = int(raw_device_id) if raw_device_id not in (None, "", 0, "0") else None
    workouts_payload = payload.get("workouts") or []
    if not workouts_payload:
        raise ValueError("No workout templates selected for update.")

    gsp.BACKUP_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []

    for item in workouts_payload:
        wid = int(item["workoutId"])
        old = client.get_workout_by_id(wid)
        name = old.get("workoutName") or f"Workout {wid}"
        new = gsp.apply_step_overrides(old, item.get("steps") or [])

        safe = "".join(c if c.isalnum() else "_" for c in name)
        backup_file = f"{stamp}-{safe}.json"
        (gsp.BACKUP_DIR / backup_file).write_text(json.dumps(old, indent=2))

        client.update_workout(wid, new)
        pushed = False
        if push:
            client.push_workout_to_device(wid, device_id)
            pushed = True

        results.append({
            "workoutId": wid,
            "workoutName": name,
            "backupFile": f"backups/{backup_file}",
            "pushed": pushed,
            "deviceId": device_id,
        })

    return {"ok": True, "results": results}


def _handle_push(payload: dict[str, Any]) -> dict[str, Any]:
    client = _get_client()
    if client is None:
        raise PermissionError("Not logged in to Garmin Connect.")

    wid = int(payload["workoutId"])
    raw_device_id = payload.get("deviceId")
    device_id = int(raw_device_id) if raw_device_id not in (None, "", 0, "0") else None
    client.push_workout_to_device(wid, device_id)
    return {"ok": True, "workoutId": wid, "deviceId": device_id}


class GSPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep console output concise
        sys_msg = fmt % args
        print(f"[UI] {self.command} {self.path} -> {sys_msg}")

    def _send_json(self, data: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not UI_HTML_PATH.is_file():
                self._send_json({"error": "ui.html not found"}, HTTPStatus.NOT_FOUND)
                return
            html = UI_HTML_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(html)
            return

        try:
            with _lock:
                if path == "/api/status":
                    client = _get_client()
                    self._send_json(_build_status(client))
                    return
                if path == "/api/preview":
                    qs = urllib.parse.parse_qs(parsed.query)
                    days = int((qs.get("days") or [gsp.DEFAULT_DAYS])[0])
                    days = max(1, min(days, 90))
                    self._send_json(_handle_preview(days))
                    return
        except PermissionError as e:
            self._send_json({"error": str(e)}, HTTPStatus.UNAUTHORIZED)
            return
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            payload = self._read_json_body()
            with _lock:
                if path == "/api/login":
                    self._send_json(_handle_login(payload))
                    return
                if path == "/api/mfa":
                    self._send_json(_handle_mfa(payload))
                    return
                if path == "/api/logout":
                    self._send_json(_handle_logout())
                    return
                if path == "/api/update":
                    self._send_json(_handle_update(payload))
                    return
                if path == "/api/push":
                    self._send_json(_handle_push(payload))
                    return
        except PermissionError as e:
            self._send_json({"error": str(e)}, HTTPStatus.UNAUTHORIZED)
            return
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def run_ui_server(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    url = f"http://{host}:{port}"
    server = ThreadingHTTPServer((host, port), GSPRequestHandler)
    print(f"GSP Web UI running at {url} (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GSP Web UI.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_ui_server()
