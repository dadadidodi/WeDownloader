from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .storage import write_json_atomic


RECENT_ERROR_LIMIT = 20


class ProgressTracker:
    def __init__(self, path: Path, enabled: bool = False):
        self.path = path
        self.enabled = enabled
        self.data: Dict[str, Any] = {}

    @classmethod
    def load(cls, path: Path) -> "ProgressTracker":
        tracker = cls(path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            if isinstance(data, dict):
                tracker.data = data
        return tracker

    def start_run(self, scope: Dict[str, Any]) -> None:
        now = current_timestamp()
        self.data = {
            "run_id": uuid.uuid4().hex,
            "status": "running",
            "phase": "discovering",
            "started_at": now,
            "ended_at": None,
            "scope": scope,
            "current_item": None,
            "counters": {
                "articles_discovered": 0,
                "items_processed": 0,
                "items_succeeded": 0,
                "items_failed": 0,
                "assets_downloaded": 0,
                "assets_failed": 0,
                "assets_needs_manual_fetch": 0,
            },
            "recent_errors": [],
        }
        self.save()
        self._print("Started run")

    def set_phase(self, phase: str) -> None:
        self.data["phase"] = phase
        self.save()
        self._print(f"Phase: {phase}")

    def set_articles_discovered(self, count: int) -> None:
        self.data.setdefault("counters", {})["articles_discovered"] = count
        self.save()

    def start_item(
        self, item_type: str, title: str, key: str, save: bool = True
    ) -> None:
        self.data["current_item"] = {
            "type": item_type,
            "title": title,
            "key": key,
            "started_at": current_timestamp(),
        }
        if save:
            self.save()
        self._print(f"Processing {item_type}: {title or key}")

    def finish_item(
        self, item_type: str, title: str, key: str, save: bool = True
    ) -> None:
        counters = self.data.setdefault("counters", {})
        counters["items_processed"] = int(counters.get("items_processed", 0)) + 1
        counters["items_succeeded"] = int(counters.get("items_succeeded", 0)) + 1
        self.data["current_item"] = {
            "type": item_type,
            "title": title,
            "key": key,
            "finished_at": current_timestamp(),
            "status": "succeeded",
        }
        if save:
            self.save()
        self._print_summary(prefix="Saved")

    def fail_item(self, item_type: str, title: str, key: str, error: str) -> None:
        counters = self.data.setdefault("counters", {})
        counters["items_processed"] = int(counters.get("items_processed", 0)) + 1
        counters["items_failed"] = int(counters.get("items_failed", 0)) + 1
        self.data["current_item"] = {
            "type": item_type,
            "title": title,
            "key": key,
            "finished_at": current_timestamp(),
            "status": "failed",
        }
        self.add_error(item_type, title, key, error, save=False)
        self.save()
        self._print_summary(prefix="Failed")

    def record_assets(self, assets: List[Dict[str, Any]], save: bool = True) -> None:
        counters = self.data.setdefault("counters", {})
        for asset in assets:
            status = asset.get("status")
            if status == "ignored":
                continue
            if status == "downloaded":
                key = "assets_downloaded"
            elif status == "needs_manual_fetch":
                key = "assets_needs_manual_fetch"
            elif status == "skipped":
                key = "assets_needs_manual_fetch"
            else:
                key = "assets_failed"
            counters[key] = int(counters.get(key, 0)) + 1
        if save:
            self.save()

    def add_error(
        self,
        item_type: str,
        title: str,
        key: str,
        error: str,
        save: bool = True,
    ) -> None:
        errors = self.data.setdefault("recent_errors", [])
        errors.append(
            {
                "time": current_timestamp(),
                "type": item_type,
                "title": title,
                "key": key,
                "error": error,
            }
        )
        del errors[:-RECENT_ERROR_LIMIT]
        if save:
            self.save()

    def complete(self) -> None:
        self.data["status"] = "completed"
        self.data["phase"] = "completed"
        self.data["ended_at"] = current_timestamp()
        self.save()
        self._print_summary(prefix="Completed")

    def fail_run(self, error: str) -> None:
        self.data["status"] = "failed"
        self.data["ended_at"] = current_timestamp()
        self.add_error("run", "", self.data.get("run_id", ""), error, save=False)
        self.save()
        self._print(f"Run failed: {error}")

    def save(self) -> None:
        write_json_atomic(self.path, self.data)

    def summary(self) -> str:
        if not self.data:
            return f"No progress file found at {self.path}"
        return format_status(self.data)

    def _print_summary(self, prefix: str) -> None:
        if not self.enabled:
            return
        counters = self.data.get("counters", {})
        total = int(counters.get("articles_discovered", 0))
        processed = int(counters.get("items_processed", 0))
        succeeded = int(counters.get("items_succeeded", 0))
        failed = int(counters.get("items_failed", 0))
        current = self.data.get("current_item") or {}
        title = current.get("title") or current.get("key") or ""
        total_text = str(total) if total else "?"
        print(
            f"{prefix}: {processed}/{total_text} items, "
            f"ok={succeeded}, failed={failed}, current={title}"
        )

    def _print(self, message: str) -> None:
        if self.enabled:
            print(f"[progress] {message}")


def current_timestamp() -> int:
    return int(time.time())


def format_status(data: Dict[str, Any]) -> str:
    counters = data.get("counters", {})
    scope = data.get("scope", {})
    current = data.get("current_item") or {}
    errors = data.get("recent_errors") or []

    lines = [
        f"Run ID: {data.get('run_id', '')}",
        f"Status: {data.get('status', '')}",
        f"Phase: {data.get('phase', '')}",
        f"Started: {format_ts(data.get('started_at'))}",
        f"Ended: {format_ts(data.get('ended_at'))}",
        "Scope: "
        f"published={scope.get('include_published')}, "
        f"drafts={scope.get('include_drafts')}, "
        f"limit={scope.get('limit')}, "
        f"image_materials={scope.get('download_image_materials')}",
        "Items: "
        f"discovered={counters.get('articles_discovered', 0)}, "
        f"processed={counters.get('items_processed', 0)}, "
        f"succeeded={counters.get('items_succeeded', 0)}, "
        f"failed={counters.get('items_failed', 0)}",
        "Assets: "
        f"downloaded={counters.get('assets_downloaded', 0)}, "
        f"failed={counters.get('assets_failed', 0)}, "
        f"needs_manual_fetch={counters.get('assets_needs_manual_fetch', 0)}",
    ]
    if current:
        lines.append(
            "Current item: "
            f"{current.get('type', '')} {current.get('title') or current.get('key') or ''} "
            f"({current.get('status', 'running')})"
        )
    if errors:
        lines.append("Recent errors:")
        for error in errors[-5:]:
            title = error.get("title") or error.get("key") or ""
            lines.append(f"- [{error.get('type')}] {title}: {error.get('error')}")
    return "\n".join(lines)


def format_ts(value: Optional[int]) -> str:
    if not value:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))
