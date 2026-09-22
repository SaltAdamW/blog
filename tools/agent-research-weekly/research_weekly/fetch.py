import datetime as dt
from html.parser import HTMLParser
import http.client
import ipaddress
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import tempfile
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .core import atomic_json, command, digest, safe_url, utcnow


class FetchError(RuntimeError):
    def __init__(self, message, attempts=None):
        super().__init__(message)
        self.attempts = attempts or []


def public_addresses(host, port):
    addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise FetchError("拒绝访问内网、环回、链路本地或保留地址")
    return sorted(addresses, key=lambda a: ":" in a)


def download(url, settings):
    """逐跳校验并固定 DNS 解析地址，不继承 Cookie、代理凭据或环境中的认证。"""
    deadline = time.monotonic() + settings["timeout_seconds"]
    for _ in range(6):
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise FetchError("下载 URL 非公开 HTTP(S) 地址")
        port = p.port or (443 if p.scheme == "https" else 80)
        if port not in (80, 443):
            raise FetchError("下载端口不在允许范围")
        addresses = public_addresses(p.hostname, port)
        timeout = max(0.1, deadline - time.monotonic())
        cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        kwargs = {"context": ssl.create_default_context()} if p.scheme == "https" else {}
        conn = cls(p.hostname, port, timeout=timeout, **kwargs)
        # HTTPSConnection 仍用原域名做 TLS/SNI 验证，但连接固定到已验证的公网 IP。
        conn._create_connection = lambda address, timeout=None, source_address=None: socket.create_connection((addresses[0], port), timeout, source_address)
        try:
            conn.request("GET", urlunsplit(("", "", p.path or "/", p.query, "")), headers={
                "User-Agent": "AgentResearchWeekly/0.1 (public research reader)", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise FetchError("重定向缺少 Location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise FetchError(f"HTTP {response.status}")
            size = response.getheader("Content-Length")
            if size and int(size) > settings["max_bytes"]:
                raise FetchError("正文超过下载预算")
            pieces, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchError("下载总时间超过预算")
                if conn.sock:
                    conn.sock.settimeout(remaining)
                piece = response.read1(65536)
                if not piece:
                    break
                total += len(piece)
                if total > settings["max_bytes"]:
                    raise FetchError("正文超过下载预算")
                pieces.append(piece)
            return b"".join(pieces), response.getheader("Content-Type", ""), safe_url(url)
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()
    raise FetchError("重定向次数超过预算")


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.preferred = 0
        self.all_text, self.article_text, self.links, self.content_links = [], [], [], []
        self.anchor = None
        self.title = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "nav", "footer", "noscript", "svg"):
            self.hidden.append(tag)
        if tag in ("main", "article"):
            self.preferred += 1
        if tag == "title":
            self.in_title = True
        if tag == "a":
            self.anchor = [attrs.get("href", ""), [], self.preferred > 0 and not self.hidden]
        if tag in ("p", "div", "li", "section", "h1", "h2", "h3", "h4", "tr", "br"):
            self._text("\n")
        if tag in ("td", "th"):
            self._text(" | ")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if tag in ("main", "article"):
            self.preferred = max(0, self.preferred - 1)
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.anchor:
            link = (self.anchor[0], " ".join(self.anchor[1]).strip())
            self.links.append(link)
            if self.anchor[2]:
                self.content_links.append(link)
            self.anchor = None
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "tr"):
            self._text("\n")

    def _text(self, text):
        if not self.hidden:
            self.all_text.append(text)
            if self.preferred:
                self.article_text.append(text)

    def handle_data(self, text):
        self._text(text)
        if self.in_title:
            self.title.append(text)
        if self.anchor:
            self.anchor[1].append(text)

    def text(self):
        preferred = "".join(self.article_text)
        return preferred if len(preferred.strip()) >= 200 else "".join(self.all_text)


def blocks_from_pages(pages):
    blocks = []
    for number, page in enumerate(pages, 1):
        paragraphs = [re.sub(r"[ \t]+", " ", part).strip() for part in re.split(r"\n\s*\n", page)]
        for paragraph in paragraphs:
            # HTML 常用单换行；长 PDF 段落按字符切片，保留页号供模型回看原页。
            for start in range(0, len(paragraph), 2000):
                text = paragraph[start:start + 2000].strip()
                if text:
                    blocks.append({"id": f"p{number}-b{len(blocks)+1}", "page": number, "text": text})
    return blocks


def extract(raw, content_type, url, settings, focus=""):
    if raw.startswith(b"%PDF-"):
        with tempfile.TemporaryDirectory(prefix="weekly-pdf-") as directory:
            pdf = Path(directory) / "source.pdf"
            pdf.write_bytes(raw)
            text, _ = command(["pdftotext", "-layout", str(pdf), "-"], timeout=45, output_limit=6_000_000)
            notes = []
            pages = text.split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
            if len(text.strip()) < 200:
                if not shutil.which("tesseract") or not shutil.which("pdftoppm"):
                    raise FetchError("扫描 PDF 无文本，且缺少 OCR 工具")
                matches = re.findall(r"第\s*(\d+)", focus)
                first = max(1, int(matches[0])) if matches else 1
                last = first + settings["ocr_pages"] - 1
                command(["pdftoppm", "-f", str(first), "-l", str(last), "-scale-to", "1800", "-png", str(pdf), str(Path(directory)/"page")], timeout=90)
                ocr = []
                for image in sorted(Path(directory).glob("page-*.png")):
                    result, _ = command(["tesseract", str(image), "stdout"], timeout=45)
                    number = int(image.stem.split("-")[-1])
                    ocr.append((number, result))
                blocks = []
                for number, result in ocr:
                    for block in blocks_from_pages([result]):
                        block["page"] = number
                        block["id"] = f"p{number}-b{len(blocks)+1}"
                        blocks.append(block)
                notes.append(f"OCR 部分读取，仅覆盖第 {first} 至 {last} 页；摘录需人工核对图像")
            else:
                blocks = blocks_from_pages(pages)
            if sum(len(b["text"]) for b in blocks) < 200:
                raise FetchError("PDF 解析/OCR 后仍缺少可用正文")
            return {"kind": "pdf", "blocks": blocks, "notes": notes, "links": []}
    text = raw.decode("utf-8", errors="replace")
    if "html" in content_type or re.search(r"<(!doctype|html|head|body)\b", text[:1000], re.I):
        parser = ArticleParser()
        parser.feed(text)
        text = parser.text()
        links = [{"url": urljoin(url, href), "title": title} for href, title in parser.links if href]
        kind = "html"
    elif "text/" in content_type:
        links, kind = [], "text"
    else:
        raise FetchError("未知正文类型，拒绝把二进制或错误页当文章")
    if len(text.strip()) < 250 or (len(text) < 1500 and re.search(r"verify you are human|access denied|just a moment|sign in to continue", text, re.I)):
        raise FetchError("正文为空、过短或被登录/反爬页面替代")
    return {"kind": kind, "blocks": blocks_from_pages([text]), "notes": [], "links": links}


def routes(candidate, settings):
    original = candidate["url"]
    result = [original]
    p = urlsplit(original)
    if p.hostname == "arxiv.org" and p.path.startswith("/abs/"):
        identifier = p.path.removeprefix("/abs/")
        result = [f"https://arxiv.org/html/{identifier}", f"https://arxiv.org/pdf/{identifier}", original]
    if p.hostname in ("huggingface.co", "hf-mirror.com") and "/blob/" in p.path and p.path.endswith(".md"):
        result.insert(0, original.replace("/blob/", "/raw/", 1))
    if settings.get("allow_hf_mirror") and p.hostname == "huggingface.co":
        result.append(urlunsplit((p.scheme, "hf-mirror.com", p.path, p.query, "")))
    alternatives = candidate.get("alternatives", [])
    result.extend(json.loads(alternatives) if isinstance(alternatives, str) else alternatives)
    if settings.get("allow_jina"):
        result.append("https://r.jina.ai/" + original)
    return list(dict.fromkeys(result))


def fetch_evidence(candidate, config, state, transport=download):
    settings = config["fetch"]
    queue = [u for u in routes(candidate, settings) if safe_url(u) not in candidate.get("_skip_routes", [])]
    attempts, visited = [], set()
    fallback = None
    while queue and len(visited) < settings["max_routes"]:
        route = queue.pop(0)
        if route in visited:
            continue
        visited.add(route)
        for attempt in range(settings["attempts_per_route"]):
            try:
                raw, mime, final = transport(route, settings)
                data = extract(raw, mime, final, settings, candidate.get("focus", ""))
                entry = {"url": safe_url(route), "final_url": safe_url(final), "attempt": attempt + 1,
                         "status": "ok", "sha256": digest(raw), "bytes": len(raw), "at": utcnow()}
                attempts.append(entry)
                pdfs = [v["url"].replace("/blob/", "/resolve/") for v in data["links"]
                        if urlsplit(v["url"]).path.lower().endswith(".pdf") or
                        (urlsplit(v["url"]).hostname == "arxiv.org" and urlsplit(v["url"]).path.startswith("/pdf/"))]
                abstract_page = urlsplit(final).hostname == "arxiv.org" and urlsplit(final).path.startswith("/abs/")
                if abstract_page or (data["kind"] != "pdf" and pdfs and any(term in candidate["title"].lower() for term in ("report", "报告", "paper"))):
                    fallback = (raw, data, final)
                    queue = [p for p in pdfs[:2] if p not in visited] + queue
                    break
                return save_evidence(candidate, raw, data, final, attempts, state)
            except (FetchError, RuntimeError, OSError, ValueError) as exc:
                attempts.append({"url": safe_url(route), "attempt": attempt + 1, "status": "failed", "error": str(exc)[:1200], "at": utcnow()})
    if fallback:
        # 找到了报告落地页但没拿到其 PDF 时，不能用落地页冒充完整报告。
        raise FetchError("已取得报告落地页，但正文 PDF 所有路径均失败", attempts)
    raise FetchError("本轮全部获取路径失败，保留候选并等待补证", attempts)


def save_evidence(candidate, raw, data, final, attempts, state):
    sha = digest(raw)
    directory = Path(state) / "evidence" / sha
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_path = directory / ("source.pdf" if data["kind"] == "pdf" else "source.html" if data["kind"] == "html" else "source.txt")
    raw_path.write_bytes(raw)
    evidence = {**data, "sha256": sha, "original_url": candidate["url"], "retrieved_url": safe_url(final),
                "retrieved_at": utcnow(), "raw_path": str(raw_path), "attempts": attempts}
    atomic_json(directory / "evidence.json", evidence)
    return evidence


def discover_source(source, config, transport=download):
    if source["kind"] == "url":
        return [{"url": source["url"], "title": source["name"], "origin": source["name"]}]
    for attempt in range(config["fetch"]["attempts_per_route"]):
        try:
            raw, mime, final = transport(source["url"], config["fetch"])
            break
        except (OSError, FetchError):
            if attempt + 1 == config["fetch"]["attempts_per_route"]:
                raise
    items = []
    if source["kind"] == "rss":
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise FetchError("不接受含 DTD/ENTITY 的 feed")
        root = ET.fromstring(raw)
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
                continue
            fields = {e.tag.rsplit("}", 1)[-1]: e for e in element}
            link = fields.get("link")
            if link is None:
                continue
            url = link.get("href") or link.text
            if not url:
                continue
            date = None
            for key in ("published", "pubDate", "updated"):
                if key in fields and fields[key].text:
                    value = fields[key].text
                    try:
                        date = dt.date.fromisoformat(value[:10]).isoformat()
                    except ValueError:
                        from email.utils import parsedate_to_datetime
                        date = parsedate_to_datetime(value).date().isoformat()
                    break
            title = fields["title"].text if "title" in fields else url
            items.append({"url": urljoin(final, url), "title": title, "published_at": date, "origin": source["name"]})
    elif source["kind"] == "index":
        parser = ArticleParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        for links in (parser.content_links, parser.links):
            for href, title in links:
                url = urljoin(final, href)
                if (urlsplit(url).hostname == urlsplit(final).hostname
                        and source["path_contains"] in urlsplit(url).path
                        and urlsplit(url).path.rstrip("/") != urlsplit(final).path.rstrip("/")):
                    # 部分站点的整卡链接没有锚文本；保留 URL 作元数据，正文审稿时再确定标题。
                    items.append({"url": url, "title": title or url, "origin": source["name"]})
            # 正文没有匹配项时，才在导航里寻找配置明确限定的路径。
            if items:
                break
    else:
        raise ValueError("未知信源类型")
    unique = {item["url"]: item for item in items}
    if not unique:
        raise FetchError("信源返回零条候选，不能据此声称无更新")
    return list(unique.values())[:config["limits"]["per_source"]]
