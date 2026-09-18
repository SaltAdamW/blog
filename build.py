"""由统一文章清单生成首页、归档、关于页、文章详情和 RSS。"""

from collections import Counter
from datetime import date
from html import escape
from html.parser import HTMLParser
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
import json
import re
import xml.etree.ElementTree as ET

from markdown_it import MarkdownIt


ROOT = Path(__file__).parent
BRAND = "Adam‘s blog"
SITE_URL = "https://saltadamw.github.io/blog/"
DESCRIPTION = "关于开源系统、架构与实现的技术笔记。"
ET.register_namespace("", "http://www.w3.org/2000/svg")


def load_posts(root=None):
    root = root or ROOT
    posts = json.loads((root / "posts.json").read_text())
    slugs = set()
    for post in posts:
        slug = post["slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in slugs:
            raise ValueError(f"无效或重复的文章路径: {slug}")
        slugs.add(slug)
        date.fromisoformat(post["date"])
        for key in ("source", "source_manifest"):
            value = post.get(key)
            if not value:
                continue
            path = (root / value).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError(f"文章文件不存在或超出仓库: {value}")
        post["url"] = f"posts/{slug}/"
    return sorted(posts, key=lambda post: (post["date"], post["slug"]), reverse=True)


def icon(name):
    element = ET.parse(ROOT / "assets/icons" / f"{name}.svg").getroot()
    element.attrib.update({"class": f"icon icon-{name}", "width": "20", "height": "20", "aria-hidden": "true", "focusable": "false"})
    return ET.tostring(element, encoding="unicode")


def tool(action, label, symbol, extra=""):
    return f'<button class="icon-button js-only" type="button" data-action="{action}" aria-label="{label}" {extra}>{icon(symbol)}<span class="tooltip" role="tooltip">{label}</span></button>'


def versioned_asset(path):
    digest = sha256((ROOT / path).read_bytes()).hexdigest()[:12]
    return f"{path}?v={digest}"


def site_header(prefix, active, article=False):
    links = []
    for key, text, path in [("home", "文章", "index.html"), ("archive", "归档", "archive/index.html"), ("about", "关于", "about/index.html")]:
        selected = ' class="current" aria-current="page"' if key == active else ""
        links.append(f'<a href="{prefix}{path}" data-page="{key}"{selected}>{text}</a>')
    progress = '<div class="reading-track js-only" role="progressbar" aria-label="阅读进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="reading-fill"></div></div>' if article else ""
    return f'''<header class="site-header"><div class="header-inner">
      <a class="brand" href="{prefix}index.html" aria-label="{BRAND} 首页">Adam<span class="brand-apostrophe">‘</span>s blog<span class="brand-dot" aria-hidden="true">.</span></a>
      <nav class="site-nav" aria-label="站点导航">{"".join(links)}{tool('theme', '切换深色主题', 'moon', 'aria-pressed="false"')}</nav>
    </div>{progress}</header>'''


def site_footer(prefix, show_visits=True):
    visits = visit_counter('site_pv', '本站访问') if show_visits else ''
    return f'''<footer class="site-footer"><span>{BRAND}</span>{visits}<nav aria-label="页脚导航"><a href="{prefix}archive/index.html">归档</a><a href="{prefix}about/index.html">关于</a><a href="https://github.com/SaltAdamW/blog">GitHub {icon('arrow-up-right')}</a><a href="{prefix}feed.xml">{icon('rss')} RSS</a></nav></footer>'''


def visit_counter(metric, text):
    return f'<span class="visit-stat" data-view-count="busuanzi_{metric}" hidden>{text} <span class="visit-value" data-count-value role="status">加载中</span></span>'


def page_shell(title, description, path, prefix, active, body, *, article=False):
    templates = "".join(f'<template id="{name}-icon">{icon(name)}</template>' for name in ["copy", "check", "sun", "moon"])
    back = f'<a class="back-to-top js-only icon-button" href="#top" aria-label="回到顶部">{icon("arrow-up")}<span class="tooltip" role="tooltip">回到顶部</span></a>' if article else ""
    return f'''<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="description" content="{escape(description, quote=True)}">
<meta name="author" content="Adam">
<meta property="og:type" content="{'article' if article else 'website'}">
<meta property="og:title" content="{escape(title, quote=True)}">
<meta property="og:description" content="{escape(description, quote=True)}">
<meta property="og:url" content="{SITE_URL}{path}">
<meta property="og:site_name" content="{BRAND}">
<link rel="canonical" href="{SITE_URL}{path}">
<link rel="alternate" type="application/rss+xml" title="{BRAND}" href="{prefix}feed.xml">
<link rel="icon" href="{prefix}assets/avatar.jpg" type="image/jpeg">
<link rel="stylesheet" href="{prefix}{versioned_asset('assets/style.css')}">
<script src="{prefix}{versioned_asset('assets/site.js')}" defer></script>
<script src="{prefix}{versioned_asset('assets/analytics.js')}" defer></script>
<title>{escape(title)}</title>
</head><body id="top" class="{'article-page' if article else 'index-page'}">
<a class="skip-link" href="#{'article-content' if article else 'main-content'}">跳到正文</a>
{site_header(prefix, active, article)}
{body}
{site_footer(prefix, show_visits=active != 'home')}
{back}<div class="toast" role="status" aria-live="polite" aria-atomic="true"></div>
{templates}
</body></html>
'''


def date_label(value):
    parsed = date.fromisoformat(value)
    return f"{parsed.year} 年 {parsed.month} 月 {parsed.day} 日"


def render_article(post):
    prefix = "../../"
    manifest = json.loads((ROOT / post["source_manifest"]).read_text()) if post.get("source_manifest") else {"sources": []}
    source_urls = {source["url"] for source in manifest["sources"]}
    parser = MarkdownIt("commonmark", {"html": True})
    default_code = parser.renderer.rules["code_inline"]

    def inline_code(tokens, index, options, env):
        value = tokens[index].content
        if value in source_urls:
            return f'<a class="source-url" href="{escape(value, quote=True)}">{escape(value)}</a>'
        return default_code(tokens, index, options, env)

    parser.renderer.rules["code_inline"] = inline_code
    tokens = parser.parse((ROOT / post["source"]).read_text())
    if not tokens or tokens[0].type != "heading_open" or tokens[0].tag != "h1" or tokens[1].content != post["title"]:
        raise ValueError(f"文章标题与清单不一致: {post['source']}")
    tokens = tokens[3:]
    sections = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            token.attrSet("id", f"section-{index}")
            if token.tag == "h2":
                sections.append((token.attrGet("id"), tokens[index + 1].content))
    content = parser.renderer.render(tokens, parser.options, {})
    labels = post.get("toc_labels", [title for _, title in sections])
    if len(labels) != len(sections):
        raise ValueError(f"目录标签数量不一致: {post['slug']}")
    toc = "".join(f'<li><a href="#{identifier}" data-section="{identifier}"><span class="toc-number">{i + 1:02d}</span><span>{escape(label)}</span></a></li>' for i, ((identifier, _), label) in enumerate(zip(sections, labels)))
    primary, _, subtitle = post["title"].partition("：")
    subtitle_html = ""
    if subtitle:
        parts = subtitle.split("，", 1)
        subtitle_text = f'<span>{escape(parts[0])}，</span><wbr><span>{escape(parts[1])}</span>' if len(parts) == 2 else escape(subtitle)
        subtitle_html = f'<span class="subtitle">{subtitle_text}</span>'
    source_link = f'<a class="source-manifest-link" href="{prefix}{post["source_manifest"]}">来源清单 {icon("arrow-up-right")}</a>' if post.get("source_manifest") else ""
    source_count = f' · {len(source_urls)} 项一手资料' if source_urls else ""
    ending = f'<p>{escape(post["closing"])}</p>' if post.get("closing") else ""
    body = f'''<div class="page-layout">
      <aside class="article-sidebar" aria-label="文章导航"><div class="sidebar-inner">
        <a class="sidebar-home" href="{prefix}index.html">{icon('arrow-left')} 全部文章</a>
        <details class="toc-disclosure" open><summary>{icon('list')}<span>本篇目录</span>{icon('chevron-down')}</summary><nav class="article-toc" aria-label="文章目录"><ol>{toc}</ol></nav></details>
        <div class="sidebar-bottom"><p>{escape(post['category'])} / 架构与实现</p><a class="download-link" href="{prefix}{post['source']}" download>{icon('download')} Markdown 原稿</a>{source_link}<div class="sidebar-progress js-only"><span>阅读进度</span><output id="progress-label">0%</output></div></div>
      </div></aside>
      <main id="main-content">
        <header class="article-header">
          <nav class="breadcrumbs" aria-label="面包屑导航"><a href="{prefix}index.html">文章</a><span aria-hidden="true">/</span><span>{escape(post['category'])}</span></nav>
          <h1>{escape(primary)}{subtitle_html}</h1><p class="article-deck">{escape(post['deck'])}</p>
          <div class="article-byline"><a class="author" href="{prefix}about/index.html"><img src="{prefix}assets/avatar.jpg" alt="" width="36" height="36"><span>Adam</span></a><div class="article-meta"><time datetime="{post['date']}">{date_label(post['date'])}</time><span>{post['word_count']:,} 字{source_count}</span>{visit_counter('page_pv', '本篇浏览')}</div><div class="article-tools">{tool('copy-link', '复制文章链接', 'link')}<a class="icon-button" href="{prefix}{post['source']}" download aria-label="下载 Markdown 原稿">{icon('download')}<span class="tooltip" role="tooltip">下载原稿</span></a></div></div>
        </header>
        <article id="article-content" class="prose" tabindex="-1">{content}</article>
        <footer class="article-footer"><span class="end-mark" aria-hidden="true">∎</span>{ending}<div><a href="{prefix}index.html">{icon('arrow-left')} 返回全部文章</a><a href="{prefix}{post['source']}" download>{icon('download')} 下载原稿</a></div></footer>
      </main>
    </div>'''
    return page_shell(f"{post['title']} | {BRAND}", post["description"], post["url"], prefix, "", body, article=True)


def post_entry(post):
    primary, _, subtitle = post["title"].partition("：")
    parts = subtitle.split("，", 1)
    subtitle_text = f'<span>{escape(parts[0])}，</span><wbr><span>{escape(parts[1])}</span>' if len(parts) == 2 else escape(subtitle)
    subtitle_html = f'<span class="post-subtitle">{subtitle_text}</span>' if subtitle else ""
    search = escape(" ".join([post["title"], post["description"], post["category"], *post["tags"]]), quote=True)
    tags = "".join(f'<span>{escape(tag)}</span>' for tag in post["tags"])
    return f'''<li class="post-entry" data-search="{search}"><article>
      <div class="post-entry-meta"><time datetime="{post['date']}">{date_label(post['date'])}</time><span>{escape(post['category'])}</span><span>{post['word_count']:,} 字</span></div>
      <h3><a class="post-title" href="{post['url']}index.html">{escape(primary)}{subtitle_html}</a></h3>
      <p class="post-excerpt">{escape(post['description'])}</p>
      <div class="post-entry-bottom"><div class="post-tags">{tags}</div><a class="read-post" href="{post['url']}index.html" aria-label="阅读：{escape(post['title'], quote=True)}">阅读全文 {icon('arrow-up-right')}</a></div>
    </article></li>'''


def render_home(posts):
    categories = Counter(post["category"] for post in posts)
    category_links = "".join(f'<li><a href="index.html?q={quote(category)}#articles"><span>{escape(category)}</span><span>{count:02d}</span></a></li>' for category, count in categories.items())
    legacy = next((post for post in posts if post.get("legacy_home")), None)
    legacy_attr = f' data-legacy-article="{legacy["url"]}index.html"' if legacy else ""
    body = f'''<main id="main-content" class="blog-main" tabindex="-1"{legacy_attr}>
      <header class="blog-intro"><p class="section-eyebrow">NOTES ON ENGINEERING</p><h1>{BRAND}<span class="brand-dot" aria-hidden="true">.</span></h1><p class="blog-description">关于开源系统、架构与实现的技术笔记。</p>{visit_counter('site_pv', '本站访问')}</header>
      <div class="blog-grid"><section id="articles" class="post-collection" aria-labelledby="collection-title">
        <div class="collection-heading"><h2 id="collection-title">全部文章 <span>{len(posts):02d}</span></h2><a href="archive/index.html">按时间归档 {icon('arrow-up-right')}</a></div>
        <form class="post-search js-only" role="search"><label for="post-query">搜索文章</label><div class="search-field">{icon('search')}<input id="post-query" type="search" name="q" placeholder="标题、关键词或主题" autocomplete="off"><button type="reset" class="clear-search" hidden>清除</button></div></form>
        <p class="search-status" role="status" aria-live="polite" hidden></p>
        <ul class="post-list">{"".join(post_entry(post) for post in posts)}</ul>
        <div class="search-empty" hidden><h3>没有找到相关文章</h3><p>换一个关键词试试。</p><button type="button" data-action="reset-search">查看全部文章</button></div>
      </section><aside class="blog-sidebar" aria-label="关于博客">
        <div class="profile-heading"><img src="assets/avatar.jpg" width="48" height="48" alt="Adam 的头像"><div><h2>Adam</h2><a href="about/index.html">关于作者 {icon('arrow-up-right')}</a></div></div>
        <p class="profile-note">从问题出发，记录技术机制与设计取舍。</p>
        <section class="topic-section"><h2>文章主题</h2><ul>{category_links}</ul></section>
        <a class="sidebar-feed" href="feed.xml">{icon('rss')} RSS 订阅 {icon('arrow-up-right')}</a>
        <a class="sidebar-github" href="https://github.com/SaltAdamW/blog">GitHub {icon('arrow-up-right')}</a>
      </aside></div>
    </main>'''
    return page_shell(BRAND, DESCRIPTION, "", "", "home", body)


def render_archive(posts):
    years = sorted({post["date"][:4] for post in posts}, reverse=True)
    groups = []
    for year in years:
        entries = [post for post in posts if post["date"].startswith(year)]
        items = "".join(f'<li><time datetime="{post["date"]}">{post["date"][5:].replace("-", ".")}</time><div><h3><a href="../{post["url"]}index.html">{escape(post["title"])}</a></h3><p>{escape(post["category"])} · {post["word_count"]:,} 字</p></div>{icon("arrow-up-right")}</li>' for post in entries)
        groups.append(f'<section class="archive-year" id="year-{year}"><h2>{year}<span>{len(entries)} 篇</span></h2><ul>{items}</ul></section>')
    body = f'''<main id="main-content" class="blog-main archive-main" tabindex="-1"><header class="index-heading"><p class="section-eyebrow">ARCHIVE</p><h1>文章归档</h1><p>共 {len(posts)} 篇文章</p></header>{"".join(groups)}</main>'''
    return page_shell(f"文章归档 | {BRAND}", DESCRIPTION, "archive/", "../", "archive", body)


def render_about(posts):
    body = f'''<main id="main-content" class="blog-main about-main" tabindex="-1"><header class="index-heading"><p class="section-eyebrow">ABOUT</p><h1>关于 Adam</h1></header><div class="about-author"><img src="../assets/avatar.jpg" width="72" height="72" alt="Adam 的头像"><div><h2>Adam</h2><a href="https://github.com/SaltAdamW">@SaltAdamW {icon('arrow-up-right')}</a></div></div><div class="about-copy"><p>这里是 {BRAND}，记录开源系统、架构与实现的技术笔记。</p><p>从一个具体问题出发，沿着源码和一手资料，理解系统为什么这样设计，以及每个选择的适用条件与代价。</p><p>目前已发布 {len(posts)} 篇文章。</p></div><div class="about-links"><a href="../index.html">浏览全部文章 {icon('arrow-up-right')}</a><a href="../feed.xml">{icon('rss')} RSS 订阅</a><a href="https://github.com/SaltAdamW/blog">博客仓库 {icon('arrow-up-right')}</a></div></main>'''
    privacy = '<section class="about-copy privacy-note" aria-labelledby="privacy-title"><h2 id="privacy-title">访问统计与隐私</h2><p>本站使用 <a href="https://busuanzi.cc/doc.php">不蒜子（busuanzi.cc）</a> 累计浏览量，自 2026 年 9 月 18 日接入。刷新也会计数，浏览量不等于读者人数；接入前的访问无法补算。</p><p>统计请求仅提交公开页面地址，不包含搜索词、URL 参数或来源页，不发送 Cookie。统计服务仍可见请求 IP 与浏览器信息。开启浏览器「请勿追踪」或 Global Privacy Control 后，本站不发送统计请求。</p></section>'
    body = body.replace('</main>', privacy + '</main>')
    return page_shell(f"关于 Adam | {BRAND}", DESCRIPTION, "about/", "../", "about", body)


def render_feed(posts):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    for tag, text in [("title", BRAND), ("link", SITE_URL), ("description", DESCRIPTION), ("language", "zh-CN")]:
        ET.SubElement(channel, tag).text = text
    for post in posts:
        item = ET.SubElement(channel, "item")
        for tag, text in [("title", post["title"]), ("link", SITE_URL + post["url"]), ("guid", SITE_URL + post["url"]), ("description", post["description"]), ("category", post["category"])]:
            ET.SubElement(item, tag).text = text
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


class LinkCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.targets = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            assert attrs["id"] not in self.ids, f'重复锚点: {attrs["id"]}'
            self.ids.add(attrs["id"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.targets.add(attrs["href"][1:])


def build_site():
    posts = load_posts()
    pages = {"index.html": render_home(posts), "archive/index.html": render_archive(posts), "about/index.html": render_about(posts)}
    for post in posts:
        pages[post["url"] + "index.html"] = render_article(post)
    for path, page in pages.items():
        check = LinkCheck()
        check.feed(page)
        assert check.targets <= check.ids, f"缺失锚点: {path}: {check.targets - check.ids}"
        destination = ROOT / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(page)
    (ROOT / "feed.xml").write_text(render_feed(posts))
    print(json.dumps({"posts": len(posts), "pages": list(pages), "feed": "feed.xml"}, ensure_ascii=False))


if __name__ == "__main__":
    build_site()
