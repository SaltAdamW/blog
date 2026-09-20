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
        self.in_figure = False
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
        if tag == "figure":
            self.in_figure = True
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs["href"]
        for key in ("src", "href"):
            if key in attrs:
                self.links.append(attrs[key])
        if tag == "source" and "srcset" in attrs:
            self.links.extend(value.strip().split()[0] for value in attrs["srcset"].split(","))

    def handle_endtag(self, tag):
        if tag == "article":
            self.in_article = False
        if tag == "figure":
            self.in_figure = False

    def handle_data(self, data):
        if self.in_article and not self.in_figure:
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
        if post.get("markdown_tables"):
            parser.enable("table")
        tokens = parser.parse((self.root / post["source"]).read_text())[3:]
        original = parser.renderer.render(tokens, parser.options, {})
        expected = Document(f'<article id="article-content">{original}</article>').article_text
        actual = Document(build.render_article(post)).article_text
        normalize = lambda chunks: " ".join("".join(chunks).split())
        self.assertEqual(normalize(actual), normalize(expected))

    def test_markdown_tables_are_opt_in_and_keep_cell_content(self):
        post = dict(build.load_posts()[0], source="table-test.md", figures={})
        (self.root / post["source"]).write_text(
            f'# {post["title"]}\n\n## 比较\n\n'
            '| 方法 | 准确率 |\n| --- | ---: |\n| kev | 0.790 |\n'
        )
        without = build.render_article(dict(post, markdown_tables=False))
        self.assertNotIn('<table>', without)
        with_tables = build.render_article(dict(post, markdown_tables=True))
        self.assertIn('<div class="prose-table" tabindex="0"><table>', with_tables)
        self.assertIn('<td>kev</td>', with_tables)
        self.assertIn('0.790</td>', with_tables)
        self.assertNotIn('| --- |', with_tables)

    def test_jev_longform_has_tables_figures_and_source_links(self):
        post = next(post for post in build.load_posts() if post["slug"] == "jev-understanding-and-generation")
        rendered = build.render_article(post)
        self.assertEqual(rendered.count('<table>'), 2)
        self.assertEqual(rendered.count('<figure class="entry-figure">'), 3)
        self.assertIn('https://docs.typesafe.ai/primitives', rendered)
        self.assertIn('https://aclanthology.org/2024.emnlp-main.491/', rendered)
        self.assertIn('data-view-count="busuanzi_page_pv"', rendered)
        self.assertNotIn('[s1]', rendered)

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

    def test_visit_counters_are_shared_and_hidden_without_javascript(self):
        self.build()
        for page in self.root.rglob("*.html"):
            text = page.read_text()
            self.assertEqual(text.count('data-view-count="busuanzi_site_pv" hidden'), 1)
            self.assertEqual(text.count(build.versioned_asset("assets/analytics.js")), 1)
            self.assertEqual(text.count('data-view-count="busuanzi_page_pv" hidden'), int("posts" in page.parts))
            self.assertNotIn('<script src="https://cdn.busuanzi.cc', text)
        self.assertIn("访问统计与隐私", (self.root / "about/index.html").read_text())

    def test_home_visit_count_is_in_intro_not_footer(self):
        self.build()
        home = (self.root / "index.html").read_text()
        intro_start = home.index('<header class="blog-intro">')
        intro = home[intro_start:home.index('</header>', intro_start)]
        footer = home[home.index('<footer class="site-footer">'):]
        self.assertIn('data-view-count="busuanzi_site_pv"', intro)
        self.assertNotIn('data-view-count=', footer)
        self.assertIn('class="blog-description"', intro)

    def test_weekly_figures_have_local_responsive_assets_and_sources(self):
        self.build()
        post = next(post for post in build.load_posts() if post["slug"] == "weekly-agent-research-2026-09-17")
        html = (self.root / post["url"] / "index.html").read_text()
        self.assertEqual(html.count('<figure class="entry-figure">'), 2)
        self.assertEqual(html.count('<picture>'), 2)
        for image, figure in post["figures"].items():
            self.assertIn(f'src="../../{image}"', html)
            self.assertIn(f'srcset="../../{figure["mobile"]}"', html)
            self.assertIn(f'href="../../{figure["diagram"]}"', html)
            self.assertIn(figure["caption"], html)
        self.assertIn("https://rtrvr.ai/blog/jev-browser-agent-benchmark", html)
        self.assertIn("https://mlcommons.org/2026/09/mlperf-inference-v6-1-results/", html)
        self.assertNotIn("drawio-viewer", html)

    def test_figure_manifest_rejects_missing_external_and_escaping_assets(self):
        path = self.root / "posts.json"
        original = json.loads(path.read_text())
        for value in ("assets/missing.png", "../outside.png", "https://example.org/image.png", "assets/avatar.jpg?tracking=1"):
            with self.subTest(value=value):
                posts = json.loads(json.dumps(original))
                post = next(post for post in posts if post.get("figures"))
                next(iter(post["figures"].values()))["mobile"] = value
                path.write_text(json.dumps(posts))
                with self.assertRaises(ValueError):
                    build.load_posts()

    def test_figure_manifest_rejects_invalid_dimensions_or_unused_entries(self):
        posts = build.load_posts()
        post = next(post for post in posts if post.get("figures"))
        figure = next(iter(post["figures"].values()))
        for value in (0, -1, True, "560"):
            figure["mobile_width"] = value
            (self.root / "posts.json").write_text(json.dumps(posts))
            with self.assertRaises(ValueError):
                build.load_posts()
        figure["mobile_width"] = 560
        post["figures"]["assets/avatar.jpg"] = dict(figure)
        with self.assertRaisesRegex(ValueError, "独立 Markdown"):
            build.render_article(post)

    def test_figure_caption_and_alt_are_escaped(self):
        post = next(post for post in build.load_posts() if post.get("figures"))
        image, figure = next(iter(post["figures"].items()))
        figure = dict(figure, caption='<script>alert("x")</script>')
        html = build.render_figure(image, figure, "../../", '图 " onload="alert(1)')
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&quot; onload=&quot;", html)


if __name__ == "__main__":
    unittest.main()
