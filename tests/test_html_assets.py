import tempfile
import unittest
from pathlib import Path

from wedownloader.html_assets import AssetDownloader


class FakeDownloader(AssetDownloader):
    def _fetch(self, url):
        if "private" in url:
            from urllib.error import HTTPError

            raise HTTPError(url, 403, "Forbidden", None, None)
        return b"image-bytes", "image/jpeg"


class HtmlAssetTests(unittest.TestCase):
    def test_localize_data_src_and_leave_external_href(self):
        html = (
            '<p><a href="https://example.com/page">link</a>'
            '<img data-src="https://mmbiz.qpic.cn/a/0?wx_fmt=jpeg"></p>'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            rewritten, assets = FakeDownloader().localize_html(html, Path(temp_dir))

        self.assertIn('href="https://example.com/page"', rewritten)
        self.assertIn('src="assets/', rewritten)
        self.assertEqual(assets[0].status, "downloaded")

    def test_private_asset_is_recorded_without_rewrite(self):
        html = '<img src="https://mmbiz.qpic.cn/private/0?wx_fmt=jpeg">'
        with tempfile.TemporaryDirectory() as temp_dir:
            rewritten, assets = FakeDownloader().localize_html(html, Path(temp_dir))

        self.assertIn("https://mmbiz.qpic.cn/private/0?wx_fmt=jpeg", rewritten)
        self.assertEqual(assets[0].status, "needs_manual_fetch")

    def test_ignores_wechat_pseudo_and_monitoring_urls(self):
        html = (
            '<script src="https://__bridge_loaded__"></script>'
            '<img src="https://badjs.weixinbridge.com/report?">'
            '<img src="https://mp.weixin.qq.com/mp/jsmonitor?idkey=1">'
            '<img src="https://mmbiz.qpic.cn/a/0?wx_fmt=jpeg">'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            rewritten, assets = FakeDownloader().localize_html(html, Path(temp_dir))

        self.assertIn("https://__bridge_loaded__", rewritten)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].status, "downloaded")

    def test_skips_asset_like_url_on_untrusted_host(self):
        html = '<img src="https://example.com/a.jpg">'
        with tempfile.TemporaryDirectory() as temp_dir:
            rewritten, assets = FakeDownloader().localize_html(html, Path(temp_dir))

        self.assertIn('src="https://example.com/a.jpg"', rewritten)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].status, "skipped")


if __name__ == "__main__":
    unittest.main()
