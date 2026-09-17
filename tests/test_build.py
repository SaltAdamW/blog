from contextlib import redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import io
import json
import shutil
import unittest
import xml.etree.ElementTree as ET

import build


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids = set()
        self.links = []
        self.canonical = None
        self.headings = 0
        self.in_article = False
        self.article_text = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "h1":
            self.headings += 1
        if tag == "article" and attrs.get("id") == "article-content":
            self.in_article = True
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs["href"]
        for key in ("src", "href"):
            if key in attrs:
                self.links.append(attrs[key])

    def handle_endtag(self, tag):
        if tag == "article":
            self.in_article = False

    def handle_data(self, data):
        if self.in_article:
            self.article_text.append(data)


class BlogBuildTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        files = {"posts.json"}
        for post in build.load_posts():
            files.update(post[key] for key in ("source", "source_manifest") if post.get(key))
        for name in files:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(build.ROOT / name, self.root / name)
        shutil.copytree(build.ROOT / "assets", self.root / "assets")
        self.root_patch = patch.object(build, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.directory.cleanup()

    def build(self):
        with redirect_stdout(io.StringIO()):
            build.build_site()

    def test_home_is_a_collection_and_article_has_a_permalink(self):
        self.build()
        home = (self.root / "index.html").read_text()
        self.assertIn('class="post-list"', home)
        self.assertNotIn('id="article-content"', home)
        post = build.load_posts()[0]
        article = (self.root / post["url"] / "index.html").read_text()
        self.assertIn('id="article-content"', article)
        self.assertEqual(Document(article).canonical, build.SITE_URL + post["url"])
        self.assertEqual(Document(home).canonical, build.SITE_URL)

    def test_local_links_assets_and_fragments_resolve(self):
        self.build()
        for path in self.root.rglob("*.html"):
            doc = Document(path.read_text())
            self.assertEqual(doc.headings, 1, str(path))
            for link in doc.links:
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc:
                    continue
                target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
                if target.is_dir():
                    target /= "index.html"
                self.assertTrue(target.is_relative_to(self.root), link)
                self.assertTrue(target.is_file(), f"{path}: {link}")
                if parsed.fragment and target.suffix == ".html":
                    self.assertIn(unquote(parsed.fragment), Document(target.read_text()).ids, link)

    def test_article_text_is_preserved(self):
        post = build.load_posts()[0]
        parser = build.MarkdownIt("commonmark", {"html": True})
        tokens = parser.parse((self.root / post["source"]).read_text())[3:]
        original = parser.renderer.render(tokens, parser.options, {})
        expected = Document(f'<article id="article-content">{original}</article>').article_text
        actual = Document(build.render_article(post)).article_text
        normalize = lambda chunks: " ".join("".join(chunks).split())
        self.assertEqual(normalize(actual), normalize(expected))

    def test_a_second_post_updates_all_collections(self):
        posts = json.loads((self.root / "posts.json").read_text())
        expected_count = len(posts) + 1
        (self.root / "second.md").write_text("# 构建测试文章\n\n正文。\n\n## 问题\n\n示例。\n")
        posts.append({"slug": "build-test", "title": "构建测试文章", "date": "2025-12-01", "category": "工程实践", "tags": ["测试"], "description": "构建验证。", "deck": "测试导语", "word_count": 10, "source": "second.md"})
        (self.root / "posts.json").write_text(json.dumps(posts, ensure_ascii=False))
        self.build()
        home = (self.root / "index.html").read_text()
        self.assertEqual(home.count('class="post-entry"'), expected_count)
        self.assertLess(home.index('datetime="2026-09-17"'), home.index('datetime="2025-12-01"'))
        archive = (self.root / "archive/index.html").read_text()
        self.assertIn('id="year-2026"', archive)
        self.assertIn('id="year-2025"', archive)
        self.assertTrue((self.root / "posts/build-test/index.html").is_file())
        feed = ET.parse(self.root / "feed.xml")
        self.assertEqual(len(feed.findall("channel/item")), expected_count)
        self.assertEqual(feed.findtext("channel/title"), build.BRAND)

    def test_invalid_paths_and_duplicate_slugs_are_rejected(self):
        original = json.loads((self.root / "posts.json").read_text())
        cases = [[original[0], original[0]], [dict(original[0], slug="../outside")], [dict(original[0], source="../outside.md")], [dict(original[0], date="2026-99-99")]]
        for posts in cases:
            with self.subTest(posts=posts):
                (self.root / "posts.json").write_text(json.dumps(posts, ensure_ascii=False))
                with self.assertRaises(ValueError):
                    build.load_posts()

    def test_build_is_reproducible(self):
        self.build()
        first = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.html")}
        first["feed.xml"] = (self.root / "feed.xml").read_bytes()
        self.build()
        for name, content in first.items():
            self.assertEqual((self.root / name).read_bytes(), content, name)

    def test_asset_version_changes_with_content(self):
        old = build.versioned_asset("assets/site.js")
        with (self.root / "assets/site.js").open("a") as output:
            output.write("\n// test\n")
        self.assertNotEqual(build.versioned_asset("assets/site.js"), old)
        self.build()
        for page in self.root.rglob("*.html"):
            self.assertIn(build.versioned_asset("assets/site.js"), page.read_text())


if __name__ == "__main__":
    unittest.main()
