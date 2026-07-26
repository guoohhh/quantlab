from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


KEYWORDS = (
    "user_paper",
    "chat",
    "notification",
    "job",
    "decision_task",
    "shadow",
    "forward",
    "experiment",
    "provider",
    "research",
    "thesis",
    "reflection",
    "portfolio",
)


def main() -> None:
    database = Path(sys.argv[1]).resolve()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if any(keyword in row[0] for keyword in KEYWORDS)
        ]
        counts = {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in names
        }
        print(json.dumps(counts, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
