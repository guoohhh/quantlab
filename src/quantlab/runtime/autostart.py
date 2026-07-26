from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from quantlab.config import Settings


DEFAULT_TASK_NAME = "QuantLab Runtime"


class RuntimeAutostartManager:
    """Manage an explicit per-user Windows Task Scheduler entry.

    The generated launcher contains only fixed executable/config/work-directory paths. API keys
    remain in the environment or dotenv file and are never embedded in task arguments.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        config_path: Path | None = None,
        task_name: str = DEFAULT_TASK_NAME,
    ):
        self.settings = settings
        self.config_path = config_path.resolve() if config_path else None
        self.task_name = task_name
        self.runtime_dir = settings.resolve(
            settings.get("runtime.autostart_directory", "data/runtime")
        )
        self.launcher_path = self.runtime_dir / "quantlab-autostart.ps1"
        appdata = Path(os.environ.get("APPDATA", str(Path.home())))
        self.startup_dir = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        self.startup_path = self.startup_dir / f"{self.task_name}.cmd"
        self.startup_disabled_path = self.startup_dir / f"{self.task_name}.cmd.disabled"

    def install(self) -> dict[str, Any]:
        self._require_windows()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.launcher_path.write_text(self._launcher_script(), encoding="utf-8")
        action = (
            f'powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden '
            f'-ExecutionPolicy Bypass -File "{self.launcher_path}"'
        )
        result = _run_schtasks(
            [
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/TN",
                self.task_name,
                "/TR",
                action,
            ],
            check=False,
        )
        mechanism = "task_scheduler"
        if result["returncode"] != 0:
            self._install_startup_fallback()
            mechanism = "user_startup_folder"
        return {
            "status": "installed" if result["returncode"] == 0 or self.startup_path.is_file() else "failed",
            "task_name": self.task_name,
            "launcher": str(self.launcher_path),
            "enabled": True,
            "hidden_runtime": True,
            "secrets_in_command": False,
            "mechanism": mechanism,
            "fallback_used": mechanism == "user_startup_folder",
            "scheduler_output": result,
        }

    def status(self) -> dict[str, Any]:
        self._require_windows()
        result = _run_schtasks(
            ["/Query", "/TN", self.task_name, "/FO", "LIST", "/V"],
            check=False,
        )
        installed = result["returncode"] == 0
        if not installed and self.startup_path.is_file():
            return {
                "status": "installed",
                "task_name": self.task_name,
                "enabled": True,
                "launcher": str(self.launcher_path),
                "launcher_exists": self.launcher_path.is_file(),
                "mechanism": "user_startup_folder",
                "fallback_used": True,
                "scheduler_output": result,
            }
        text = f"{result['stdout']}\n{result['stderr']}".lower()
        disabled = "disabled" in text or "已禁用" in text
        return {
            "status": "installed" if installed else "not_installed",
            "task_name": self.task_name,
            "enabled": bool(installed and not disabled),
            "launcher": str(self.launcher_path),
            "launcher_exists": self.launcher_path.is_file(),
            "mechanism": "task_scheduler",
            "fallback_used": False,
            "scheduler_output": result,
        }

    def disable(self) -> dict[str, Any]:
        self._require_windows()
        result = _run_schtasks(
            ["/Change", "/TN", self.task_name, "/Disable"],
            check=False,
        )
        fallback_disabled = False
        if self.startup_path.is_file():
            self.startup_path.replace(self.startup_disabled_path)
            fallback_disabled = True
        return {
            "status": "disabled",
            "task_name": self.task_name,
            "enabled": False,
            "mechanism": "task_scheduler" if result["returncode"] == 0 else "user_startup_folder",
            "fallback_used": fallback_disabled,
            "scheduler_output": result,
        }

    def remove(self) -> dict[str, Any]:
        self._require_windows()
        result = _run_schtasks(
            ["/Delete", "/F", "/TN", self.task_name],
            check=False,
        )
        if result["returncode"] not in {0, 1}:
            raise RuntimeError(result["stderr"] or "failed to remove Windows scheduled task")
        if self.launcher_path.is_file():
            self.launcher_path.unlink()
        if self.startup_path.is_file():
            self.startup_path.unlink()
        if self.startup_disabled_path.is_file():
            self.startup_disabled_path.unlink()
        return {
            "status": "removed",
            "task_name": self.task_name,
            "enabled": False,
            "mechanism": "task_scheduler" if result["returncode"] == 0 else "user_startup_folder",
            "scheduler_output": result,
        }

    def _install_startup_fallback(self) -> None:
        self.startup_dir.mkdir(parents=True, exist_ok=True)
        if self.startup_disabled_path.is_file():
            self.startup_disabled_path.unlink()
        command = (
            "@echo off\n"
            f'powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden '
            f'-ExecutionPolicy Bypass -File "{self.launcher_path}"\n'
        )
        self.startup_path.write_text(command, encoding="utf-8")

    def _launcher_script(self) -> str:
        python = Path(sys.executable).resolve()
        pythonw = python.with_name("pythonw.exe")
        executable = pythonw if pythonw.is_file() else python
        args = ["-m", "quantlab.cli", "runtime-start"]
        if self.config_path is not None:
            args.extend(["--config", str(self.config_path)])
        quoted_args = " ".join(_ps_quote(item) for item in args)
        return (
            "$ErrorActionPreference = 'Stop'\n"
            f"Set-Location -LiteralPath {_ps_quote(str(self.settings.root.resolve()))}\n"
            f"& {_ps_quote(str(executable))} {quoted_args}\n"
            "exit $LASTEXITCODE\n"
        )

    @staticmethod
    def _require_windows() -> None:
        if os.name != "nt":
            raise RuntimeError("Windows Task Scheduler autostart is available only on Windows")


def _run_schtasks(arguments: list[str], *, check: bool = True) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603 - fixed Windows system executable
        ["schtasks.exe", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    result = {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }
    if check and completed.returncode != 0:
        raise RuntimeError(result["stderr"] or result["stdout"] or "schtasks failed")
    return result


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


__all__ = ["DEFAULT_TASK_NAME", "RuntimeAutostartManager"]
