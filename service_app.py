#!/usr/bin/python
# coding:utf-8

import argparse
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, Optional

from api import consumeDeviceOwnerBalance, getDeviceOnlineStatus, getDeviceOwnerBenefit
from liveMan import DouyinLiveWebFetcher


UTC_PLUS_8 = timezone(timedelta(hours=8))


def utc_now_iso() -> str:
    return datetime.now(UTC_PLUS_8).isoformat()


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class MonitorTask:
    live_id: str
    device_id: str
    config_json: Optional[Dict[str, Any]] = None
    status: str = "starting"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    error: Optional[str] = None
    sent_prompt_count: int = 0
    last_sent_prompt_at: Optional[str] = None
    last_sent_prompt_preview: Optional[str] = None
    device_online: Optional[bool] = None
    device_online_checked_at: Optional[str] = None
    device_offline_since: Optional[str] = None
    room_ended_since: Optional[str] = None
    benefit_user_id: Optional[str] = None
    benefit_balance_seconds: Optional[int] = None
    benefit_membership_active: Optional[bool] = None
    benefit_membership_start_at: Optional[str] = None
    benefit_membership_end_at: Optional[str] = None
    benefit_checked_at: Optional[str] = None
    membership_last_checked_at: Optional[str] = None
    membership_expired_since: Optional[str] = None
    last_billed_at: Optional[str] = None
    billed_seconds_total: int = 0
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    fetcher: Optional[DouyinLiveWebFetcher] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        prompt_state = {}
        live_state = {}
        if self.fetcher and hasattr(self.fetcher, "get_prompt_state"):
            try:
                prompt_state = self.fetcher.get_prompt_state()
            except Exception:
                prompt_state = {}
        if self.fetcher and hasattr(self.fetcher, "get_live_state"):
            try:
                live_state = self.fetcher.get_live_state()
            except Exception:
                live_state = {}
        started_at_dt = parse_iso_datetime(self.started_at)
        stopped_at_dt = parse_iso_datetime(self.stopped_at)
        device_offline_since_dt = parse_iso_datetime(self.device_offline_since)
        room_ended_since_dt = parse_iso_datetime(self.room_ended_since)
        membership_expired_since_dt = parse_iso_datetime(self.membership_expired_since)
        now_dt = datetime.now(UTC_PLUS_8)
        runtime_seconds = None
        device_offline_duration_seconds = None
        room_ended_duration_seconds = None
        membership_expired_duration_seconds = None
        if started_at_dt:
            end_dt = stopped_at_dt or now_dt
            runtime_seconds = max(0, int((end_dt - started_at_dt).total_seconds()))
        if device_offline_since_dt:
            device_offline_duration_seconds = max(0, int((now_dt - device_offline_since_dt).total_seconds()))
        if room_ended_since_dt:
            room_ended_duration_seconds = max(0, int((now_dt - room_ended_since_dt).total_seconds()))
        if membership_expired_since_dt:
            membership_expired_duration_seconds = max(0, int((now_dt - membership_expired_since_dt).total_seconds()))
        return {
            "live_id": self.live_id,
            "device_id": self.device_id,
            "status": self.status,
            "is_active": self.status in {"starting", "running", "stopping"},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "runtime_seconds": runtime_seconds,
            "error": self.error,
            "sent_prompt_count": self.sent_prompt_count,
            "last_sent_prompt_at": self.last_sent_prompt_at,
            "last_sent_prompt_preview": self.last_sent_prompt_preview,
            "device_online": self.device_online,
            "device_online_checked_at": self.device_online_checked_at,
            "device_offline_since": self.device_offline_since,
            "device_offline_duration_seconds": device_offline_duration_seconds,
            "room_ended_since": self.room_ended_since,
            "room_ended_duration_seconds": room_ended_duration_seconds,
            "benefit_user_id": self.benefit_user_id,
            "benefit_balance_seconds": self.benefit_balance_seconds,
            "benefit_membership_active": self.benefit_membership_active,
            "benefit_membership_start_at": self.benefit_membership_start_at,
            "benefit_membership_end_at": self.benefit_membership_end_at,
            "benefit_checked_at": self.benefit_checked_at,
            "membership_last_checked_at": self.membership_last_checked_at,
            "membership_expired_since": self.membership_expired_since,
            "membership_expired_duration_seconds": membership_expired_duration_seconds,
            "last_billed_at": self.last_billed_at,
            "billed_seconds_total": self.billed_seconds_total,
            "room_status": live_state.get("room_status"),
            "current_viewer_count": live_state.get("current_viewer_count"),
            "total_viewer_count": live_state.get("total_viewer_count"),
            "room_stats_updated_at": live_state.get("room_stats_updated_at"),
            "prompt_state": prompt_state,
        }


class MonitorManager:
    def __init__(self):
        self._tasks: Dict[str, MonitorTask] = {}
        self._lock = threading.Lock()
        self._sent_events_by_device: Dict[str, deque] = {}
        self._event_seq = 0
        self._event_buffer_size = 200
        self._watchdog_started = False
        self._watchdog_lock = threading.Lock()
        self._cleanup_threshold = timedelta(minutes=30)
        self._watchdog_interval_seconds = 30
        self._membership_check_interval = timedelta(minutes=10)

    def _set_status_unlocked(self, task: MonitorTask, status: str, error: Optional[str] = None):
        task.status = status
        task.updated_at = utc_now_iso()
        if error is not None:
            task.error = error

    def _extract_device_exists(self, response: Dict[str, Any]) -> Optional[bool]:
        if not response or not response.get("ok"):
            return None
        payload = response.get("data", {})
        data = payload.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        for value in data.values():
            if isinstance(value, dict) and "exists" in value:
                return bool(value.get("exists"))
        return None

    def _extract_benefit_payload(self, response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not response or not response.get("ok"):
            return None
        payload = response.get("data", {})
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        return data

    def _apply_benefit_payload_unlocked(self, task: MonitorTask, benefit_payload: Dict[str, Any]):
        task.benefit_user_id = benefit_payload.get("userId")
        balance_seconds = benefit_payload.get("balanceSeconds")
        task.benefit_balance_seconds = int(balance_seconds) if balance_seconds is not None else None
        task.benefit_membership_active = bool(benefit_payload.get("membershipActive"))
        task.benefit_membership_start_at = benefit_payload.get("membershipStartAt")
        task.benefit_membership_end_at = benefit_payload.get("membershipEndAt")
        task.benefit_checked_at = utc_now_iso()
        task.updated_at = utc_now_iso()
        if task.benefit_membership_active:
            task.membership_expired_since = None

    def _refresh_monitor_benefit(self, device_id: str, now_dt: Optional[datetime] = None) -> bool:
        if now_dt is None:
            now_dt = datetime.now(UTC_PLUS_8)

        task = self.get_active_monitor_by_device(device_id)
        if not task:
            return False

        need_remote_benefit_check = False
        with self._lock:
            current = self._tasks.get(device_id)
            if not current:
                return False
            if current.benefit_membership_active:
                membership_end_dt = parse_iso_datetime(current.benefit_membership_end_at)
                last_checked_dt = parse_iso_datetime(current.membership_last_checked_at)
                if membership_end_dt and now_dt >= membership_end_dt:
                    need_remote_benefit_check = True
                elif current.membership_expired_since is not None:
                    if last_checked_dt is None or (now_dt - last_checked_dt) >= self._membership_check_interval:
                        need_remote_benefit_check = True
                    else:
                        expired_since_dt = parse_iso_datetime(current.membership_expired_since)
                        if expired_since_dt and (now_dt - expired_since_dt) >= self._cleanup_threshold:
                            current.error = "membership expired"
                            current.updated_at = utc_now_iso()
                            return False
                else:
                    return True

        if need_remote_benefit_check:
            benefit_payload = None
            try:
                benefit_payload = self._extract_benefit_payload(getDeviceOwnerBenefit(device_id))
            except Exception:
                benefit_payload = None

            with self._lock:
                current = self._tasks.get(device_id)
                if not current:
                    return False

                current.membership_last_checked_at = utc_now_iso()
                if benefit_payload is not None:
                    self._apply_benefit_payload_unlocked(current, benefit_payload)
                    if current.benefit_membership_active:
                        current.membership_expired_since = None
                        current.last_billed_at = utc_now_iso()
                        return True

                if current.membership_expired_since is None:
                    current.membership_expired_since = utc_now_iso()
                    current.updated_at = utc_now_iso()

                expired_since_dt = parse_iso_datetime(current.membership_expired_since)
                if expired_since_dt and (now_dt - expired_since_dt) >= self._cleanup_threshold:
                    current.error = "membership expired"
                    current.updated_at = utc_now_iso()
                    return False
                return True

        with self._lock:
            current = self._tasks.get(device_id)
            if not current:
                return False

            if current.last_billed_at is None:
                current.last_billed_at = utc_now_iso()
                if (current.benefit_balance_seconds or 0) <= 0:
                    current.error = "device owner benefit insufficient"
                    current.updated_at = utc_now_iso()
                    return False
                return True

            last_billed_dt = parse_iso_datetime(current.last_billed_at)
            if not last_billed_dt:
                current.last_billed_at = utc_now_iso()
                return True

            consume_seconds = int((now_dt - last_billed_dt).total_seconds())
            if consume_seconds <= 0:
                return True

        consume_result = None
        try:
            consume_result = consumeDeviceOwnerBalance(device_id, consume_seconds)
        except Exception:
            consume_result = None

        with self._lock:
            current = self._tasks.get(device_id)
            if not current:
                return False

            if consume_result and consume_result.get("ok"):
                current.last_billed_at = utc_now_iso()
                current.billed_seconds_total += consume_seconds
                if current.benefit_balance_seconds is not None:
                    current.benefit_balance_seconds = max(0, current.benefit_balance_seconds - consume_seconds)
                current.updated_at = utc_now_iso()
                return True

            current.error = "device owner balance insufficient"
            current.updated_at = utc_now_iso()
            return False

    def start_watchdog(self):
        with self._watchdog_lock:
            if self._watchdog_started:
                return
            thread = threading.Thread(target=self._watchdog_loop, daemon=True)
            thread.start()
            self._watchdog_started = True

    def _watchdog_loop(self):
        while True:
            self._run_watchdog_once()
            time.sleep(self._watchdog_interval_seconds)

    def _refresh_monitor_health(self, device_id: str, now_dt: Optional[datetime] = None) -> bool:
        if now_dt is None:
            now_dt = datetime.now(UTC_PLUS_8)

        task = self.get_active_monitor_by_device(device_id)
        if not task:
            return False

        device_online = None
        try:
            device_online = self._extract_device_exists(getDeviceOnlineStatus(device_id))
        except Exception:
            device_online = None

        live_state = {}
        fetcher = task.fetcher
        if fetcher and hasattr(fetcher, "get_live_state"):
            try:
                live_state = fetcher.get_live_state()
            except Exception:
                live_state = {}

        should_delete = False
        with self._lock:
            current = self._tasks.get(device_id)
            if not current:
                return False

            if device_online is not None:
                current.device_online = device_online
                current.device_online_checked_at = utc_now_iso()
                current.updated_at = utc_now_iso()
                if device_online:
                    current.device_offline_since = None
                elif current.device_offline_since is None:
                    current.device_offline_since = utc_now_iso()

            room_status = live_state.get("room_status")
            if room_status == "ended":
                if current.room_ended_since is None:
                    current.room_ended_since = utc_now_iso()
                    current.updated_at = utc_now_iso()
            elif room_status == "running":
                if current.room_ended_since is not None:
                    current.room_ended_since = None
                    current.updated_at = utc_now_iso()

            offline_since_dt = parse_iso_datetime(current.device_offline_since)
            room_ended_since_dt = parse_iso_datetime(current.room_ended_since)
            if offline_since_dt and (now_dt - offline_since_dt) >= self._cleanup_threshold:
                should_delete = True
            if room_ended_since_dt and (now_dt - room_ended_since_dt) >= self._cleanup_threshold:
                should_delete = True

        if should_delete:
            self.delete_monitor(device_id)
            return False
        return True

    def _run_watchdog_once(self):
        with self._lock:
            device_ids = list(self._tasks.keys())

        now_dt = datetime.now(UTC_PLUS_8)
        for device_id in device_ids:
            health_ok = self._refresh_monitor_health(device_id, now_dt=now_dt)
            if not health_ok:
                continue
            benefit_ok = self._refresh_monitor_benefit(device_id, now_dt=now_dt)
            if not benefit_ok:
                self.delete_monitor(device_id)

    def create_monitor(
        self,
        live_id: str,
        device_id: str,
        config_json: Optional[Dict[str, Any]] = None,
    ) -> MonitorTask:
        with self._lock:
            existing_task = self._tasks.get(device_id)
            if existing_task and existing_task.status not in {"stopped", "error"}:
                raise ValueError(f"device_id already has active monitor: {device_id}")

        initial_benefit = None
        try:
            initial_benefit = self._extract_benefit_payload(getDeviceOwnerBenefit(device_id))
        except Exception:
            initial_benefit = None
        if not initial_benefit:
            raise ValueError("failed to get device owner benefit")
        if not bool(initial_benefit.get("membershipActive")) and int(initial_benefit.get("balanceSeconds") or 0) <= 0:
            raise ValueError("device owner benefit insufficient")

        task = MonitorTask(
            live_id=live_id,
            device_id=device_id,
            config_json=config_json,
            status="starting",
        )
        task.last_billed_at = utc_now_iso()
        task.membership_last_checked_at = utc_now_iso()
        self._apply_benefit_payload_unlocked(task, initial_benefit)
        fetcher = DouyinLiveWebFetcher(
            live_id=live_id,
            device_id=device_id,
            config_json=config_json,
            sent_prompt_callback=lambda payload: self.record_sent_prompt(device_id, payload),
        )
        thread = threading.Thread(target=self._run_fetcher, args=(device_id,), daemon=True)
        task.fetcher = fetcher
        task.thread = thread
        with self._lock:
            self._tasks[device_id] = task
        thread.start()
        self._refresh_monitor_health(device_id)
        return task

    def _run_fetcher(self, device_id: str):
        with self._lock:
            task = self._tasks.get(device_id)
            if not task:
                return
            self._set_status_unlocked(task, "running")
            task.started_at = utc_now_iso()
            fetcher = task.fetcher

        try:
            fetcher.start()
            with self._lock:
                current = self._tasks.get(device_id)
                if current and current.status not in {"stopped", "error"}:
                    self._set_status_unlocked(current, "stopped")
                    current.stopped_at = utc_now_iso()
        except Exception as err:
            with self._lock:
                current = self._tasks.get(device_id)
                if current:
                    self._set_status_unlocked(current, "error", str(err))
                    current.stopped_at = utc_now_iso()

    def stop_monitor(self, device_id: str) -> Optional[MonitorTask]:
        with self._lock:
            task = self._tasks.get(device_id)
            if not task:
                return None
            if task.status in {"stopped", "error"}:
                return task
            self._set_status_unlocked(task, "stopping")
            fetcher = task.fetcher
            thread = task.thread

        if fetcher:
            try:
                fetcher.stop()
            except Exception as err:
                with self._lock:
                    task = self._tasks.get(device_id)
                    if task:
                        self._set_status_unlocked(task, "error", str(err))
                        task.stopped_at = utc_now_iso()
                        return task

        if thread and thread.is_alive():
            thread.join(timeout=3)

        with self._lock:
            task = self._tasks.get(device_id)
            if task and task.status not in {"error", "stopped"}:
                self._set_status_unlocked(task, "stopped")
                task.stopped_at = utc_now_iso()
            return task

    def delete_monitor(self, device_id: str) -> Optional[MonitorTask]:
        task = self.stop_monitor(device_id)
        if not task:
            return None
        with self._lock:
            return self._tasks.pop(device_id, task)

    def list_monitors(self) -> Dict[str, Any]:
        with self._lock:
            return {device_id: task.to_dict() for device_id, task in self._tasks.items()}

    def get_active_monitor_by_device(self, device_id: str) -> Optional[MonitorTask]:
        with self._lock:
            task = self._tasks.get(device_id)
            if task and task.status not in {"stopped", "error"}:
                return task
        return None

    def allow_prompt_by_device(self, device_id: str) -> Optional[MonitorTask]:
        task = self.get_active_monitor_by_device(device_id)
        if not task:
            return None
        fetcher = task.fetcher
        if fetcher and hasattr(fetcher, "allow_send_prompt"):
            fetcher.allow_send_prompt()
        return task

    def record_sent_prompt(self, device_id: str, payload: Dict[str, Any]):
        with self._lock:
            task = self._tasks.get(device_id)
            if not task:
                return
            task.sent_prompt_count += 1
            task.last_sent_prompt_at = utc_now_iso()
            task.last_sent_prompt_preview = (payload.get("prompt") or "")[:120]
            task.updated_at = utc_now_iso()
            device_id = task.device_id
            events = self._sent_events_by_device.get(device_id)
            if events is None:
                events = deque(maxlen=self._event_buffer_size)
                self._sent_events_by_device[device_id] = events
            self._event_seq += 1
            events.append({
                "id": self._event_seq,
                "device_id": device_id,
                "live_id": payload.get("live_id"),
                "room_id": payload.get("room_id"),
                "prompt": payload.get("prompt"),
                "created_at": utc_now_iso(),
            })

    def get_sent_prompts_by_device(self, device_id: str, after_id: int = 0, limit: int = 50):
        with self._lock:
            events = list(self._sent_events_by_device.get(device_id, []))
        filtered = [event for event in events if event["id"] > after_id]
        return filtered[:limit]


MANAGER = MonitorManager()


class ServiceHandler(BaseHTTPRequestHandler):
    server_version = "DouyinMonitorAPI/1.0"

    def _extract_monitor_configs(self, payload: Dict[str, Any]):
        config_json = payload.get("config_json")
        if isinstance(config_json, str):
            try:
                config_json = json.loads(config_json)
            except json.JSONDecodeError as err:
                raise ValueError(f"invalid config_json: {err.msg}") from err

        if config_json is None:
            config_json = {}
        if not isinstance(config_json, dict):
            raise ValueError("config_json must be an object")

        return {
            "config_json": config_json,
        }

    def _set_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, payload: Dict[str, Any], status_code: int = 200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        body = self.rfile.read(content_length).decode("utf-8")
        if not body.strip():
            return {}
        return json.loads(body)

    def _not_found(self):
        self._send_json({"code": 404, "msg": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json({"code": 0, "msg": "ok", "timestamp": utc_now_iso()})
            return

        if path == "/monitors":
            self._send_json({"code": 0, "data": MANAGER.list_monitors()})
            return

        if path.startswith("/sent-prompts/device/"):
            device_id = path[len("/sent-prompts/device/"):].strip("/")
            if not device_id:
                self._send_json({"code": 400, "msg": "invalid device id"})
                return
            try:
                after_id = int(query.get("after_id", ["0"])[0])
            except (TypeError, ValueError):
                after_id = 0
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            limit = max(1, min(limit, 200))
            events = MANAGER.get_sent_prompts_by_device(device_id, after_id=after_id, limit=limit)
            self._send_json({
                "code": 0,
                "device_id": device_id,
                "count": len(events),
                "data": events,
            })
            return

        if path.startswith("/monitors/device/"):
            device_id = path[len("/monitors/device/"):].strip("/")
            if not device_id:
                self._send_json({"code": 400, "msg": "invalid device id"})
                return
            task = MANAGER.get_active_monitor_by_device(device_id)
            if not task:
                self._send_json({"code": 404, "msg": "no active monitor for this device_id"})
                return
            self._send_json({
                "code": 0,
                "device_id": device_id,
                "data": task.to_dict(),
            })
            return

        self._not_found()

    def do_POST(self):
        if self.path.startswith("/monitors/allow-prompt/"):
            device_id = self.path[len("/monitors/allow-prompt/"):].strip("/")
            if not device_id:
                self._send_json({"code": 400, "msg": "invalid device id"})
                return
            task = MANAGER.allow_prompt_by_device(device_id)
            if not task:
                self._send_json({"code": 404, "msg": "no active monitor for this device_id"})
                return
            self._send_json({
                "code": 0,
                "msg": "prompt unlocked",
                "device_id": device_id,
                "data": task.to_dict(),
            })
            return

        if self.path != "/monitors":
            self._not_found()
            return

        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json({"code": 400, "msg": "invalid json"})
            return

        live_id = str(payload.get("live_id", "")).strip()
        device_id = str(payload.get("device_id", "")).strip()

        if not live_id or not device_id:
            self._send_json({"code": 400, "msg": "live_id and device_id are required"})
            return

        try:
            config_payload = self._extract_monitor_configs(payload)
        except ValueError as err:
            self._send_json({"code": 400, "msg": str(err)})
            return

        try:
            task = MANAGER.create_monitor(
                live_id=live_id,
                device_id=device_id,
                config_json=config_payload.get("config_json"),
            )
        except ValueError as err:
            self._send_json({"code": 409, "msg": str(err)})
            return
        self._send_json({"code": 0, "msg": "monitor created", "data": task.to_dict()})

    def do_DELETE(self):
        if self.path.startswith("/monitors/device/"):
            device_id = self.path[len("/monitors/device/"):].strip("/")
            if not device_id:
                self._send_json({"code": 400, "msg": "invalid device id"})
                return
            task = MANAGER.delete_monitor(device_id)
            if not task:
                self._send_json({"code": 404, "msg": "no active monitor for this device_id"})
                return
            self._send_json({
                "code": 0,
                "msg": "monitor deleted",
                "device_id": device_id,
                "data": task.to_dict(),
            })
            return

        self._not_found()

    def log_message(self, format: str, *args):
        message = "%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args)
        print(message, end="")


def run_server(host: str = "0.0.0.0", port: int = 18080):
    MANAGER.start_watchdog()
    server = ThreadingHTTPServer((host, port), ServiceHandler)
    print(f"API server listening on http://{host}:{port}")
    print("Endpoints: GET /health, POST /monitors, GET /monitors, GET /monitors/device/{device_id}, GET /sent-prompts/device/{device_id}, DELETE /monitors/device/{device_id}, POST /monitors/allow-prompt/{device_id}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down API server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Douyin live monitor API service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()

    run_server(host=args.host, port=args.port)
