from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .html_assets import AssetDownloader, AssetResult
from .manifest import Manifest
from .progress import ProgressTracker
from .wechat import WeChatApiError


@dataclass
class Article:
    source: str
    source_id: str
    index: int
    title: str
    author: str
    digest: str
    content: str
    url: str
    update_time: int
    raw: Dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}:{self.index}"


class Archiver:
    def __init__(self, client: Any, archive_dir: Path):
        self.client = client
        self.archive_dir = archive_dir
        self.manifest = Manifest(archive_dir / "manifest.json")
        self.asset_downloader = AssetDownloader()
        self.progress = ProgressTracker(archive_dir / "progress.json")

    def dry_run(self, include_published: bool, include_drafts: bool, limit: Optional[int]) -> int:
        count = 0
        for article in self.iter_articles(include_published, include_drafts, limit):
            count += 1
            date = format_ts(article.update_time)
            print(f"[{article.source}] {date} {article.title}")
        print(f"Total: {count}")
        return count

    def run(
        self,
        include_published: bool,
        include_drafts: bool,
        limit: Optional[int],
        download_image_materials: bool = False,
        show_progress: bool = False,
        workers: int = 3,
    ) -> int:
        validate_workers(workers)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.progress = ProgressTracker(self.archive_dir / "progress.json", enabled=show_progress)
        self.progress.start_run(
            {
                "include_published": include_published,
                "include_drafts": include_drafts,
                "limit": limit,
                "download_image_materials": download_image_materials,
                "workers": workers,
            }
        )

        try:
            discovered_articles = list(self.iter_articles(include_published, include_drafts, limit))
            self.progress.set_articles_discovered(len(discovered_articles))
            self.progress.set_phase("articles")
            count, articles = self.archive_articles(
                discovered_articles,
                show_progress=show_progress,
                workers=workers,
            )

            self.progress.set_phase("index")
            self.write_index(articles)
            if download_image_materials:
                self.progress.set_phase("materials")
                self.download_materials("image", show_progress=show_progress)
            self.manifest.save()
            self.progress.complete()
            return count
        except Exception as exc:
            self.progress.fail_run(str(exc))
            raise

    def iter_articles(
        self, include_published: bool, include_drafts: bool, limit: Optional[int]
    ) -> Iterable[Article]:
        emitted = 0
        sources = []
        if include_published:
            sources.append(("published", self.client.iter_freepublish))
        if include_drafts:
            sources.append(("draft", self.client.iter_drafts))

        for source, item_factory in sources:
            try:
                for item in item_factory():
                    for article in extract_articles(source, item):
                        yield article
                        emitted += 1
                        if limit is not None and emitted >= limit:
                            return
            except WeChatApiError as exc:
                if source == "published" and is_api_unauthorized(exc):
                    print(
                        "Warning: 官方已发布文章接口无权限，已跳过 published；"
                        "请使用 mp-login/mp-download 后台登录态模式导出已发布文章。"
                    )
                    continue
                raise

    def archive_articles(
        self,
        articles: List[Article],
        show_progress: bool,
        workers: int,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        if workers == 1:
            return self._archive_articles_serial(articles, show_progress)
        return self._archive_articles_parallel(articles, show_progress, workers)

    def _archive_articles_serial(
        self, articles: List[Article], show_progress: bool
    ) -> Tuple[int, List[Dict[str, Any]]]:
        records: List[Dict[str, Any]] = []
        count = 0
        for article in articles:
            count += 1
            self.progress.start_item("article", article.title, article.key)
            try:
                record = self.write_article(article)
            except Exception as exc:
                self.progress.fail_item("article", article.title, article.key, str(exc))
                print(f"Failed [{article.source}] {article.title}: {exc}")
                continue
            records.append(record)
            self.record_article_result(article, record)
            if not show_progress:
                print(f"Saved [{article.source}] {article.title}")
        return count, records

    def _archive_articles_parallel(
        self, articles: List[Article], show_progress: bool, workers: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        records: List[Dict[str, Any]] = []
        count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for article in articles:
                self.progress.start_item("article", article.title, article.key)
                future = executor.submit(self.write_article, article, AssetDownloader())
                futures[future] = article

            for future in as_completed(futures):
                article = futures[future]
                count += 1
                try:
                    record = future.result()
                except Exception as exc:
                    self.progress.fail_item("article", article.title, article.key, str(exc))
                    print(f"Failed [{article.source}] {article.title}: {exc}")
                    continue
                records.append(record)
                self.record_article_result(article, record)
                if not show_progress:
                    print(f"Saved [{article.source}] {article.title}")
        return count, records

    def record_article_result(self, article: Article, record: Dict[str, Any]) -> None:
        self.remove_duplicate_articles(article.key, record)
        self.manifest.record_article(article.key, record)
        for asset in record.get("assets", []):
            url = asset.get("url")
            if url:
                self.manifest.record_asset(url, asset)
        self.manifest.save()
        self.progress.record_assets(record.get("assets", []))
        self.progress.finish_item("article", article.title, article.key)

    def remove_duplicate_articles(self, key: str, record: Dict[str, Any]) -> None:
        identity = article_record_identity(record)
        if not identity:
            return
        articles = self.manifest.data.get("articles", {})
        for existing_key, existing in list(articles.items()):
            if existing_key == key or not isinstance(existing, dict):
                continue
            if article_record_identity(existing) == identity or is_low_info_same_title(existing, record):
                del articles[existing_key]

    def write_article(
        self, article: Article, asset_downloader: Optional[AssetDownloader] = None
    ) -> Dict[str, Any]:
        article_dir = (
            self.archive_dir
            / "articles"
            / sanitize_filename(article.source)
            / sanitize_filename(article.source_id or "unknown")
            / str(article.index)
        )
        assets_dir = article_dir / "assets"
        downloader = asset_downloader or self.asset_downloader
        localized_content, assets = downloader.localize_html(
            article.content, assets_dir
        )
        document = render_article_html(article, localized_content)

        article_dir.mkdir(parents=True, exist_ok=True)
        (article_dir / "index.html").write_text(document, encoding="utf-8")

        metadata = article_metadata(article, assets, article_dir, self.archive_dir)
        (article_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return metadata

    def write_index(self, articles: List[Dict[str, Any]]) -> None:
        known = list(self.manifest.data.get("articles", {}).values())
        by_path = {item.get("path"): item for item in known if item.get("path")}
        for item in articles:
            by_path[item["path"]] = item
        rows = sorted(
            dedupe_article_records(by_path.values()),
            key=lambda item: item.get("update_time", 0),
            reverse=True,
        )

        body = "\n".join(
            "<tr>"
            f"<td>{html.escape(item.get('source', ''))}</td>"
            f"<td><a href=\"{html.escape(item.get('path', ''))}\">{html.escape(item.get('title') or '(untitled)')}</a></td>"
            f"<td>{html.escape(item.get('author', ''))}</td>"
            f"<td>{html.escape(format_ts(int(item.get('update_time') or 0)))}</td>"
            "</tr>"
            for item in rows
        )
        index = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>微信公众号归档</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 40px; color: #1f2328; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #d0d7de; padding: 10px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f6f8fa; }}
    a {{ color: #0969da; }}
  </style>
</head>
<body>
  <h1>微信公众号归档</h1>
  <p>共 {len(rows)} 篇文章。</p>
  <table>
    <thead><tr><th>来源</th><th>标题</th><th>作者</th><th>时间</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</body>
</html>
"""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        (self.archive_dir / "index.html").write_text(index, encoding="utf-8")
        self.write_readable_manifest(rows)

    def write_readable_manifest(self, rows: List[Dict[str, Any]]) -> None:
        readable = []
        for item in rows:
            assets = item.get("assets") or []
            readable.append(
                {
                    "标题": item.get("title") or "",
                    "作者": item.get("author") or "",
                    "来源": item.get("source") or "",
                    "时间": format_ts(int(item.get("update_time") or 0)),
                    "原文链接": item.get("url") or "",
                    "本地文件": item.get("path") or "",
                    "元数据文件": item.get("metadata_path") or "",
                    "资源总数": len(assets),
                    "资源下载成功": count_assets(assets, "downloaded"),
                    "资源下载失败": count_assets(assets, "failed"),
                    "资源需手动处理": count_assets(assets, "needs_manual_fetch"),
                }
            )
        (self.archive_dir / "articles_readable.json").write_text(
            json.dumps(readable, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def download_materials(self, material_type: str, show_progress: bool = False) -> int:
        output_dir = self.archive_dir / "materials" / sanitize_filename(material_type)
        count = 0
        for item in self.client.iter_materials(material_type):
            url = str(item.get("url") or "")
            if not url:
                continue
            title = str(item.get("name") or item.get("media_id") or url)
            key = f"material:{material_type}:{item.get('media_id') or url}"
            self.progress.start_item("material", title, key)
            result = self.asset_downloader.download_asset(url, output_dir)
            record = {
                "type": material_type,
                "name": item.get("name") or "",
                "media_id": item.get("media_id") or "",
                "update_time": item.get("update_time") or 0,
                "url": url,
                "status": result.status,
                "local_path": (
                    str((output_dir / result.local_path).relative_to(self.archive_dir))
                    if result.local_path
                    else ""
                ),
                "error": result.error,
            }
            self.manifest.data.setdefault("materials", []).append(record)
            self.manifest.record_asset(url, result.__dict__)
            self.progress.record_assets([result.__dict__])
            count += 1
            if result.status == "downloaded":
                self.progress.finish_item("material", title, key)
                if not show_progress:
                    print(f"Saved material [{material_type}] {record['name'] or record['media_id']}")
            else:
                error = result.error or result.status
                self.progress.fail_item("material", title, key, error)
                print(f"Failed material [{material_type}] {record['name'] or record['media_id']}: {error}")
        return count


def extract_articles(source: str, item: Dict[str, Any]) -> Iterable[Article]:
    source_id = str(item.get("publish_id") or item.get("media_id") or item.get("article_id") or "")
    update_time = int(item.get("update_time") or item.get("create_time") or 0)
    content_root = item.get("content") or {}
    news_items = content_root.get("news_item") or item.get("news_item") or []

    for index, news in enumerate(news_items):
        yield Article(
            source=source,
            source_id=source_id,
            index=index,
            title=str(news.get("title") or ""),
            author=str(news.get("author") or ""),
            digest=str(news.get("digest") or ""),
            content=str(news.get("content") or ""),
            url=str(news.get("url") or news.get("content_source_url") or ""),
            update_time=int(news.get("update_time") or update_time),
            raw={"item": item, "news": news},
        )


def render_article_html(article: Article, content: str) -> str:
    title = html.escape(article.title or "(untitled)")
    author = html.escape(article.author)
    digest = html.escape(article.digest)
    source_url = html.escape(article.url)
    date = html.escape(format_ts(article.update_time))
    source_link = f'<p><a href="{source_url}">原文链接</a></p>' if source_url else ""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ max-width: 760px; margin: 40px auto; padding: 0 20px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.7; color: #1f2328; }}
    img, video {{ max-width: 100%; height: auto; }}
    .meta {{ color: #57606a; font-size: 14px; }}
    .digest {{ color: #57606a; border-left: 3px solid #d0d7de; padding-left: 12px; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p class="meta">{author} {date}</p>
  {f'<p class="digest">{digest}</p>' if digest else ''}
  <main>{content}</main>
  {source_link}
</body>
</html>
"""


def article_metadata(
    article: Article, assets: List[AssetResult], article_dir: Path, archive_dir: Path
) -> Dict[str, Any]:
    return {
        "key": article.key,
        "source": article.source,
        "source_id": article.source_id,
        "index": article.index,
        "title": article.title,
        "author": article.author,
        "digest": article.digest,
        "url": article.url,
        "update_time": article.update_time,
        "path": str((article_dir / "index.html").relative_to(archive_dir)),
        "metadata_path": str((article_dir / "metadata.json").relative_to(archive_dir)),
        "assets": [asset.__dict__ for asset in assets],
    }


def sanitize_filename(value: str) -> str:
    value = value.strip() or "untitled"
    value = re.sub(r"[\\/:*?\"<>|\s]+", "_", value)
    return value[:120].strip("._") or "untitled"


def format_ts(value: int) -> str:
    if not value:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def count_assets(assets: List[Dict[str, Any]], status: str) -> int:
    return sum(1 for asset in assets if asset.get("status") == status)


def dedupe_article_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: Dict[str, Dict[str, Any]] = {}
    for record in records:
        identity = article_record_identity(record)
        if not identity:
            identity = str(record.get("path") or record.get("key") or len(deduped))
        existing = deduped.get(identity)
        if existing is None or article_record_score(record) >= article_record_score(existing):
            deduped[identity] = record

    by_title: Dict[str, Dict[str, Any]] = {}
    for record in deduped.values():
        title_key = article_title_identity(record)
        existing = by_title.get(title_key)
        if (
            existing is not None
            and (is_low_info_record(existing) or is_low_info_record(record))
            and article_record_score(record) >= article_record_score(existing)
        ):
            by_title[title_key] = record
        elif existing is None:
            by_title[title_key] = record
    return list(by_title.values())


def article_record_identity(record: Dict[str, Any]) -> str:
    source = str(record.get("source") or "")
    if source != "mp_published":
        return str(record.get("key") or record.get("path") or "")

    mid, idx = mid_idx_from_url(str(record.get("url") or ""))
    if mid:
        return f"{source}:{mid}:{idx}"

    source_id = str(record.get("source_id") or "")
    if source_id.isdigit():
        return f"{source}:{source_id}:{int(record.get('index') or 0)}"

    match = re.search(r"(?:^|_)(\d{6,})(?:_(\d+))?$", source_id)
    if match:
        item_index = int(match.group(2) or int(record.get("index") or 0) + 1) - 1
        return f"{source}:{match.group(1)}:{max(item_index, 0)}"

    title = str(record.get("title") or "")
    update_time = int(record.get("update_time") or 0)
    if title and update_time:
        return f"{source}:title:{title}:{update_time}"
    return ""


def mid_idx_from_url(url: str) -> Tuple[str, int]:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    mid = (query.get("mid") or [""])[0]
    idx_value = (query.get("idx") or ["1"])[0]
    try:
        idx = max(int(idx_value) - 1, 0)
    except ValueError:
        idx = 0
    return mid, idx


def article_record_score(record: Dict[str, Any]) -> int:
    score = 0
    if record.get("url"):
        score += 4
    if record.get("update_time"):
        score += 2
    if record.get("assets"):
        score += 1
    if str(record.get("source_id") or "").isdigit():
        score += 1
    return score


def article_title_identity(record: Dict[str, Any]) -> str:
    return f"{record.get('source') or ''}:{record.get('title') or ''}"


def is_low_info_record(record: Dict[str, Any]) -> bool:
    return not record.get("url") and not record.get("update_time")


def is_low_info_same_title(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if not is_low_info_record(left):
        return False
    return bool(left.get("title")) and article_title_identity(left) == article_title_identity(right)


def validate_workers(workers: int) -> None:
    if workers < 1:
        raise ValueError("--workers must be at least 1.")
    if workers > 5:
        print(
            f"Warning: --workers {workers} may trigger WeChat rate limits; "
            "1-5 is recommended."
        )


def is_api_unauthorized(exc: WeChatApiError) -> bool:
    errcode = exc.response.get("errcode")
    return str(errcode) == "48001" or "48001" in str(exc)
