from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


API_BASE = "https://api.weixin.qq.com"


class WeChatApiError(RuntimeError):
    def __init__(self, message: str, response: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.response = response or {}


@dataclass
class TokenCache:
    access_token: str
    expires_at: float

    @classmethod
    def load(cls, path: Path) -> Optional["TokenCache"]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            token = str(data["access_token"])
            expires_at = float(data["expires_at"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if expires_at <= time.time() + 120:
            return None
        return cls(access_token=token, expires_at=expires_at)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"access_token": self.access_token, "expires_at": self.expires_at},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


class WeChatClient:
    def __init__(self, app_id: str, app_secret: str, token_cache_path: Path):
        self.app_id = app_id
        self.app_secret = app_secret
        self.token_cache_path = token_cache_path
        self._access_token: Optional[str] = None

    def get_access_token(self) -> str:
        if self._access_token:
            return self._access_token

        cache = TokenCache.load(self.token_cache_path)
        if cache:
            self._access_token = cache.access_token
            return cache.access_token

        params = urllib.parse.urlencode(
            {
                "grant_type": "client_credential",
                "appid": self.app_id,
                "secret": self.app_secret,
            }
        )
        data = self._request_json("GET", f"/cgi-bin/token?{params}", auth=False)
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 7200))
        if not token:
            raise WeChatApiError("WeChat token response did not include access_token.", data)

        cache = TokenCache(access_token=token, expires_at=time.time() + expires_in)
        cache.save(self.token_cache_path)
        self._access_token = token
        return token

    def batch_get_freepublish(self, offset: int, count: int = 20) -> Dict[str, Any]:
        return self._post_json(
            "/cgi-bin/freepublish/batchget",
            {"offset": offset, "count": count, "no_content": 0},
        )

    def batch_get_drafts(self, offset: int, count: int = 20) -> Dict[str, Any]:
        return self._post_json(
            "/cgi-bin/draft/batchget",
            {"offset": offset, "count": count, "no_content": 0},
        )

    def batch_get_materials(
        self, material_type: str, offset: int, count: int = 20
    ) -> Dict[str, Any]:
        return self._post_json(
            "/cgi-bin/material/batchget_material",
            {"type": material_type, "offset": offset, "count": count},
        )

    def iter_freepublish(self, page_size: int = 20) -> Iterator[Dict[str, Any]]:
        yield from self._iter_paged(self.batch_get_freepublish, page_size=page_size)

    def iter_drafts(self, page_size: int = 20) -> Iterator[Dict[str, Any]]:
        yield from self._iter_paged(self.batch_get_drafts, page_size=page_size)

    def iter_materials(
        self, material_type: str, page_size: int = 20
    ) -> Iterator[Dict[str, Any]]:
        def fetch(offset: int, count: int) -> Dict[str, Any]:
            return self.batch_get_materials(material_type, offset, count)

        yield from self._iter_paged(fetch, page_size=page_size)

    def _iter_paged(self, fetch, page_size: int) -> Iterator[Dict[str, Any]]:
        offset = 0
        while True:
            data = fetch(offset, page_size)
            items: List[Dict[str, Any]] = data.get("item") or data.get("items") or []
            if not items:
                break
            for item in items:
                yield item
            offset += len(items)
            total_count = int(data.get("total_count") or data.get("total") or 0)
            item_count = int(data.get("item_count") or len(items))
            if item_count < page_size or (total_count and offset >= total_count):
                break

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_json("POST", self._with_token(path), payload=payload)

    def _with_token(self, path: str) -> str:
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}access_token={urllib.parse.quote(self.get_access_token())}"

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        if auth and "access_token=" not in path:
            path = self._with_token(path)

        url = f"{API_BASE}{path}"
        body = None
        headers = {"User-Agent": "WeDownloader/0.1"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WeChatApiError(f"WeChat HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WeChatApiError(f"WeChat request failed: {exc.reason}") from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WeChatApiError("WeChat response was not JSON.") from exc

        errcode = data.get("errcode", 0)
        if errcode not in (0, "0", None):
            errmsg = data.get("errmsg", "unknown error")
            raise WeChatApiError(f"WeChat API error {errcode}: {errmsg}", data)
        return data

