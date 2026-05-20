from __future__ import annotations

import hashlib
import html
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .archive import Article
from .storage import write_json_atomic


MP_BASE = "https://mp.weixin.qq.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class MpBackendError(RuntimeError):
    pass


@dataclass
class MpSession:
    token: str
    cookie: str
    login_at: int
    fakeid: str = ""

    @classmethod
    def load(cls, path: Path) -> "MpSession":
        if not path.exists():
            raise MpBackendError(
                f"Session not found at {path}. Run `python3 -m wedownloader mp-login` first."
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MpBackendError(f"Invalid session file: {path}") from exc

        token = str(data.get("token") or "")
        cookie = str(data.get("cookie") or "")
        login_at = int(data.get("login_at") or 0)
        fakeid = str(data.get("fakeid") or "")
        if not token or not cookie:
            raise MpBackendError("Session is missing token or cookie. Run mp-login again.")
        return cls(token=token, cookie=cookie, login_at=login_at, fakeid=fakeid)

    def save(self, path: Path) -> None:
        write_json_atomic(
            path,
            {
                "token": self.token,
                "cookie": self.cookie,
                "login_at": self.login_at,
                "fakeid": self.fakeid,
            },
            private=True,
        )

    def age_seconds(self) -> int:
        return int(time.time()) - self.login_at


def save_session_interactively(path: Path) -> MpSession:
    print("请先在浏览器登录 https://mp.weixin.qq.com/ ，进入公众号后台首页。")
    print("然后复制当前后台 URL 里的 token，以及浏览器请求里的 Cookie。")
    token_input = input("token 或包含 token=... 的后台 URL: ").strip()
    token = extract_token(token_input)
    cookie = input("Cookie: ").strip()
    fakeid = input("可选：自己的公众号 fakeid（不知道可直接回车）: ").strip()
    if not token:
        raise MpBackendError("token missing: 请复制后台 URL 里的 token 参数。")
    if not cookie:
        raise MpBackendError("cookie missing: 请从浏览器开发者工具复制 Cookie。")
    session = MpSession(token=token, cookie=cookie, login_at=int(time.time()), fakeid=fakeid)
    session.save(path)
    return session


def save_session_from_file(input_path: Path, output_path: Path) -> MpSession:
    values = parse_login_file(input_path)
    token = extract_token(values.get("token", "") or values.get("url", ""))
    cookie = values.get("cookie", "").strip()
    fakeid = values.get("fakeid", "").strip()
    if not token:
        raise MpBackendError("token missing: 请在文件里写 TOKEN=... 或 URL=...。")
    if not cookie:
        raise MpBackendError("cookie missing: 请在文件里写 COOKIE=...。")
    session = MpSession(token=token, cookie=cookie, login_at=int(time.time()), fakeid=fakeid)
    session.save(output_path)
    return session


def parse_login_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise MpBackendError(f"Login file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise MpBackendError(f"Login file is empty: {path}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return {str(key).lower(): str(value) for key, value in data.items()}

    values: Dict[str, str] = {}
    positional: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            normalized_key = key.strip().lower()
            if normalized_key in ("token", "url", "cookie", "fakeid"):
                values[normalized_key] = value.strip().strip('"').strip("'")
        else:
            positional.append(line)

    if positional:
        values.setdefault("token", positional[0])
    if len(positional) >= 2:
        values.setdefault("cookie", positional[1])
    return values


def extract_token(value: str) -> str:
    if not value:
        return ""
    match = re.search(r"(?:[?&])token=(\d+)", value)
    if match:
        return match.group(1)
    if value.isdigit():
        return value
    return ""


class MpBackendClient:
    def __init__(self, session: MpSession):
        self.session = session

    def check_status(self) -> Dict[str, Any]:
        params = {
            "token": self.session.token,
            "lang": "zh_CN",
            "f": "json",
            "ajax": "1",
        }
        return self._request_json("/cgi-bin/home", params)

    def iter_published_articles(self, limit: Optional[int] = None) -> Iterator[Article]:
        for raw in self.iter_raw_published(limit=limit):
            yield self.article_from_raw(raw)

    def iter_raw_published(self, limit: Optional[int] = None) -> Iterator[Dict[str, Any]]:
        emitted = 0
        begin = 0
        count = 10
        while True:
            data = self.list_page(begin=begin, count=count)
            items = parse_article_list(data)
            if not items:
                break
            for raw in items:
                yield raw
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
            page_entries = count_article_list_entries(data)
            begin += count
            total = int(data.get("app_msg_cnt") or data.get("total_count") or 0)
            if page_entries < count or (total and begin >= total):
                break

    def list_page(self, begin: int, count: int = 20) -> Dict[str, Any]:
        errors: List[str] = []

        publish_params: Dict[str, Any] = {
            "sub": "list",
            "begin": begin,
            "count": count,
            "token": self.session.token,
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        try:
            data = self._request_json("/cgi-bin/appmsgpublish", publish_params)
            if parse_article_list(data):
                return data
            if begin > 0:
                return data
            errors.append("browser publish endpoint returned no articles")
        except MpBackendError as exc:
            errors.append(str(exc))

        legacy_publish_params: Dict[str, Any] = {
            "sub": "list",
            "search_field": "null",
            "begin": begin,
            "count": count,
            "query": "",
            "type": "101_1",
            "free_publish_type": 1,
            "sub_action": "list_ex",
            "token": self.session.token,
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        if self.session.fakeid:
            legacy_publish_params["fakeid"] = self.session.fakeid
        try:
            data = self._request_json("/cgi-bin/appmsgpublish", legacy_publish_params)
            if parse_article_list(data):
                return data
            if begin > 0:
                return data
            errors.append("legacy publish endpoint returned no articles")
        except MpBackendError as exc:
            errors.append(str(exc))

        appmsg_params: Dict[str, Any] = {
            "action": "list_ex",
            "begin": begin,
            "count": count,
            "type": 9,
            "query": "",
            "token": self.session.token,
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        if self.session.fakeid:
            appmsg_params["fakeid"] = self.session.fakeid
        try:
            data = self._request_json("/cgi-bin/appmsg", appmsg_params)
            if parse_article_list(data):
                return data
            return data
        except MpBackendError as exc:
            errors.append(str(exc))

        raise MpBackendError("; ".join(errors) or "No backend article list endpoint worked.")

    def article_from_raw(self, raw: Dict[str, Any]) -> Article:
        url = str(raw.get("link") or raw.get("url") or raw.get("content_url") or "")
        content = str(raw.get("content") or "")
        title = html.unescape(str(raw.get("title") or ""))
        author = html.unescape(str(raw.get("author") or raw.get("author_name") or ""))
        digest = html.unescape(str(raw.get("digest") or ""))
        update_time = backend_article_time(raw)

        if url and not content:
            page = self.fetch_article_page(url)
            parsed = parse_article_html(page)
            content = parsed.get("content") or page
            title = title or parsed.get("title", "")
            author = author or parsed.get("author", "")
            update_time = update_time or int(parsed.get("create_time") or 0)

        source_id = source_id_from_raw(raw) or source_id_from_url(url) or hashlib.sha256(
            json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        index = article_index_from_raw(raw)
        return Article(
            source="mp_published",
            source_id=source_id,
            index=index,
            title=title,
            author=author,
            digest=digest,
            content=content,
            url=url,
            update_time=update_time,
            raw={"mp_backend": raw},
        )

    def fetch_article_page(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": self.session.cookie,
                "Referer": "https://mp.weixin.qq.com/",
            },
        )
        try:
            return fetch_with_retries(request).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise MpBackendError(classify_http_error(exc.code)) from exc
        except urllib.error.URLError as exc:
            raise MpBackendError(f"backend article request failed: {exc.reason}") from exc
        except http.client.IncompleteRead as exc:
            raise MpBackendError(f"backend article request incomplete: {exc}") from exc

    def _request_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{MP_BASE}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": self.session.cookie,
                "Referer": "https://mp.weixin.qq.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )
        try:
            raw = fetch_with_retries(request).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise MpBackendError(classify_http_error(exc.code)) from exc
        except urllib.error.URLError as exc:
            raise MpBackendError(f"backend request failed: {exc.reason}") from exc
        except http.client.IncompleteRead as exc:
            raise MpBackendError(f"backend request incomplete: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            if "login" in raw.lower():
                raise MpBackendError("not logged in: 后台返回登录页，请重新运行 mp-login。") from exc
            raise MpBackendError("backend response was not JSON.") from exc

        base_resp = data.get("base_resp") or {}
        ret = base_resp.get("ret", data.get("ret", 0))
        if ret not in (0, "0", None):
            err_msg = str(base_resp.get("err_msg") or data.get("errmsg") or data.get("msg") or "")
            raise MpBackendError(classify_backend_error(ret, err_msg))
        return data


def fetch_with_retries(request: urllib.request.Request, attempts: int = 3) -> bytes:
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, http.client.IncompleteRead) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(1 + attempt)
    if last_error:
        raise last_error
    return b""


def parse_article_list(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(data.get("app_msg_list"), list):
        return [item for item in data["app_msg_list"] if isinstance(item, dict)]

    publish_page = data.get("publish_page")
    if isinstance(publish_page, str) and publish_page:
        try:
            page_data = json.loads(publish_page)
        except json.JSONDecodeError:
            page_data = {}
        publish_list = page_data.get("publish_list")
        if isinstance(publish_list, list):
            return flatten_publish_list(publish_list)

    publish_list = data.get("publish_list")
    if isinstance(publish_list, list):
        return flatten_publish_list(publish_list)

    return []


def count_article_list_entries(data: Dict[str, Any]) -> int:
    """Return the backend page entry count before multi-article messages are flattened."""
    if isinstance(data.get("app_msg_list"), list):
        return len(data["app_msg_list"])

    publish_page = data.get("publish_page")
    if isinstance(publish_page, str) and publish_page:
        try:
            page_data = json.loads(publish_page)
        except json.JSONDecodeError:
            page_data = {}
        publish_list = page_data.get("publish_list")
        if isinstance(publish_list, list):
            return len(publish_list)

    publish_list = data.get("publish_list")
    if isinstance(publish_list, list):
        return len(publish_list)

    return 0


def flatten_publish_list(publish_list: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    articles: List[Dict[str, Any]] = []
    for publish in publish_list:
        publish_info = publish.get("publish_info")
        if isinstance(publish_info, str):
            try:
                publish_info = json.loads(publish_info)
            except json.JSONDecodeError:
                publish_info = {}
        if not isinstance(publish_info, dict):
            publish_info = publish
        appmsgex = publish_info.get("appmsgex") or publish_info.get("appmsg_info") or []
        if isinstance(appmsgex, dict):
            appmsgex = [appmsgex]
        for item in appmsgex:
            if isinstance(item, dict):
                merged = dict(item)
                for key in ("publish_time", "create_time", "update_time"):
                    if key not in merged and key in publish_info:
                        merged[key] = publish_info[key]
                articles.append(merged)
    return articles


def parse_article_html(page: str) -> Dict[str, Any]:
    return {
        "title": extract_text_by_id(page, "activity-name") or extract_title(page),
        "author": extract_text_by_id(page, "js_name"),
        "content": extract_inner_html_by_id(page, "js_content"),
        "create_time": extract_create_time(page),
    }


def extract_text_by_id(page: str, element_id: str) -> str:
    inner = extract_inner_html_by_id(page, element_id)
    if not inner:
        return ""
    text = re.sub(r"<[^>]+>", "", inner)
    return html.unescape(text).strip()


def extract_inner_html_by_id(page: str, element_id: str) -> str:
    pattern = re.compile(
        rf"""<(?P<tag>[a-zA-Z0-9]+)[^>]*\bid=["']{re.escape(element_id)}["'][^>]*>(?P<body>.*?)</(?P=tag)>""",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(page)
    return match.group("body").strip() if match else ""


def extract_title(page: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()


def extract_create_time(page: str) -> int:
    match = re.search(r"var\s+createTime\s*=\s*['\"]?(\d{10})", page)
    return int(match.group(1)) if match else 0


def backend_article_time(raw: Dict[str, Any]) -> int:
    for value in (
        raw.get("create_time"),
        raw.get("update_time"),
        raw.get("publish_time"),
        nested_int(raw, "line_info", "send_time"),
        nested_int(raw, "private_info", "update_time"),
    ):
        if value:
            return int(value)
    return 0


def article_index_from_raw(raw: Dict[str, Any]) -> int:
    for key in ("idx", "itemidx"):
        value = raw.get(key)
        if value in (None, ""):
            continue
        index = int(value)
        return max(index - 1, 0) if key == "itemidx" else index
    return 0


def nested_int(raw: Dict[str, Any], parent: str, key: str) -> int:
    value = raw.get(parent)
    if not isinstance(value, dict):
        return 0
    try:
        return int(value.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def source_id_from_raw(raw: Dict[str, Any]) -> str:
    for key in ("appmsgid", "aid", "appmsg_id"):
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def source_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    mid = (query.get("mid") or [""])[0]
    idx = (query.get("idx") or [""])[0]
    biz = (query.get("__biz") or [""])[0]
    if mid or idx or biz:
        return "_".join(part for part in (biz, mid, idx) if part)
    if url:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return ""


def classify_backend_error(ret: Any, err_msg: str) -> str:
    text = f"{ret} {err_msg}".lower()
    if "login" in text or "timeout" in text or str(ret) in ("200003", "200002"):
        return "not logged in: 后台 session 失效，请重新运行 mp-login。"
    if "token" in text:
        return "token missing or expired: 请重新复制后台 URL token 并运行 mp-login。"
    if "freq" in text or "rate" in text or "too many" in text:
        return "rate limited: 微信后台频控，请稍后再试。"
    if "privilege" in text or "permission" in text or "forbid" in text:
        return "permission denied: 当前登录账号可能不是该公众号管理员/运营者。"
    return f"backend error {ret}: {err_msg}"


def classify_http_error(code: int) -> str:
    if code in (401, 403):
        return "permission denied or not logged in: 后台拒绝访问，请重新运行 mp-login。"
    if code == 429:
        return "rate limited: 微信后台频控，请稍后再试。"
    return f"backend HTTP {code}"
