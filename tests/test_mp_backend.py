import contextlib
import io
import stat
import tempfile
import unittest
from pathlib import Path

from wedownloader.cli import run_mp_download
from wedownloader.mp_backend import (
    MpBackendClient,
    MpSession,
    count_article_list_entries,
    extract_token,
    parse_login_file,
    parse_article_html,
    parse_article_list,
    save_session_from_file,
)
from wedownloader.archive import Article


class MpBackendTests(unittest.TestCase):
    def test_session_save_and_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mp_session.json"
            session = MpSession(token="123", cookie="a=b", login_at=1710000000)

            session.save(path)
            loaded = MpSession.load(path)

        self.assertEqual(loaded.token, "123")
        self.assertEqual(loaded.cookie, "a=b")

    def test_session_file_is_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mp_session.json"
            MpSession(token="123", cookie="a=b", login_at=1710000000).save(path)

            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(mode, 0o600)

    def test_extract_token_from_url_or_plain_value(self):
        self.assertEqual(extract_token("https://mp.weixin.qq.com/?token=123&lang=zh_CN"), "123")
        self.assertEqual(extract_token("456"), "456")
        self.assertEqual(extract_token("not-a-token"), "")

    def test_parse_login_file_key_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mp_login.txt"
            path.write_text(
                "URL=https://mp.weixin.qq.com/cgi-bin/home?token=123\n"
                "COOKIE=a=b; c=d\n"
                "FAKEID=fake\n",
                encoding="utf-8",
            )

            values = parse_login_file(path)

        self.assertEqual(values["url"], "https://mp.weixin.qq.com/cgi-bin/home?token=123")
        self.assertEqual(values["cookie"], "a=b; c=d")
        self.assertEqual(values["fakeid"], "fake")

    def test_save_session_from_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "mp_login.txt"
            output_path = Path(temp_dir) / "mp_session.json"
            input_path.write_text("TOKEN=123\nCOOKIE=a=b\n", encoding="utf-8")

            session = save_session_from_file(input_path, output_path)
            loaded = MpSession.load(output_path)

        self.assertEqual(session.token, "123")
        self.assertEqual(loaded.cookie, "a=b")

    def test_parse_appmsg_list_response(self):
        data = {"app_msg_list": [{"title": "A", "link": "https://mp.weixin.qq.com/s/a"}]}

        items = parse_article_list(data)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "A")

    def test_parse_publish_page_response(self):
        data = {
            "publish_page": (
                '{"publish_list":[{"publish_info":"{\\"appmsgex\\":[{\\"title\\":\\"A\\",'
                '\\"link\\":\\"https://mp.weixin.qq.com/s/a\\"}]}"}]}'
            )
        }

        items = parse_article_list(data)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "A")

    def test_count_publish_page_entries_before_flattening(self):
        data = {
            "publish_page": (
                '{"publish_list":['
                '{"publish_info":"{\\"appmsgex\\":[{\\"title\\":\\"A\\"},{\\"title\\":\\"B\\"}]}"},'
                '{"publish_info":"{\\"appmsgex\\":[{\\"title\\":\\"C\\"}]}" }'
                "]}"
            )
        }

        self.assertEqual(count_article_list_entries(data), 2)
        self.assertEqual(len(parse_article_list(data)), 3)

    def test_backend_list_uses_browser_publish_endpoint_first(self):
        client = FakeListClient()

        data = client.list_page(begin=20, count=10)

        self.assertEqual(data["source"], "publish")
        self.assertEqual(client.requests[0][0], "/cgi-bin/appmsgpublish")
        self.assertEqual(client.requests[0][1]["sub"], "list")
        self.assertNotIn("sub_action", client.requests[0][1])

    def test_iter_raw_published_paginates_by_backend_entries(self):
        client = FakePagedPublishClient()

        titles = [item["title"] for item in client.iter_raw_published()]

        self.assertEqual(titles, ["A", "B", "C", "D"])
        self.assertEqual([request[1]["begin"] for request in client.requests], [0, 10])

    def test_parse_article_html(self):
        page = """
        <html><head><title>fallback</title></head><body>
        <h1 id="activity-name"> 标题 </h1>
        <span id="js_name"> 作者 </span>
        <div id="js_content"><p>正文</p></div>
        <script>var createTime = "1710000000";</script>
        </body></html>
        """

        parsed = parse_article_html(page)

        self.assertEqual(parsed["title"], "标题")
        self.assertEqual(parsed["author"], "作者")
        self.assertEqual(parsed["content"], "<p>正文</p>")
        self.assertEqual(parsed["create_time"], 1710000000)

    def test_mp_download_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeMpClient()
            with contextlib.redirect_stdout(io.StringIO()):
                count = run_mp_download(
                    client=client,
                    archive_dir=Path(temp_dir),
                    limit=1,
                    show_progress=False,
                )

        self.assertEqual(count, 1)
        self.assertEqual(client.converted, 1)

    def test_article_from_browser_publish_raw_fields(self):
        client = FakeArticleClient()

        article = client.article_from_raw(
            {
                "appmsgid": 2247485787,
                "itemidx": 1,
                "content_url": "https://mp.weixin.qq.com/s/short",
                "title": "DAY2: 2025读书小结",
                "digest": "摘要",
                "line_info": {"send_time": 1767109942},
            }
        )

        self.assertEqual(article.source_id, "2247485787")
        self.assertEqual(article.index, 0)
        self.assertEqual(article.url, "https://mp.weixin.qq.com/s/short")
        self.assertEqual(article.update_time, 1767109942)
        self.assertEqual(article.content, "<p>正文</p>")


class FakeMpClient:
    def __init__(self):
        self.converted = 0

    def iter_raw_published(self, limit=None):
        items = [
            {"title": "A", "link": "https://mp.weixin.qq.com/s/a"},
            {"title": "B", "link": "https://mp.weixin.qq.com/s/b"},
        ]
        for item in items[:limit]:
            yield item

    def article_from_raw(self, raw):
        self.converted += 1
        return Article(
            source="mp_published",
            source_id=raw["title"],
            index=0,
            title=raw["title"],
            author="",
            digest="",
            content="<p>正文</p>",
            url=raw["link"],
            update_time=1710000000,
            raw=raw,
        )


class FakeListClient(MpBackendClient):
    def __init__(self):
        super().__init__(MpSession(token="123", cookie="a=b", login_at=1710000000))
        self.requests = []

    def _request_json(self, path, params):
        self.requests.append((path, params))
        return {
            "source": "publish",
            "publish_page": (
                '{"publish_list":[{"publish_info":"{\\"appmsgex\\":[{\\"title\\":\\"A\\",'
                '\\"link\\":\\"https://mp.weixin.qq.com/s/a\\"}]}"}]}'
            ),
        }


class FakePagedPublishClient(MpBackendClient):
    def __init__(self):
        super().__init__(MpSession(token="123", cookie="a=b", login_at=1710000000))
        self.requests = []

    def _request_json(self, path, params):
        self.requests.append((path, params))
        begin = int(params["begin"])
        if begin == 0:
            return {
                "publish_page": (
                    '{"publish_list":['
                    '{"publish_info":"{\\"appmsgex\\":[{\\"title\\":\\"A\\"},{\\"title\\":\\"B\\"}]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[{\\"title\\":\\"C\\"}]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[]}"},'
                    '{"publish_info":"{\\"appmsgex\\":[]}" }'
                    "]}"
                )
            }
        return {
            "publish_page": (
                '{"publish_list":[{"publish_info":"{\\"appmsgex\\":[{\\"title\\":\\"D\\"}]}" }]}'
            )
        }


class FakeArticleClient(MpBackendClient):
    def __init__(self):
        super().__init__(MpSession(token="123", cookie="a=b", login_at=1710000000))

    def fetch_article_page(self, url):
        return '<div id="js_content"><p>正文</p></div>'


if __name__ == "__main__":
    unittest.main()
