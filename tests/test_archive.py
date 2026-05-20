import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from wedownloader.cli import clean_run_outputs
from wedownloader.archive import (
    Archiver,
    Article,
    article_record_identity,
    dedupe_article_records,
    extract_articles,
    sanitize_filename,
    validate_workers,
)
from wedownloader.wechat import WeChatApiError


class FakeClient:
    def iter_freepublish(self):
        return iter(
            [
                {
                    "publish_id": "pub-1",
                    "update_time": 1710000000,
                    "content": {
                        "news_item": [
                            {
                                "title": "成功文章",
                                "author": "作者",
                                "digest": "摘要",
                                "content": '<img src="https://mmbiz.qpic.cn/ok/0">',
                            },
                            {
                                "title": "失败文章",
                                "author": "作者",
                                "digest": "摘要",
                                "content": '<img src="https://mmbiz.qpic.cn/bad/0">',
                            },
                        ]
                    },
                }
            ]
        )

    def iter_drafts(self):
        return iter([])

    def iter_materials(self, material_type):
        return iter([])


class UnauthorizedPublishedClient:
    def iter_freepublish(self):
        raise WeChatApiError("WeChat API error 48001: api unauthorized", {"errcode": 48001})

    def iter_drafts(self):
        return iter(
            [
                {
                    "media_id": "draft-1",
                    "update_time": 1710000000,
                    "content": {"news_item": [{"title": "草稿", "content": "<p>正文</p>"}]},
                }
            ]
        )


class FakeAssetDownloader:
    def localize_html(self, content, assets_dir):
        if "bad" in content:
            raise RuntimeError("asset exploded")
        return content, [
            Asset("https://mmbiz.qpic.cn/ok/0", "downloaded"),
            Asset("https://mmbiz.qpic.cn/manual/0", "needs_manual_fetch"),
        ]


class Asset:
    def __init__(self, url, status):
        self.url = url
        self.status = status
        self.local_path = "assets/x.jpg"
        self.error = ""


class SaveCounter:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.wrapped()


class ArchiveTests(unittest.TestCase):
    def test_extract_articles_from_draft_shape(self):
        item = {
            "media_id": "media-1",
            "update_time": 1710000000,
            "content": {
                "news_item": [
                    {
                        "title": "标题",
                        "author": "作者",
                        "digest": "摘要",
                        "content": "<p>正文</p>",
                        "url": "https://example.com/a",
                    }
                ]
            },
        }

        articles = list(extract_articles("draft", item))

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].source_id, "media-1")
        self.assertEqual(articles[0].title, "标题")
        self.assertEqual(articles[0].content, "<p>正文</p>")

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename(' a/b:c * " d '), "a_b_c_d")

    def test_run_records_progress_and_continues_after_article_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archiver = Archiver(FakeClient(), Path(temp_dir))
            archiver.asset_downloader = FakeAssetDownloader()

            with contextlib.redirect_stdout(io.StringIO()):
                count = archiver.run(
                    include_published=True,
                    include_drafts=False,
                    limit=None,
                    workers=1,
                )

            progress = archiver.progress.data

        self.assertEqual(count, 2)
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(progress["counters"]["articles_discovered"], 2)
        self.assertEqual(progress["counters"]["items_processed"], 2)
        self.assertEqual(progress["counters"]["items_succeeded"], 1)
        self.assertEqual(progress["counters"]["items_failed"], 1)
        self.assertEqual(progress["counters"]["assets_downloaded"], 1)
        self.assertEqual(progress["counters"]["assets_needs_manual_fetch"], 1)
        self.assertEqual(progress["recent_errors"][0]["error"], "asset exploded")

    def test_parallel_archive_updates_manifest_from_main_thread(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archiver = Archiver(FakeClient(), Path(temp_dir))
            articles = [
                Article("mp_published", "1", 0, "A", "", "", "<p>A</p>", "", 1, {}),
                Article("mp_published", "2", 0, "B", "", "", "<p>B</p>", "", 1, {}),
                Article("mp_published", "3", 0, "C", "", "", "<p>C</p>", "", 1, {}),
            ]

            def fake_write_article(article, asset_downloader=None):
                if article.title == "B":
                    raise RuntimeError("boom")
                return {
                    "key": article.key,
                    "source": article.source,
                    "source_id": article.source_id,
                    "index": article.index,
                    "title": article.title,
                    "author": "",
                    "digest": "",
                    "url": "",
                    "update_time": 1,
                    "path": f"{article.title}/index.html",
                    "metadata_path": f"{article.title}/metadata.json",
                    "assets": [{"url": f"https://example.com/{article.title}.jpg", "status": "downloaded"}],
                }

            archiver.write_article = fake_write_article
            archiver.progress.start_run({"workers": 3})
            archiver.progress.set_articles_discovered(3)

            with contextlib.redirect_stdout(io.StringIO()):
                count, records = archiver.archive_articles(
                    articles, show_progress=False, workers=3
                )

        self.assertEqual(count, 3)
        self.assertEqual(len(records), 2)
        self.assertEqual(archiver.progress.data["counters"]["items_succeeded"], 2)
        self.assertEqual(archiver.progress.data["counters"]["items_failed"], 1)
        self.assertEqual(len(archiver.manifest.data["articles"]), 2)

    def test_archive_batches_manifest_saves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archiver = Archiver(FakeClient(), Path(temp_dir))
            archiver.manifest.save = SaveCounter(archiver.manifest.save)
            archiver.progress.save = SaveCounter(archiver.progress.save)
            articles = [
                Article("mp_published", str(index), 0, f"A{index}", "", "", "<p>A</p>", "", 1, {})
                for index in range(9)
            ]

            with contextlib.redirect_stdout(io.StringIO()):
                count, records = archiver.archive_articles(
                    articles, show_progress=False, workers=1
                )

        self.assertEqual(count, 9)
        self.assertEqual(len(records), 9)
        self.assertEqual(archiver.manifest.save.calls, 1)
        self.assertEqual(archiver.progress.save.calls, 1)

    def test_write_index_also_writes_readable_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archiver = Archiver(FakeClient(), Path(temp_dir))

            archiver.write_index(
                [
                    {
                        "title": "中文标题",
                        "author": "作者",
                        "source": "mp_published",
                        "update_time": 1710000000,
                        "url": "https://example.com",
                        "path": "articles/a/index.html",
                        "metadata_path": "articles/a/metadata.json",
                        "assets": [{"status": "downloaded"}, {"status": "failed"}],
                    }
                ]
            )

            readable = (Path(temp_dir) / "articles_readable.json").read_text(
                encoding="utf-8"
            )

        self.assertIn("中文标题", readable)
        self.assertIn('"资源下载成功": 1', readable)

    def test_mp_published_identity_matches_full_and_backend_urls(self):
        old_record = {
            "source": "mp_published",
            "source_id": "MzU4MDg0MTk4Nw==_2247486329_1",
            "index": 0,
            "title": "A",
            "url": "http://mp.weixin.qq.com/s?__biz=MzU4MDg0MTk4Nw==&mid=2247486329&idx=1&sn=x#rd",
        }
        new_record = {
            "source": "mp_published",
            "source_id": "2247486329",
            "index": 0,
            "title": "A",
            "url": "https://mp.weixin.qq.com/s/short",
        }

        self.assertEqual(article_record_identity(old_record), article_record_identity(new_record))

    def test_dedupe_article_records_prefers_richer_record(self):
        records = [
            {
                "source": "mp_published",
                "source_id": "2247486329",
                "index": 0,
                "title": "A",
                "url": "",
                "update_time": 0,
            },
            {
                "source": "mp_published",
                "source_id": "2247486329",
                "index": 0,
                "title": "A",
                "url": "https://mp.weixin.qq.com/s/short",
                "update_time": 1770000000,
            },
        ]

        deduped = dedupe_article_records(records)

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["url"], "https://mp.weixin.qq.com/s/short")

    def test_published_48001_does_not_block_drafts(self):
        archiver = Archiver(UnauthorizedPublishedClient(), Path("/tmp/wedownloader-test"))

        with contextlib.redirect_stdout(io.StringIO()):
            articles = list(archiver.iter_articles(True, True, None))

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].source, "draft")
        self.assertEqual(articles[0].title, "草稿")

    def test_clean_run_outputs_keeps_session_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "archive"
            (archive / "articles").mkdir(parents=True)
            (archive / "articles" / "x.txt").write_text("x", encoding="utf-8")
            (archive / "manifest.json").write_text("{}", encoding="utf-8")
            (archive / "mp_session.json").write_text("{}", encoding="utf-8")

            removed = clean_run_outputs(archive)

            self.assertFalse((archive / "articles").exists())
            self.assertFalse((archive / "manifest.json").exists())
            self.assertTrue((archive / "mp_session.json").exists())
            self.assertTrue(any("articles" in item for item in removed))

    def test_validate_workers_rejects_zero(self):
        with self.assertRaises(ValueError):
            validate_workers(0)


if __name__ == "__main__":
    unittest.main()
