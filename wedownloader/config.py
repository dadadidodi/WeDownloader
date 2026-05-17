from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    archive_dir: Path
    token_cache_path: Path


def load_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_settings(env_path: Path, archive_dir: Path) -> Settings:
    file_env = load_dotenv(env_path)
    app_id = os.environ.get("WECHAT_APP_ID") or file_env.get("WECHAT_APP_ID", "")
    app_secret = os.environ.get("WECHAT_APP_SECRET") or file_env.get("WECHAT_APP_SECRET", "")

    if not app_id or not app_secret:
        raise ValueError(
            "Missing WECHAT_APP_ID or WECHAT_APP_SECRET. Put them in .env or export them."
        )

    archive_dir = archive_dir.expanduser().resolve()
    return Settings(
        app_id=app_id,
        app_secret=app_secret,
        archive_dir=archive_dir,
        token_cache_path=archive_dir / ".access_token.json",
    )

