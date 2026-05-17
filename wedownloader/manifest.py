from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {"articles": {}, "assets": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)
            self.data.setdefault("articles", {})
            self.data.setdefault("assets", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_article(self, key: str, metadata: Dict[str, Any]) -> None:
        self.data["articles"][key] = metadata

    def record_asset(self, url: str, metadata: Dict[str, Any]) -> None:
        self.data["assets"][url] = metadata

