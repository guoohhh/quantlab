from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from quantlab.data.base import ProviderError


class WestockToolProvider:
    name = "westock-tool"

    def __init__(self, project_root: Path, node_executable: str | None = None):
        self.node = node_executable or os.getenv("QUANTLAB_NODE_EXECUTABLE", "node")
        self.script = (
            project_root / "third-party" / "westock" / "westock-tool" / "scripts" / "index.js"
        )

    def filter(
        self, expression: str, limit: int = 50, orderby: str | None = None, ascending=False
    ) -> list[dict]:
        args = [self.node, str(self.script), "filter", expression, "--limit", str(limit), "--raw"]
        if orderby:
            args.extend(["--orderby", orderby, "--asc" if ascending else "--desc"])
        process = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", timeout=90, check=False
        )
        output = process.stdout.strip()
        if process.returncode != 0:
            raise ProviderError(process.stderr.strip() or output or "westock-tool failed")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ProviderError("westock-tool did not return JSON") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ProviderError(payload.get("error", {}).get("message", "westock-tool failed"))
        if not isinstance(payload, list):
            raise ProviderError("westock-tool returned an unexpected payload")
        return payload
