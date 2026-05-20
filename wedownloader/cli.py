from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, List, Optional

from .archive import Archiver, SAVE_EVERY_N_ARTICLES, validate_workers
from .config import load_settings
from .html_assets import AssetDownloader
from .mp_backend import (
    MpBackendClient,
    MpBackendError,
    MpSession,
    backend_article_time,
    save_session_from_file,
    save_session_interactively,
)
from .progress import ProgressTracker
from .wechat import WeChatApiError, WeChatClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wedownloader",
        description="Archive your own WeChat Official Account articles via official APIs.",
        epilog=(
            "Backend session commands for your own account: "
            "mp-login, mp-status, mp-list, mp-download."
        ),
    )
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file.")
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=Path("archive"),
        help="Output directory for HTML, assets, and manifest.",
    )
    parser.add_argument("--dry-run", action="store_true", help="List articles without downloading.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum article count to process.")
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Article download worker count. Default: 3. Use 1 for serial mode.",
    )
    parser.add_argument(
        "--published-only",
        action="store_true",
        help="Only archive published records.",
    )
    parser.add_argument("--drafts-only", action="store_true", help="Only archive drafts.")
    parser.add_argument(
        "--download-image-materials",
        action="store_true",
        help="Also download permanent image materials into archive/materials/image.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print live progress while archiving.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the latest run status from archive/progress.json without calling WeChat APIs.",
    )
    parser.add_argument(
        "--clean-runs",
        action="store_true",
        help="Remove archived run outputs while keeping mp_session.json by default.",
    )
    parser.add_argument(
        "--clean-session",
        action="store_true",
        help="Also remove archive/mp_session.json when used with --clean-runs.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0].startswith("mp-"):
        return main_mp(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.published_only and args.drafts_only:
        parser.error("--published-only and --drafts-only cannot be used together.")

    include_published = not args.drafts_only
    include_drafts = not args.published_only

    if args.status:
        progress = ProgressTracker.load(args.archive_dir.expanduser().resolve() / "progress.json")
        print(progress.summary())
        return 0

    if args.clean_runs:
        removed = clean_run_outputs(
            args.archive_dir.expanduser().resolve(),
            remove_session=args.clean_session,
        )
        print("Removed run output:")
        if removed:
            for path in removed:
                print(f"- {path}")
        else:
            print("- nothing to remove")
        return 0

    try:
        settings = load_settings(args.env, args.archive_dir)
        client = WeChatClient(
            settings.app_id,
            settings.app_secret,
            settings.token_cache_path,
        )
        archiver = Archiver(client, settings.archive_dir)
        if args.dry_run:
            count = archiver.dry_run(include_published, include_drafts, args.limit)
        else:
            count = archiver.run(
                include_published,
                include_drafts,
                args.limit,
                download_image_materials=args.download_image_materials,
                show_progress=args.progress,
                workers=args.workers,
            )
    except (ValueError, WeChatApiError) as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Done. Processed {count} article(s).")
    return 0


def build_mp_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"wedownloader {command}")
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=Path("archive"),
        help="Output directory containing mp_session.json, progress.json, and archived HTML.",
    )
    if command in ("mp-list", "mp-download"):
        parser.add_argument("--limit", type=int, default=None, help="Maximum article count.")
    if command == "mp-login":
        parser.add_argument(
            "--from-file",
            type=Path,
            default=None,
            help="Read TOKEN/COOKIE/FAKEID from a local file instead of interactive paste.",
        )
    if command == "mp-list":
        parser.add_argument("--dry-run", action="store_true", help="List only; do not download.")
    if command == "mp-download":
        parser.add_argument("--progress", action="store_true", help="Print live progress.")
        parser.add_argument(
            "--workers",
            type=int,
            default=3,
            help="Article download worker count. Default: 3. Use 1 for serial mode.",
        )
    return parser


def main_mp(argv: list[str]) -> int:
    command = argv[0]
    if command not in ("mp-login", "mp-status", "mp-list", "mp-download"):
        print(f"Error: unknown mp command {command}")
        return 1

    parser = build_mp_parser(command)
    args = parser.parse_args(argv[1:])
    archive_dir = args.archive_dir.expanduser().resolve()
    session_path = archive_dir / "mp_session.json"

    try:
        if command == "mp-login":
            if args.from_file:
                session = save_session_from_file(args.from_file.expanduser(), session_path)
            else:
                session = save_session_interactively(session_path)
            print(f"Saved backend session to {session_path}")
            print(f"Token: {mask_secret(session.token)}; fakeid: {session.fakeid or '(empty)'}")
            return 0

        session = MpSession.load(session_path)
        client = MpBackendClient(session)

        if command == "mp-status":
            client.list_page(begin=0, count=1)
            print("Backend session looks usable.")
            print(f"Session age: {session.age_seconds()} seconds")
            print(f"Token: {mask_secret(session.token)}; fakeid: {session.fakeid or '(empty)'}")
            return 0

        if command == "mp-list":
            count = 0
            for raw in client.iter_raw_published(limit=args.limit):
                count += 1
                title, _ = mp_raw_title_key(raw)
                publish_time = backend_article_time(raw) or ""
                print(f"[mp_published] {publish_time} {title}")
            print(f"Total: {count}")
            return 0

        if command == "mp-download":
            count = run_mp_download(
                client=client,
                archive_dir=archive_dir,
                limit=args.limit,
                show_progress=args.progress,
                workers=args.workers,
            )
            print(f"Done. Processed {count} mp published article(s).")
            return 0
    except MpBackendError as exc:
        print(f"Error: {exc}")
        return 1
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    return 1


def run_mp_download(
    client: Any,
    archive_dir: Path,
    limit: Optional[int],
    show_progress: bool,
    workers: int = 3,
) -> int:
    validate_workers(workers)
    archiver = Archiver(client, archive_dir)
    archiver.progress = ProgressTracker(archive_dir / "progress.json", enabled=show_progress)
    archiver.progress.start_run(
        {
            "mode": "mp_backend",
            "include_published": True,
            "include_drafts": False,
            "limit": limit,
            "download_image_materials": False,
            "workers": workers,
        }
    )
    count = 0
    records = []
    try:
        raw_articles = list(client.iter_raw_published(limit=limit))
        archiver.progress.set_articles_discovered(len(raw_articles))
        archiver.progress.set_phase("articles")
        if workers == 1:
            count, records = archive_mp_raw_serial(
                archiver, client, raw_articles, show_progress
            )
        else:
            count, records = archive_mp_raw_parallel(
                archiver, client, raw_articles, show_progress, workers
            )
        archiver.progress.set_phase("index")
        archiver.write_index(records)
        archiver.manifest.save()
        archiver.progress.complete()
        return count
    except Exception as exc:
        archiver.progress.fail_run(str(exc))
        raise


def archive_mp_raw_serial(
    archiver: Archiver,
    client: Any,
    raw_articles: List[dict[str, Any]],
    show_progress: bool,
) -> tuple[int, List[dict[str, Any]]]:
    count = 0
    successful = 0
    records: List[dict[str, Any]] = []
    for raw in raw_articles:
        title, key = mp_raw_title_key(raw)
        count += 1
        archiver.progress.start_item("article", title, key, save=False)
        try:
            article = client.article_from_raw(raw)
            record = archiver.write_article(article)
        except Exception as exc:
            archiver.progress.fail_item("article", title, key, str(exc))
            print(f"Failed [mp_published] {title}: {exc}")
            continue
        records.append(record)
        successful += 1
        archiver.record_article_result(
            article,
            record,
            save_now=successful % SAVE_EVERY_N_ARTICLES == 0,
        )
        if not show_progress:
            print(f"Saved [mp_published] {article.title}")
    archiver.manifest.save()
    archiver.progress.save()
    return count, records


def archive_mp_raw_parallel(
    archiver: Archiver,
    client: Any,
    raw_articles: List[dict[str, Any]],
    show_progress: bool,
    workers: int,
) -> tuple[int, List[dict[str, Any]]]:
    count = 0
    successful = 0
    records: List[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for raw in raw_articles:
            title, key = mp_raw_title_key(raw)
            archiver.progress.start_item("article", title, key, save=False)
            future = executor.submit(download_mp_raw_article, archiver, client, raw)
            futures[future] = (title, key)

        for future in as_completed(futures):
            title, key = futures[future]
            count += 1
            try:
                article, record = future.result()
            except Exception as exc:
                archiver.progress.fail_item("article", title, key, str(exc))
                print(f"Failed [mp_published] {title}: {exc}")
                continue
            records.append(record)
            successful += 1
            archiver.record_article_result(
                article,
                record,
                save_now=successful % SAVE_EVERY_N_ARTICLES == 0,
            )
            if not show_progress:
                print(f"Saved [mp_published] {article.title}")
    archiver.manifest.save()
    archiver.progress.save()
    return count, records


def download_mp_raw_article(
    archiver: Archiver, client: Any, raw: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    article = client.article_from_raw(raw)
    record = archiver.write_article(article, AssetDownloader())
    return article, record


def mp_raw_title_key(raw: dict[str, Any]) -> tuple[str, str]:
    title = str(
        raw.get("title")
        or raw.get("digest")
        or raw.get("link")
        or raw.get("content_url")
        or "(untitled)"
    )
    key = str(
        raw.get("aid")
        or raw.get("appmsgid")
        or raw.get("link")
        or raw.get("content_url")
        or title
    )
    return title, key


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return "(empty)"
    if len(value) <= visible:
        return "*" * len(value)
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"


def clean_run_outputs(archive_dir: Path, remove_session: bool = False) -> List[str]:
    targets = [
        archive_dir / "articles",
        archive_dir / "materials",
        archive_dir / "index.html",
        archive_dir / "manifest.json",
        archive_dir / "articles_readable.json",
        archive_dir / "progress.json",
        archive_dir / ".access_token.json",
    ]
    if remove_session:
        targets.append(archive_dir / "mp_session.json")

    removed: List[str] = []
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(str(target))
        elif target.exists():
            target.unlink()
            removed.append(str(target))

    login_file = Path("mp_login.txt")
    if login_file.exists():
        login_file.unlink()
        removed.append(str(login_file))
    return removed
