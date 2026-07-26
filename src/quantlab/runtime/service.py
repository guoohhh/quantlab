from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any

from quantlab.config import Settings
from quantlab.persistence.round6 import Round6Repository
from quantlab.runtime.notification_delivery import NotificationDeliveryWorker
from quantlab.runtime.readiness import runtime_health
from quantlab.runtime.soak import capture_soak_observation
from quantlab.runtime.scheduler import RuntimeScheduler
from quantlab.runtime.worker import JobWorker


RUNTIME_COMPONENTS = ("api", "worker", "scheduler", "notification_worker")


def run_runtime_component(
    settings: Settings,
    component: str,
    *,
    instance_id: str | None = None,
) -> dict[str, Any]:
    if component not in RUNTIME_COMPONENTS:
        raise ValueError(f"unsupported runtime component: {component}")
    from quantlab.persistence.migrations import ensure_database_initialized

    database_path = settings.resolve(settings.get("system.database_path"))
    ensure_database_initialized(database_path)
    repository = Round6Repository(database_path)
    identity = instance_id or f"{component}-{uuid.uuid4()}"
    claimed = repository.claim_process(
        component,
        instance_id=identity,
        stale_after_seconds=int(settings.get("runtime.runtime_process_stale_after_seconds", 90)),
        detail={"host": os.environ.get("COMPUTERNAME") or "localhost"},
    )
    if not claimed["claimed"]:
        return claimed
    try:
        if component == "api":
            result = _run_api(settings, repository, identity)
        elif component == "worker":
            result = _run_job_worker(settings, repository, identity)
        elif component == "scheduler":
            result = _run_scheduler(settings, repository, identity)
        else:
            result = _run_notification_worker(settings, repository, identity)
        repository.finish_process(component, identity, status="stopped", detail=result)
        return {"component": component, "instance_id": identity, **result}
    except BaseException as exc:
        repository.finish_process(
            component,
            identity,
            status="failed",
            detail={"error_type": type(exc).__name__},
        )
        raise


class RuntimeServiceController:
    """Starts and stops the four local Windows processes without exposing secrets in arguments."""

    def __init__(self, settings: Settings, *, config_path: Path | None = None):
        from quantlab.persistence.migrations import ensure_database_initialized

        ensure_database_initialized(settings.resolve(settings.get("system.database_path")))
        self.settings = settings
        self.config_path = config_path
        self.repository = Round6Repository(
            settings.resolve(settings.get("system.database_path"))
        )

    def start(self) -> dict[str, Any]:
        launched = []
        existing = {item["process_type"]: item for item in self.repository.processes()}
        maximum_age = float(settings_value(self.settings, "runtime.runtime_health_maximum_age_seconds", 90))
        now = datetime.now(UTC)
        for component in RUNTIME_COMPONENTS:
            item = existing.get(component)
            if item and item["status"] in {"running", "stopping"}:
                heartbeat = datetime.fromisoformat(item["heartbeat_at"])
                if (now - heartbeat.astimezone(UTC)).total_seconds() <= maximum_age:
                    launched.append(
                        {"component": component, "status": "already_running", "pid": item["pid"]}
                    )
                    continue
            command = [
                sys.executable,
                "-m",
                "quantlab.cli",
                "runtime-component",
                component,
            ]
            if self.config_path is not None:
                command.extend(["--config", str(self.config_path)])
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter and internal command
                command,
                cwd=str(self.settings.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
            launched.append({"component": component, "status": "launched", "pid": process.pid})
        return {"components": launched, "single_host": True, "database": str(self.repository.path)}

    def stop(self, *, grace_seconds: float = 15.0) -> dict[str, Any]:
        requested = self.repository.request_stop()
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            active = [
                item
                for item in self.repository.processes()
                if item["status"] in {"running", "stopping"}
            ]
            if not active:
                return {"stop_requested": requested, "forced": [], "status": "stopped"}
            time.sleep(0.2)
        forced = []
        for item in self.repository.processes():
            if item["status"] not in {"running", "stopping"}:
                continue
            try:
                os.kill(int(item["pid"]), signal.SIGTERM)
                forced.append({"component": item["process_type"], "pid": item["pid"]})
            except (OSError, ProcessLookupError):
                continue
        return {"stop_requested": requested, "forced": forced, "status": "stop_signalled"}

    def status(self) -> dict[str, Any]:
        return runtime_health(self.settings)


def _run_job_worker(
    settings: Settings,
    repository: Round6Repository,
    instance_id: str,
) -> dict[str, Any]:
    worker = JobWorker(settings, worker_id=instance_id)
    idle_seconds = float(settings.get("runtime.worker_idle_poll_seconds", 1.0))
    handled = 0
    while not repository.stop_requested("worker", instance_id):
        result = worker.run_once()
        handled += int(result is not None)
        repository.heartbeat_process(
            "worker",
            instance_id,
            detail={"handled_jobs": handled, "last_job_status": result.get("status") if result else None},
        )
        if result is None:
            time.sleep(max(0.05, idle_seconds))
    return {"status": "stopped", "handled_jobs": handled}


def _run_scheduler(
    settings: Settings,
    repository: Round6Repository,
    instance_id: str,
) -> dict[str, Any]:
    scheduler = RuntimeScheduler(settings)
    poll_seconds = float(settings.get("runtime.scheduler_poll_seconds", 30.0))
    ticks = 0
    last_result: dict[str, Any] | None = None
    while not repository.stop_requested("scheduler", instance_id):
        try:
            last_result = _scheduler_result_summary(scheduler.tick(now=datetime.now(UTC)))
            try:
                capture_soak_observation(settings, source="scheduler")
                last_result["soak_observation"] = "saved"
            except Exception as exc:
                last_result["soak_observation"] = f"failed:{type(exc).__name__}"
            status = "ok"
        except Exception as exc:
            last_result = {"status": "failed", "error_type": type(exc).__name__}
            status = "failed"
        ticks += 1
        repository.heartbeat_process(
            "scheduler",
            instance_id,
            detail={"ticks": ticks, "last_tick_status": status, "last_result": last_result},
        )
        _wait_for_stop(repository, "scheduler", instance_id, poll_seconds)
    return {"status": "stopped", "ticks": ticks, "last_result": last_result}


def _scheduler_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Persist a bounded operational summary, never the full recursive readiness tree."""

    calendar = result.get("calendar") or {}
    readiness = result.get("readiness") or {}
    jobs = list(result.get("jobs") or [])
    skipped = list(result.get("skipped") or [])
    return {
        "run_date": result.get("run_date"),
        "calendar": {
            "status": calendar.get("status"),
            "is_open": calendar.get("is_open"),
            "source": calendar.get("source"),
            "manifest_id": calendar.get("manifest_id"),
        },
        "jobs": [
            {
                "job_id": item.get("job_id"),
                "job_type": item.get("job_type"),
                "status": item.get("status"),
            }
            for item in jobs
        ],
        "job_count": len(jobs),
        "skipped_count": len(skipped),
        "skipped": [
            {
                "name": item.get("name"),
                "reason": item.get("reason"),
            }
            for item in skipped
        ],
        "readiness": {
            "as_of": readiness.get("as_of"),
            "start_allowed": readiness.get("start_allowed"),
            "sample_registration_allowed": readiness.get("sample_registration_allowed"),
            "blockers": list(readiness.get("blockers") or []),
        },
        "idempotent": bool(result.get("idempotent", False)),
    }


def _run_notification_worker(
    settings: Settings,
    repository: Round6Repository,
    instance_id: str,
) -> dict[str, Any]:
    worker = NotificationDeliveryWorker(settings, worker_id=instance_id)
    poll_seconds = float(settings.get("runtime.notification_poll_seconds", 5.0))
    deliveries = 0
    while not repository.stop_requested("notification_worker", instance_id):
        result = worker.run_once(limit=100)
        deliveries += int(result.get("external_delivered", 0))
        repository.heartbeat_process(
            "notification_worker",
            instance_id,
            detail={"deliveries": deliveries, "last_result": result},
        )
        _wait_for_stop(repository, "notification_worker", instance_id, poll_seconds)
    return {"status": "stopped", "deliveries": deliveries}


def _run_api(
    settings: Settings,
    repository: Round6Repository,
    instance_id: str,
) -> dict[str, Any]:
    import uvicorn

    stop = Event()
    config = uvicorn.Config(
        "quantlab.api.app:app",
        host=str(settings.get("runtime.api_host", "127.0.0.1")),
        port=int(settings.get("runtime.api_port", 8000)),
        log_level=str(settings.get("runtime.api_log_level", "info")),
    )
    server = uvicorn.Server(config)

    def heartbeat() -> None:
        while not stop.wait(5.0):
            if repository.stop_requested("api", instance_id):
                server.should_exit = True
                return
            repository.heartbeat_process(
                "api",
                instance_id,
                detail={"host": config.host, "port": config.port},
            )

    thread = Thread(target=heartbeat, name="quantlab-api-heartbeat", daemon=True)
    thread.start()
    try:
        server.run()
    finally:
        stop.set()
        thread.join(timeout=2.0)
    return {"status": "stopped", "host": config.host, "port": config.port}


def _wait_for_stop(
    repository: Round6Repository,
    component: str,
    instance_id: str,
    seconds: float,
) -> None:
    deadline = time.monotonic() + max(0.05, seconds)
    while time.monotonic() < deadline:
        if repository.stop_requested(component, instance_id):
            return
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def settings_value(settings: Settings, key: str, default: Any) -> Any:
    return settings.get(key, default)


__all__ = [
    "RUNTIME_COMPONENTS",
    "RuntimeServiceController",
    "run_runtime_component",
]
