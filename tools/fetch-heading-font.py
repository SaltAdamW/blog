"""按公开文章标题用字下载字体子集，仅更新标题时需要联网执行。"""

from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import re

from markdown_it import MarkdownIt

root = Path(__file__).resolve().parent.parent
tokens = MarkdownIt().parse((root / "article.md").read_text())
headings = "独立运行，不必从零开始。"
for index, token in enumerate(tokens):
    if token.type == "heading_open" and token.tag in {"h1", "h2"}:
        headings += tokens[index + 1].content
text = "".join(sorted(set(headings)))
query = urlencode({"family": "Noto Serif SC:wght@600", "display": "swap", "text": text})
request = Request("https://fonts.googleapis.com/css2?" + query, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"})
with urlopen(request, timeout=30) as response:
    css = response.read().decode()
urls = re.findall(r"url\((https://[^)]+)\) format\('woff2'\)", css)
assert len(urls) == 1, "预期一个 WOFF2 字体子集"
with urlopen(urls[0], timeout=30) as response:
    font = response.read()
assert font[:4] == b"wOF2"
(root / "assets/fonts/heading-serif.woff2").write_bytes(font)
license_url = "https://api.github.com/repos/google/fonts/contents/ofl/notoserifsc/OFL.txt"
license_request = Request(license_url, headers={"Accept": "application/vnd.github.raw+json", "User-Agent": "adam-blog-font-subset"})
with urlopen(license_request, timeout=30) as response:
    license_text = response.read()
assert b"SIL OPEN FONT LICENSE" in license_text
(root / "assets/fonts/OFL.txt").write_bytes(license_text)
print(f"字体子集: {len(text)} 个字符, {len(font)} 字节")
