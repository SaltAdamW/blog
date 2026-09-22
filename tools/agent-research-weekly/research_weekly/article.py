"""按文章持久化研究、确认与返修状态；resume 不越过人工确认或触发发布。"""

import datetime as dt
import json
import math
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt

from .article_editor import ArticleEditor, CRITIQUE, NOTE, OUTLINE, SECTION, rules_snapshot
from .codex import choose_blocks, normalized, validate
from .core import atomic_json, atomic_text, canonical_url, digest, run_lock, utcnow
from .fetch import FetchError, fetch_evidence
from .scope import excluded


SLUG = re.compile(r"[a-z][a-z0-9-]{1,69}\Z")
MARKER = re.compile(r"\[source:([a-f0-9]{16})\]")


def checked_slug(value):
    if not SLUG.fullmatch(value):
        raise ValueError("文章 ID 必须为2至70位小写字母、数字或连字符，以字母开头")
    return value


def path_in(root, value):
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError("文件超出文章目录")
    return target


def verify_refs(references, sources):
    for ref in references:
        source = sources.get(ref["source_id"])
        if not source:
            raise ValueError("引用来源不在已读资料中")
        blocks = {b["id"]: b["text"] for b in source["shown"]}
        quote = ref["quote"]
        if not 12 <= len(quote) <= 300 or normalized(quote) not in normalized(blocks.get(ref["block_id"], "")):
            raise ValueError("原文引用未在已读块中逐字匹配")


def critique_ok(review, section_ids):
    validate(review, CRITIQUE)
    if (review["verdict"] == "pass") != (not review["issues"]):
        raise ValueError("审稿通过状态与问题列表矛盾")
    if any(i["section_id"] not in {*section_ids, "all"} for i in review["issues"]):
        raise ValueError("审稿意见引用不存在的章节")
    return review["verdict"] == "pass"


def plain_heading(value):
    if not value.strip() or any(c in value for c in "\n\r<>[]`#\\"):
        raise ValueError("标题为空或含非法 Markdown/HTML")
    return value.strip()


def prose_only(body):
    """使用与博客相同的语法判断代码和正文，避免列表中的围栏逃逸。"""
    env, prose, lines = {}, [], body.splitlines()
    tokens = MarkdownIt("commonmark", {"html": True}).parse(body, env)
    if env.get("references"):
        raise ValueError("正文不能定义外部引用链接")
    for token in walk_tokens(tokens):
        if token.type in ("html_block", "html_inline", "image", "link_open") or (token.type == "heading_open" and token.tag in ("h1", "h2")):
            raise ValueError("正文含外部链接、HTML、图片或越界标题")
        if token.type == "fence":
            fence = token.markup
            if not re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", lines[token.map[1] - 1]):
                raise ValueError("代码围栏未闭合，或不是可独立组装的顶层代码块")
        if token.type in ("fence", "code_block", "code_inline") and "[source:" in token.content:
            raise ValueError("来源标记不能放在代码中")
        if token.type == "text":
            prose.append(token.content)
    return "\n".join(prose)


def walk_tokens(tokens):
    for token in tokens:
        yield token
        yield from walk_tokens(token.children or [])


class Article:
    def __init__(self, config, identifier):
        self.config = config
        base = Path(config["state_dir"]) / "articles"
        self.root = path_in(base, checked_slug(identifier))
        self.state_path = self.root / "state.json"

    def load(self):
        self.state = json.loads(self.state_path.read_text())
        if self.state["version"] != 1:
            raise ValueError("不支持的长文状态版本")
        return self.state

    def save(self, event):
        self.state["updated_at"] = utcnow()
        self.state["history"].append({"at": utcnow(), "event": event, "phase": self.state["phase"]})
        atomic_json(self.state_path, self.state)

    def artifact(self, kind, data, markdown=None):
        name = f"versions/{kind}-{uuid.uuid4().hex[:12]}"
        path = self.root / (name + ".json")
        atomic_json(path, data)
        ref = {"path": str(path.relative_to(self.root)), "sha256": digest(path.read_bytes())}
        if markdown is not None:
            md = self.root / (name + ".md")
            atomic_text(md, markdown)
            ref.update(markdown=str(md.relative_to(self.root)), markdown_sha256=digest(md.read_bytes()))
        return ref

    def read(self, ref):
        path = path_in(self.root, ref["path"])
        if digest(path.read_bytes()) != ref["sha256"]:
            raise ValueError("版本文件已修改；请通过返修入口生成新版本")
        if ref.get("markdown") and digest(path_in(self.root, ref["markdown"]).read_bytes()) != ref["markdown_sha256"]:
            raise ValueError("Markdown 与已登记版本不一致；旧确认不能用于修改后的稿件")
        return json.loads(path.read_text())

    def create(self, topic, *, urls=(), audience=None, skill=None, rounds=3, max_sources=8, repair_passes=2, search=True):
        if not topic.strip() or excluded(topic):
            raise ValueError("主题为空或属于当前项目排除的医学领域")
        if not 1 <= rounds <= 5 or not 1 <= max_sources <= 20 or not 0 <= repair_passes <= 3:
            raise ValueError("预算越界：检索1至5轮，原文1至20篇，自动返修0至3轮")
        with run_lock(self.root):
            if self.state_path.exists():
                raise ValueError("文章已存在，请 resume；不覆盖旧稿")
            rules = rules_snapshot(skill or Path.home() / ".codex/skills/deep-tech-writing")
            self.state = {"version": 1, "id": self.root.name, "phase": "research", "created_at": utcnow(),
                          "brief": {"topic": topic, "audience": audience or "熟悉日常软件操作、对 AI 和技术原理感兴趣，不预设内部架构知识",
                                    "as_of": dt.datetime.now(ZoneInfo(self.config["timezone"])).date().isoformat(),
                                    "exclusions": "不涉及医学及生物医学应用"},
                          "limits": {"rounds": rounds if search else 0, "max_sources": max_sources, "repair_passes": repair_passes},
                          "sources": {}, "rounds": [], "followups": [], "outline": None, "outline_approval": None,
                          "sections": {}, "section_reviews": {}, "feedback": [], "reviews": [], "repairs": 0,
                          "draft": None, "draft_approval": None, "history": [], "runs": []}
            self.state["rules"] = self.artifact("writing-rules", rules)
            for url in urls:
                self.add_candidate({"url": url, "title": url, "focus": topic, "origin": "user"})
            self.save("created")
        return self.status()

    def add_candidate(self, item):
        url = canonical_url(item["url"])
        identifier = digest(url)[:16]
        if identifier in self.state["sources"]:
            if item.get("origin") == "user":
                self.state["sources"][identifier]["origin"] = "user"
            return identifier
        if excluded(item.get("title", "") + " " + item.get("focus", "")):
            return None
        published = item.get("published_at")
        if published:
            published = dt.date.fromisoformat(published[:10]).isoformat()
            if published > self.state["brief"]["as_of"]:
                return None
        self.state["sources"][identifier] = {"id": identifier, "url": url, "title": item.get("title") or url,
                                            "focus": item.get("focus", ""), "published_at": published,
                                            "origin": item.get("origin", "search"), "status": "pending",
                                            "discovery_round": len(self.state["rounds"])}
        return identifier

    def status(self):
        state = self.load()
        return {"id": state["id"], "phase": state["phase"], "topic": state["brief"]["topic"], "directory": str(self.root),
                "sources": {s["url"]: s["status"] for s in state["sources"].values()},
                "rounds": len(state["rounds"]), "limits": state["limits"], "pending_followups": len(state["followups"]),
                "outline": state["outline"], "draft": state["draft"], "error": state.get("error"),
                "latest_review": state["reviews"][-1] if state["reviews"] else None}

    def source_data(self):
        sources = {}
        for key, record in self.state["sources"].items():
            if record["status"] != "read":
                continue
            data = self.read(record["note"])
            evidence = self.read(record["snapshot"])
            raw = path_in(self.root, evidence["raw_path"])
            if digest(raw.read_bytes()) != evidence["sha256"]:
                raise ValueError("原文快照哈希不匹配")
            sources[key] = {**data, "url": record["url"], "title": record["title"], "evidence": evidence}
        return sources

    def packet(self):
        sources = self.source_data()
        packet = {}
        for key, data in sources.items():
            block_ids = {e["block_id"] for claim in data["note"]["claims"] for e in claim["evidence"]}
            packet[key] = {"url": data["url"], "title": data["title"], "note": data["note"],
                           "retrieved_url": data["evidence"].get("retrieved_url"), "snapshot_sha256": data["evidence"]["sha256"],
                           "extraction_notes": data["evidence"].get("notes", []),
                           "blocks": [b for b in data["shown"] if b["id"] in block_ids],
                           "coverage": {"shown": len(data["shown"]), "total": len(data["evidence"]["blocks"])}}
        if len(json.dumps(packet, ensure_ascii=False)) > self.config["codex"]["max_context_chars"]:
            raise ValueError("材料笔记超出上下文预算；请缩小文章范围，不静默截断")
        return packet

    def read_count(self):
        return sum(s["status"] in ("read", "irrelevant") for s in self.state["sources"].values())

    def read_pending(self, editor, fetcher, maximum=None):
        completed = self.read_count()
        maximum = min(maximum or self.state["limits"]["max_sources"], self.state["limits"]["max_sources"])
        pending = [s for s in self.state["sources"].values() if s["status"] == "pending"]
        pending.sort(key=lambda s: ({"user": 0, "original-link": 1}.get(s["origin"], 2), -s.get("discovery_round", 0)))
        for source in pending:
            if completed >= maximum:
                break
            try:
                if source.get("snapshot"):
                    evidence = self.read(source["snapshot"])
                else:
                    evidence = fetcher(source, self.config, self.root)
                    raw = Path(evidence["raw_path"]).resolve()
                    if not raw.is_relative_to(self.root.resolve()) or digest(raw.read_bytes()) != evidence["sha256"]:
                        raise ValueError("原文快照路径或哈希错误")
                    evidence["raw_path"] = str(raw.relative_to(self.root))
                    source["snapshot"] = self.artifact("source-" + source["id"], evidence)
                    self.save("source-fetched:" + source["id"])
                if digest(path_in(self.root, evidence["raw_path"]).read_bytes()) != evidence["sha256"]:
                    raise ValueError("原文快照哈希不匹配")
                budget = self.config["codex"]["max_context_chars"]
                shown = choose_blocks(evidence["blocks"], self.state["brief"]["topic"] + source["focus"], budget)
                if not shown:
                    raise ValueError("未能在预算内读取原文块")
                inventory = [{"id": b["id"], "preview": b["text"][:80]} for b in evidence["blocks"]]
                if len(inventory) > 1500:
                    raise ValueError("原文块索引过大，需要指定具体文档或小节")
                for _ in range(self.config["codex"]["extra_read_rounds"] + 1):
                    note = editor.read_source(self.state["brief"], {**source, "links": evidence["links"],
                        "retrieved_url": evidence.get("retrieved_url"), "extraction_notes": evidence.get("notes", [])}, shown, inventory)
                    validate(note, NOTE)
                    for claim in note["claims"]:
                        if not claim["evidence"]:
                            raise ValueError("材料结论缺少引用")
                        verify_refs([{**e, "source_id": source["id"]} for e in claim["evidence"]], {source["id"]: {"shown": shown}})
                    if not note["requested_blocks"]:
                        break
                    by_id = {b["id"]: b for b in evidence["blocks"]}
                    if not set(note["requested_blocks"]) <= set(by_id):
                        raise ValueError("请求了不存在的原文块")
                    shown = list({b["id"]: b for b in shown + [by_id[k] for k in note["requested_blocks"]]}.values())
                    if sum(len(b["text"]) for b in shown) > budget:
                        raise ValueError("补读超出原文上下文预算")
                if note["requested_blocks"]:
                    raise ValueError("补读预算耗尽，所需原文仍未读完")
                allowed = set()
                for link in evidence["links"]:
                    try:
                        allowed.add(canonical_url(link["url"]))
                    except ValueError:
                        continue
                requested = []
                for url in note["followup_urls"]:
                    try:
                        url = canonical_url(url)
                    except ValueError:
                        continue
                    if url in allowed:
                        requested.append(url)
                note["followup_urls"] = list(dict.fromkeys(requested))
                source["note"] = self.artifact("reading-" + source["id"], {"note": note, "shown": shown})
                source["status"] = "read" if note["relevant"] else "irrelevant"
                source.pop("error", None)
                completed += 1
                if note["relevant"]:
                    for url in note["followup_urls"]:
                        self.add_candidate({"url": url, "focus": self.state["brief"]["topic"], "origin": "original-link"})
                    for gap in note["gaps"]:
                        if gap["status"] == "unread":
                            task = {"query": self.state["brief"]["topic"] + " " + gap["question"], "source_url": source["url"],
                                    "role": "implementation", "reason": gap["question"]}
                            if not any(t["query"] == task["query"] for t in self.state["followups"]):
                                self.state["followups"].append(task)
                self.save("source-read:" + source["id"])
            except (FetchError, ValueError, OSError) as exc:
                source.update(status="needs_evidence", error=str(exc))
                if isinstance(exc, FetchError):
                    source["fetch_failure"] = self.artifact("fetch-failure-" + source["id"], {"error": str(exc), "attempts": exc.attempts})
                self.save("source-gap:" + source["id"])

    def research(self, editor, fetcher):
        def allocation(rounds_left):
            count = self.read_count()
            return count + math.ceil((self.state["limits"]["max_sources"] - count) / max(1, rounds_left))

        self.read_pending(editor, fetcher, allocation(self.state["limits"]["rounds"] - len(self.state["rounds"]) + 1))
        while len(self.state["rounds"]) < self.state["limits"]["rounds"]:
            index = len(self.state["rounds"])
            history = [self.read(r) for r in self.state["rounds"]]
            context = {"items": [{k: s.get(k) for k in ("id", "url", "title", "focus", "published_at", "status", "error")}
                                 for s in self.state["sources"].values()],
                       "rounds": [{k: r.get(k) for k in ("queries", "notes", "followups", "search_complete")} for r in history],
                       "followups": self.state["followups"], "read_materials": self.packet(),
                       "review_feedback": self.state["feedback"]}
            result = editor.discover_article(self.state["brief"], context, index)
            if not result.get("queries"):
                raise ValueError("发现步骤没有真实查询记录")
            for item in result["items"]:
                self.add_candidate(item)
            executed = {normalized(q).lower() for q in result["queries"]}
            pending = [t for t in self.state["followups"] if normalized(t["query"]).lower() not in executed]
            self.state["followups"] = list({t["query"]: t for t in pending + result.get("followups", [])}.values())
            self.state["rounds"].append(self.artifact("search", result))
            self.save("search-round:" + str(index + 1))
            self.read_pending(editor, fetcher, allocation(self.state["limits"]["rounds"] - index))
        # 最后一轮原文中新找到的链接也可补读，但不能越过本篇预算。
        while self.read_count() < self.state["limits"]["max_sources"]:
            before = self.read_count()
            self.read_pending(editor, fetcher)
            if self.read_count() == before:
                break
        if not any(s["status"] == "read" for s in self.state["sources"].values()):
            self.state["phase"] = "needs_evidence"
        else:
            self.state["phase"] = self.state.pop("after_research", "outline")
        self.save("research-checkpoint")

    def outline_markdown(self, outline):
        lines = ["# " + plain_heading(outline["title"]), "", outline["entry"], "", "## 主问题", "", outline["question"], "", outline["answer"], ""]
        for section in outline["sections"]:
            lines += ["## " + plain_heading(section["title"]), ""]
            for label, key in [("建立的认识", "goal"), ("章节关系", "connection"), ("必要机制", "mechanism"),
                               ("设计理由", "design_reason"), ("细节边界", "stop_at")]:
                lines += [f"{label}：{section[key]}", ""]
            lines += ["依据：" + "、".join(f"[原文](<{self.state['sources'][key]['url']}>)" for key in section["source_ids"]), ""]
        lines += ["## 范围与缺口", "", *["- " + s for s in outline["out_of_scope"] + outline["gaps"]], ""]
        return "\n".join(lines)

    def make_outline(self, editor):
        outline = editor.outline(self.state["brief"], self.packet(), self.state["feedback"])
        validate(outline, OUTLINE)
        if any(not outline[k].strip() for k in ("title", "entry", "question", "answer")):
            raise ValueError("框架缺少主问题或核心解释")
        for value in [outline["entry"], outline["question"], outline["answer"], *outline["out_of_scope"], *outline["gaps"]]:
            prose_only(value)
        ids = [s["id"] for s in outline["sections"]]
        if len(set(ids)) != len(ids):
            raise ValueError("框架章节 ID 重复")
        for section in outline["sections"]:
            if not section["source_ids"] or not set(section["source_ids"]) <= set(self.packet()):
                raise ValueError("框架章节缺少已读来源")
            if any(not section[key].strip() for key in ("goal", "connection", "mechanism", "design_reason", "stop_at")):
                raise ValueError("框架缺少解释目标或设计理由")
            for key in ("goal", "connection", "mechanism", "design_reason", "stop_at"):
                prose_only(section[key])
        self.state["outline"] = self.artifact("outline", outline, self.outline_markdown(outline))
        self.state["phase"] = "outline_review"
        self.save("outline-written")

    def validate_section(self, data):
        validate(data, SECTION)
        body = data["body"]
        prose = prose_only(body)
        if re.search(r"https?://|!\[|\]\(|\]\[", prose):
            raise ValueError("正文含未登记 URL 或图片占位")
        if excluded(body):
            raise ValueError("正文涉及项目排除领域")
        sources = self.source_data()
        markers = set(MARKER.findall(prose))
        if "[source:" in MARKER.sub("", body) or re.search(r"\\\[source:", body):
            raise ValueError("来源标记格式错误")
        if not markers or not markers <= set(sources):
            raise ValueError("章节来源标记缺失或引用未读来源")
        rendered = MARKER.sub(lambda m: f"[原文](<{sources[m[1]]['url']}>)", body)
        links = [t for t in walk_tokens(MarkdownIt("commonmark", {"html": True}).parse(rendered)) if t.type == "link_open"]
        if len(links) != len(MARKER.findall(body)):
            raise ValueError("来源标记未能渲染成可点击原文链接")
        refs = []
        for claim in data["claims"]:
            if not claim["passage"].strip() or normalized(claim["passage"]) not in normalized(body):
                raise ValueError("结论无法定位到章节正文")
            if claim["kind"] in ("fact", "author_claim") and not claim["references"]:
                raise ValueError("事实结论没有原文引用")
            refs += claim["references"]
        verify_refs(refs, sources)
        if not {r["source_id"] for r in refs} <= markers:
            raise ValueError("关键结论引用没有展示在正文")

    def draft_sections(self, editor):
        outline = self.read(self.state["outline"])
        if not self.state["outline_approval"] or self.state["outline_approval"]["sha256"] != self.state["outline"]["sha256"]:
            raise ValueError("框架版本未获确认")
        for section in outline["sections"]:
            identifier = section["id"]
            if identifier in self.state["sections"]:
                self.read(self.state["sections"][identifier])
                continue
            previous = self.state.get("previous_sections", {}).get(identifier)
            feedback = [f for f in self.state["feedback"] if f.get("section_id") in (identifier, "all")]
            written = {key: self.read(ref)["body"] for key, ref in self.state["sections"].items()}
            data = editor.draft_section(self.state["brief"], outline, section, self.packet(),
                                        self.read(previous) if previous else None, feedback, written=written)
            self.validate_section(data)
            self.state["sections"][identifier] = self.artifact("section-" + identifier, data, data["body"] + "\n")
            self.save("section-written:" + identifier)
        self.state["phase"] = "review"
        self.save("draft-checkpoint")

    def review(self, editor):
        outline = self.read(self.state["outline"])
        sections = {k: self.read(v) for k, v in self.state["sections"].items()}
        if set(sections) != {s["id"] for s in outline["sections"]}:
            raise ValueError("章节尚未完整，不能审查或组装成稿")
        issues, needs_evidence = [], False
        for key, data in sections.items():
            self.validate_section(data)
            cached = self.state["section_reviews"].get(key)
            if cached and cached["section_hash"] == self.state["sections"][key]["sha256"]:
                result = self.read(cached["review"])
            else:
                result = editor.critique("facts", self.state["brief"], outline, {key: data}, self.packet())
                critique_ok(result, [key])
                ref = self.artifact("facts-" + key, result)
                self.state["section_reviews"][key] = {"section_hash": self.state["sections"][key]["sha256"], "review": ref}
                self.save("facts-reviewed:" + key)
            issues += result["issues"]
            needs_evidence |= result["verdict"] == "needs_evidence"
        explanation = editor.critique("explanation", self.state["brief"], outline, sections, {})
        critique_ok(explanation, list(sections))
        issues += explanation["issues"]
        needs_evidence |= explanation["verdict"] == "needs_evidence"
        self.state["reviews"].append(self.artifact("review", {"issues": issues, "explanation": explanation,
                                                               "sections": self.state["sections"].copy()}))
        self.state["feedback"].extend(issues)
        if not issues:
            self.state["draft"] = self.artifact("article", {"outline": self.state["outline"], "sections": self.state["sections"].copy()}, self.render())
            self.state["phase"] = "awaiting_draft_approval"
        elif needs_evidence:
            self.state["phase"] = "needs_evidence"
        elif any(i["kind"] == "structure" for i in issues):
            self.state["phase"] = "outline_revision_needed"
        elif self.state["repairs"] >= self.state["limits"]["repair_passes"]:
            self.state["phase"] = "revision_needed"
        else:
            self.state["repairs"] += 1
            for issue in issues:
                targets = list(sections) if issue["section_id"] == "all" else [issue["section_id"]]
                for key in targets:
                    old = self.state["sections"].pop(key, None)
                    if old:
                        self.state.setdefault("previous_sections", {})[key] = old
                    self.state["section_reviews"].pop(key, None)
            self.state["phase"] = "drafting"
        self.save("review-checkpoint")

    def render(self):
        outline = self.read(self.state["outline"])
        lines = ["# " + plain_heading(outline["title"]), ""]
        sources = self.source_data()
        for section in outline["sections"]:
            body = self.read(self.state["sections"][section["id"]])["body"]
            body = MARKER.sub(lambda m: f"[原文](<{sources[m[1]]['url']}>)", body)
            lines += ["## " + plain_heading(section["title"]), "", body, ""]
        return "\n".join(lines)

    def resume(self, *, editor_factory=ArticleEditor, fetcher=fetch_evidence):
        with run_lock(self.root):
            self.load()
            active = {"research", "outline", "outline_review", "drafting", "review"}
            if self.state["phase"] not in active:
                return self.status()
            run_dir = self.root / "runs" / uuid.uuid4().hex[:12]
            editor = editor_factory(self.config, run_dir, time.monotonic() + self.config["limits"]["run_seconds"], self.read(self.state["rules"]))
            self.state.pop("error", None)
            self.save("run-started")
            try:
                while self.state["phase"] in active:
                    phase = self.state["phase"]
                    if phase == "research":
                        self.research(editor, fetcher)
                    elif phase == "outline":
                        self.make_outline(editor)
                    elif phase == "outline_review":
                        outline = self.read(self.state["outline"])
                        review = editor.critique("outline", self.state["brief"], outline, {}, self.packet())
                        passed = critique_ok(review, [s["id"] for s in outline["sections"]])
                        self.state["reviews"].append(self.artifact("outline-review", review))
                        self.state["feedback"].extend(review["issues"])
                        self.state["phase"] = ("awaiting_outline_approval" if passed else
                                               "needs_evidence" if review["verdict"] == "needs_evidence" else "outline_revision_needed")
                        self.save("outline-reviewed")
                    elif phase == "drafting":
                        self.draft_sections(editor)
                    else:
                        self.review(editor)
            except BaseException as exc:
                self.state["error"] = f"{type(exc).__name__}: {exc}"
                self.save("interrupted")
                raise
            finally:
                self.state["runs"].append({"path": str(run_dir.relative_to(self.root)), "calls": editor.calls, "usage": editor.usage})
                self.save("run-finished")
        return self.status()

    def approve(self, stage, sha256, note):
        with run_lock(self.root):
            self.load()
            expected = f"awaiting_{stage}_approval"
            if stage not in ("outline", "draft") or self.state["phase"] != expected:
                raise ValueError("当前阶段不能执行该确认")
            ref = self.state[stage]
            self.read(ref)
            if sha256 != ref["sha256"] or not note.strip():
                raise ValueError("确认必须绑定当前完整版本哈希并记录用户指令")
            if stage == "draft":
                self.validate_draft()
            self.state[stage + "_approval"] = {"sha256": sha256, "note": note, "at": utcnow()}
            self.state["phase"] = "drafting" if stage == "outline" else "ready_to_publish"
            self.save("user-approved:" + stage)
        return self.status()

    def revise(self, feedback, section_id=None):
        with run_lock(self.root):
            self.load()
            self.ensure_mutable()
            if not feedback.strip() or self.state["phase"] == "published":
                raise ValueError("返修意见不能为空；已发布文章请新建修订任务")
            if section_id:
                if not self.state["outline_approval"] or section_id not in self.state["sections"]:
                    raise ValueError("待修改章节不存在或框架未确认")
                self.state.setdefault("previous_sections", {})[section_id] = self.state["sections"].pop(section_id)
                self.state["section_reviews"].pop(section_id, None)
                self.state["phase"] = "drafting"
            else:
                self.state["previous_sections"] = self.state["sections"].copy()
                self.state["sections"] = {}
                self.state["section_reviews"] = {}
                self.state["outline_approval"] = None
                self.state["phase"] = "outline"
            self.state.pop("after_research", None)
            self.state["feedback"].append({"section_id": section_id or "all", "change": feedback, "origin": "user"})
            self.state["draft"] = self.state["draft_approval"] = None
            self.state["repairs"] = 0
            self.save("revision-requested")
        return self.status()

    def add_source(self, url):
        with run_lock(self.root):
            self.load()
            self.ensure_mutable()
            if self.state["phase"] == "published":
                raise ValueError("已发布文章请新建任务")
            identifier = self.add_candidate({"url": url, "title": url, "focus": self.state["brief"]["topic"], "origin": "user"})
            if not identifier:
                raise ValueError("来源不在本篇范围内")
            if identifier and self.state["sources"][identifier]["status"] == "needs_evidence":
                self.state["sources"][identifier]["status"] = "pending"
            if self.state["sources"][identifier]["status"] == "pending" and self.read_count() >= self.state["limits"]["max_sources"]:
                raise ValueError("原文阅读预算已满，请先用 research --max-sources 显式扩充，再加入来源")
            self.reopen_research()
            self.save("source-added")
        return self.status()

    def reopen_research(self):
        self.state["draft"] = self.state["draft_approval"] = None
        self.state["section_reviews"] = {}
        self.state["after_research"] = "drafting" if self.state["outline_approval"] else "outline"
        self.state["phase"] = "research"
        self.state["repairs"] = 0

    def extend_research(self, question, rounds=1, max_sources=None):
        with run_lock(self.root):
            self.load()
            self.ensure_mutable()
            if self.state["phase"] == "published" or not question.strip():
                raise ValueError("补查须说明具体问题；已发布文章请新建任务")
            total = self.state["limits"]["rounds"] + rounds
            maximum = max_sources if max_sources is not None else self.state["limits"]["max_sources"]
            if not 0 <= rounds <= 5 or total > 10 or not self.state["limits"]["max_sources"] <= maximum <= 20:
                raise ValueError("每次补查0至5轮、累计最多10轮；原文预算只可增加至20篇")
            if self.read_count() >= maximum:
                raise ValueError("阅读预算已满，需要显式增加 --max-sources 或收窄论点")
            self.state["limits"].update(rounds=total, max_sources=maximum)
            self.state["followups"].append({"query": self.state["brief"]["topic"] + " " + question,
                                            "source_url": "", "role": "implementation", "reason": question})
            self.state["feedback"].append({"section_id": "all", "change": question, "origin": "user-research"})
            self.reopen_research()
            self.save("research-extended")
        return self.status()

    def ensure_mutable(self):
        for path in (self.root / "publication").glob("*/status.json"):
            ledger = json.loads(path.read_text())
            if ledger.get("commit") and ledger["status"] != "verified":
                raise ValueError("已有未完成的发布提交，请先恢复该版本的发布核验，不得返修改写现场")

    def validate_draft(self):
        approval = self.state["outline_approval"]
        if not approval or approval["sha256"] != self.state["outline"]["sha256"]:
            raise ValueError("成稿所用框架未获确认")
        draft = self.read(self.state["draft"])
        if draft["outline"] != self.state["outline"] or draft["sections"] != self.state["sections"]:
            raise ValueError("成稿与当前章节版本不一致")
        if self.render() != path_in(self.root, self.state["draft"]["markdown"]).read_text():
            raise ValueError("成稿渲染内容已变化")
        for ref in self.state["sections"].values():
            self.validate_section(self.read(ref))
        return draft

    def delivery(self):
        self.validate_draft()
        outline = self.read(self.state["outline"])
        used = set()
        for ref in self.state["sections"].values():
            used.update(MARKER.findall(self.read(ref)["body"]))
        sources = self.source_data()
        manifest = {"article_id": self.state["id"], "as_of": self.state["brief"]["as_of"], "sources": [
            {"id": key, "url": sources[key]["url"], "title": sources[key]["title"],
             "published_at": self.state["sources"][key].get("published_at"), "date_status": "candidate_metadata_not_verified",
             "review_status": "primary_sections_read", "sha256": sources[key]["evidence"]["sha256"],
             "retrieved_url": sources[key]["evidence"].get("retrieved_url"),
             "reading_coverage": {"shown_blocks": len(sources[key]["shown"]), "total_blocks": len(sources[key]["evidence"]["blocks"])}}
            for key in sorted(used)]}
        return {"title": outline["title"], "description": outline["answer"], "body": self.render(), "manifest": manifest}


if __name__ == "__main__":
    from .article_cli import main
    main()
