from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from quantlab.data.base import ProviderError


class WestockDataProvider:
    """Thin JSON adapter for the bundled westock-data research CLI."""

    name = "westock-data"

    def __init__(self, project_root: Path, node_executable: str | None = None):
        self.node = node_executable or os.getenv("QUANTLAB_NODE_EXECUTABLE", "node")
        self.script = (
            project_root / "third-party" / "westock" / "westock-data" / "scripts" / "index.js"
        )

    def run(self, *args: str, timeout: int = 45) -> Any:
        command = [self.node, str(self.script), *args]
        if "--raw" not in command:
            command.append("--raw")
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
        output = process.stdout.strip()
        if process.returncode != 0:
            raise ProviderError(process.stderr.strip() or output or "westock-data failed")
        if not output:
            return None
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ProviderError("westock-data did not return JSON") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ProviderError(str(payload.get("error") or "westock-data failed"))
        return payload

    def stock_risk(self, symbol: str, risk_type: str) -> Any:
        return self.run("risk", symbol, "--types", risk_type)

    def search_stocks(self, keyword: str, limit: int = 20) -> Any:
        return self.run(
            "search",
            keyword,
            "--type",
            "stock",
            "--limit",
            str(limit),
        )

    def bond_detail(self, symbol: str) -> dict[str, Any]:
        payload = self.run("bond", "detail", symbol)
        if not isinstance(payload, list):
            return {}
        output: dict[str, Any] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            key = row.get("项目") or row.get("item")
            if key:
                output[str(key)] = row.get("内容") if "内容" in row else row.get("value")
        return output
