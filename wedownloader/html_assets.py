from __future__ import annotations

import hashlib
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


URL_ATTR_RE = re.compile(
    r"""(?P<attr>\b(?:src|data-src|href|poster)\s*=\s*)(?P<quote>["'])(?P<url>https?://[^"']+)(?P=quote)""",
    re.IGNORECASE,
)
CSS_URL_RE = re.compile(
    r"""url\((?P<quote>["']?)(?P<url>https?://[^"')]+)(?P=quote)\)""",
    re.IGNORECASE,
)


@dataclass
class AssetResult:
    url: str
    status: str
    local_path: str = ""
    error: str = ""


class AssetDownloader:
    def __init__(self, user_agent: str = "WeDownloader/0.1"):
        self.user_agent = user_agent

    def localize_html(self, html: str, assets_dir: Path) -> Tuple[str, List[AssetResult]]:
        assets_dir.mkdir(parents=True, exist_ok=True)
        results: Dict[str, AssetResult] = {}

        def attr_replace(match: re.Match) -> str:
            attr_name = match.group("attr").split("=", 1)[0].strip().lower()
            url = self._clean_url(match.group("url"))
            if self._should_ignore_url(url):
                return match.group(0)
            if attr_name == "href" and not self._looks_like_asset_url(url):
                return match.group(0)
            result = self._download_once(url, assets_dir, results, local_prefix="assets")
            replacement = result.local_path if result.status == "downloaded" else url
            output_attr = "src=" if attr_name == "data-src" else match.group("attr")
            return f"{output_attr}{match.group('quote')}{replacement}{match.group('quote')}"

        def css_replace(match: re.Match) -> str:
            url = self._clean_url(match.group("url"))
            if self._should_ignore_url(url):
                return match.group(0)
            result = self._download_once(url, assets_dir, results, local_prefix="assets")
            replacement = result.local_path if result.status == "downloaded" else url
            quote = match.group("quote") or ""
            return f"url({quote}{replacement}{quote})"

        html = URL_ATTR_RE.sub(attr_replace, html or "")
        html = CSS_URL_RE.sub(css_replace, html)
        return html, list(results.values())

    def download_asset(self, url: str, assets_dir: Path) -> AssetResult:
        assets_dir.mkdir(parents=True, exist_ok=True)
        cleaned = self._clean_url(url)
        if self._should_ignore_url(cleaned):
            return AssetResult(url=cleaned, status="ignored")
        return self._download_once(cleaned, assets_dir, {}, local_prefix="")

    def _download_once(
        self,
        url: str,
        assets_dir: Path,
        results: Dict[str, AssetResult],
        local_prefix: str,
    ) -> AssetResult:
        if url in results:
            return results[url]

        try:
            data, content_type = self._fetch(url)
            filename = self._filename_for(url, content_type)
            path = assets_dir / filename
            path.write_bytes(data)
            local_path = f"{local_prefix}/{filename}" if local_prefix else filename
            result = AssetResult(url=url, status="downloaded", local_path=local_path)
        except urllib.error.HTTPError as exc:
            status = "needs_manual_fetch" if exc.code in (401, 403) else "failed"
            result = AssetResult(url=url, status=status, error=f"HTTP {exc.code}")
        except Exception as exc:
            result = AssetResult(url=url, status="failed", error=str(exc))

        results[url] = result
        return result

    def _fetch(self, url: str) -> Tuple[bytes, str]:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(), response.headers.get("Content-Type", "")

    def _filename_for(self, url: str, content_type: str) -> str:
        parsed = urllib.parse.urlparse(url)
        suffix = Path(parsed.path).suffix
        if not suffix or len(suffix) > 10:
            suffix = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".bin"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return f"{digest}{suffix}"

    def _clean_url(self, url: str) -> str:
        return url.replace("&amp;", "&")

    def _looks_like_asset_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.lower()
        if parsed.netloc.endswith(("mmbiz.qpic.cn", "mmbiz.qlogo.cn")):
            return True
        if parsed.netloc == "res.wx.qq.com":
            return True
        return path.endswith(
            (
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".svg",
                ".css",
                ".js",
                ".mp3",
                ".mp4",
                ".m4a",
                ".wav",
                ".webm",
            )
        )

    def _should_ignore_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if not host or "_" in host:
            return True
        if host == "__bridge_loaded__":
            return True
        if host == "badjs.weixinbridge.com":
            return True
        if host == "mp.weixin.qq.com" and path.startswith("/mp/jsmonitor"):
            return True
        if "weixinbridge" in host:
            return True
        return False
