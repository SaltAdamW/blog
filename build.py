"""从 Markdown 生成可以直接打开、无需服务端的文章页面。"""

from html import escape
from html.parser import HTMLParser
from pathlib import Path
import json
import xml.etree.ElementTree as ET

from markdown_it import MarkdownIt


ROOT = Path(__file__).parent
BRAND = "Adam‘s blog"
CANONICAL = "https://saltadamw.github.io/blog/"
DESCRIPTION = "一次准备，如何成为一百个彼此独立的起点？从开源实现理解模板、内存快照、写时复制与沙箱编排。"
manifest = json.loads((ROOT / "source-manifest.json").read_text())
source_urls = {item["url"] for item in manifest["sources"]}
parser = MarkdownIt("commonmark", {"html": True})
default_code = parser.renderer.rules["code_inline"]


def inline_code(tokens, index, options, env):
    value = tokens[index].content
    if value in source_urls:
        url = escape(value, quote=True)
        return f'<a class="source-url" href="{url}">{escape(value)}</a>'
    return default_code(tokens, index, options, env)


parser.renderer.rules["code_inline"] = inline_code
tokens = parser.parse((ROOT / "article.md").read_text())
title = tokens[1].content
tokens = tokens[3:]
sections = []
for index, token in enumerate(tokens):
    if token.type == "heading_open":
        token.attrSet("id", f"section-{index}")
        if token.tag == "h2":
            sections.append((token.attrGet("id"), tokens[index + 1].content))

assert len(sections) == 8
content = parser.renderer.render(tokens, parser.options, {})
ET.register_namespace("", "http://www.w3.org/2000/svg")


def icon(name):
    element = ET.parse(ROOT / "assets" / "icons" / f"{name}.svg").getroot()
    element.attrib.update({"class": f"icon icon-{name}", "width": "20", "height": "20", "aria-hidden": "true", "focusable": "false"})
    return ET.tostring(element, encoding="unicode")


def tool(action, label, symbol, extra=""):
    return f'<button class="icon-button js-only" type="button" data-action="{action}" aria-label="{label}" {extra}>{icon(symbol)}<span class="tooltip" role="tooltip">{label}</span></button>'


short_names = ["阅读主线", "装好软件，为什么还不够", "一份环境，一百个独立起点", "保存现场，不必保留机器", "延续现场，还是重新开始", "恢复之外，还需要什么", "结语", "参考资料"]
toc = "".join(
    f'<li><a href="#{identifier}" data-section="{identifier}"><span class="toc-number">{str(i).zfill(2) if 1 <= i <= 5 else "·"}</span><span>{short_names[i]}</span></a></li>'
    for i, (identifier, label) in enumerate(sections)
)
reference_id = sections[-1][0]
primary_title, subtitle = title.split("：", 1)
subtitle_parts = subtitle.split("，", 1)
subtitle_html = f'<span>{escape(subtitle_parts[0])}，</span><wbr><span>{escape(subtitle_parts[1])}</span>'
page = f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="description" content="{DESCRIPTION}">
  <meta name="author" content="Adam">
  <meta property="og:type" content="article">
  <meta property="og:title" content="{escape(title)} | {BRAND}">
  <meta property="og:description" content="{DESCRIPTION}">
  <meta property="og:url" content="{CANONICAL}">
  <meta property="og:site_name" content="{BRAND}">
  <link rel="canonical" href="{CANONICAL}">
  <link rel="icon" href="assets/avatar.jpg" type="image/jpeg">
  <link rel="stylesheet" href="assets/style.css">
  <script src="assets/site.js" defer></script>
  <title>{escape(title)} | {BRAND}</title>
</head>
<body id="top">
  <a class="skip-link" href="#article-content">跳到正文</a>
  <header class="site-header">
    <div class="header-inner">
      <a class="brand" href="./" aria-label="{BRAND} 首页">Adam<span class="brand-apostrophe">‘</span>s blog<span class="brand-dot" aria-hidden="true">.</span></a>
      <nav class="site-nav" aria-label="站点导航">
        <a class="current" href="#top" aria-current="page">文章</a>
        <a class="references-nav" href="#{reference_id}">资料</a>
        <a class="github-link" href="https://github.com/SaltAdamW/blog">GitHub {icon('arrow-up-right')}</a>
        {tool('theme', '切换深色主题', 'moon', 'aria-pressed="false"')}
      </nav>
    </div>
    <div class="reading-track js-only" role="progressbar" aria-label="阅读进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="reading-fill"></div></div>
  </header>
  <div class="page-layout">
    <aside class="article-sidebar" aria-label="文章导航">
      <div class="sidebar-inner">
        <p class="sidebar-eyebrow">CONTENTS <span>01 / 长文</span></p>
        <details class="toc-disclosure" open>
          <summary>{icon('list')}<span>本篇目录</span>{icon('chevron-down')}</summary>
          <nav class="article-toc" aria-label="文章目录"><ol>{toc}</ol></nav>
        </details>
        <div class="sidebar-bottom">
          <p>开源系统 / 架构与实现</p>
          <a class="download-link" href="article.md" download>{icon('download')} Markdown 原稿</a>
          <a class="source-manifest-link" href="source-manifest.json">来源清单 {icon('arrow-up-right')}</a>
          <div class="sidebar-progress js-only"><span>阅读进度</span><output id="progress-label">0%</output></div>
        </div>
      </div>
    </aside>
    <main id="main-content">
      <header class="article-header">
        <div class="article-kicker"><span>ENGINEERING NOTES</span><span class="issue">NO. 001</span></div>
        <h1>{escape(primary_title)}<span class="subtitle">{subtitle_html}</span></h1>
        <p class="article-deck">一次准备，如何成为一百个彼此独立的起点？</p>
        <div class="article-byline">
          <a class="author" href="https://github.com/SaltAdamW"><img src="assets/avatar.jpg" alt="" width="36" height="36"><span>Adam</span></a>
          <div class="article-meta"><time datetime="2026-09-17">2026 年 9 月 17 日</time><span>40,598 字 · 29 项一手资料</span></div>
          <div class="article-tools">{tool('copy-link', '复制文章链接', 'link')}<a class="icon-button" href="article.md" download aria-label="下载 Markdown 原稿">{icon('download')}<span class="tooltip" role="tooltip">下载原稿</span></a></div>
        </div>
      </header>
      <article id="article-content" class="prose" tabindex="-1">{content}</article>
      <footer class="article-footer">
        <span class="end-mark" aria-hidden="true">∎</span>
        <p>独立运行，不必从零开始。</p>
        <div><a href="article.md" download>{icon('download')} 下载原稿</a><a href="https://github.com/SaltAdamW/blog">GitHub 仓库 {icon('arrow-up-right')}</a></div>
      </footer>
    </main>
  </div>
  <footer class="site-footer"><span>{BRAND}</span><span>2026 · 技术笔记</span><a href="#top">回到顶部 {icon('arrow-up')}</a></footer>
  <a class="back-to-top js-only icon-button" href="#top" aria-label="回到顶部">{icon('arrow-up')}<span class="tooltip" role="tooltip">回到顶部</span></a>
  <div class="toast" role="status" aria-live="polite" aria-atomic="true"></div>
  <template id="copy-icon">{icon('copy')}</template>
  <template id="check-icon">{icon('check')}</template>
  <template id="sun-icon">{icon('sun')}</template>
  <template id="moon-icon">{icon('moon')}</template>
</body>
</html>
'''


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


check = LinkCheck()
check.feed(page)
assert check.targets <= check.ids, f"缺失锚点: {check.targets - check.ids}"
(ROOT / "index.html").write_text(page)
print(json.dumps({"bytes": len(page.encode()), "sections": len(sections), "internal_targets": len(check.targets), "sources": len(source_urls)}, ensure_ascii=False))
