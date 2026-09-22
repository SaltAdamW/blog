"""长文工作流测试使用模拟模型与原文，不调用模型或发布真实文章。"""

import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from research_weekly.article import Article, checked_slug, path_in, prose_only
from research_weekly.article_editor import ArticleEditor
from research_weekly.article_publish import add_post, export, publish
from research_weekly.core import atomic_json, atomic_text, command, digest, run_lock
from research_weekly.discovery import external_search
from research_weekly.fetch import fetch_evidence
from research_weekly.publish import git as local_git
from test_weekly import config_at


QUOTE = "Register the pending query before starting the backend request."
TEXT = QUOTE + " Identical parameters share a result only when callers have the same data permissions. No latency was measured. Later callers wait for the already registered request and receive its result after that request completes. Different permissions can change the result and require a different grouping key."
SEED = "https://example.com/design"
LINK = "https://example.com/implementation"
PASS = {"verdict": "pass", "issues": []}


def fetch(candidate, config, state):
    def transport(url, _settings):
        html = f'<article><h1>Request coalescing</h1><p>{TEXT}</p><a href="{LINK}">Source implementation</a></article>'
        return html.encode(), "text/html", url
    return fetch_evidence(candidate, config, state, transport=transport)


class Editor:
    def __init__(self, config, directory, deadline, rules):
        self.calls, self.usage, self.events = 0, [], []

    def record(self, name, **data):
        self.calls += 1
        self.events.append({"name": name, **copy.deepcopy(data)})

    def discover_article(self, brief, context, index):
        self.record("search", context=context, index=index)
        return {"items": [], "queries": [f"query {index}"], "notes": [], "followups": [], "search_complete": True}

    def read_source(self, brief, candidate, shown, inventory):
        self.record("read", url=candidate["url"])
        block = next(b for b in shown if QUOTE in b["text"])
        return {"summary": "登记先于后端调用，结果共享要求权限相同。", "relevant": True,
                "claims": [{"statement": "先登记正在进行的查询。", "kind": "fact", "limits": "未测量延迟。",
                            "evidence": [{"block_id": block["id"], "quote": QUOTE}]}],
                "gaps": [], "requested_blocks": [], "followup_urls": []}

    def outline(self, brief, packet, feedback):
        self.record("outline", feedback=feedback, packet=packet)
        return {"title": "为什么相同查询可以共享一次工作", "entry": "从同时点击查询进入。", "question": "什么时候可以合并查询？",
                "answer": "先登记未完成查询，让允许共享结果的调用者等待同一次工作。",
                "sections": [{"id": key, "title": title, "goal": "理解等待尚未完成的工作。", "connection": "服务于共享的条件。",
                              "mechanism": "先登记，再访问后端。", "design_reason": "晚登记无法防止并发重复访问。",
                              "stop_at": "不展开缓存系统。", "source_ids": [next(iter(packet))]}
                             for key, title in (("mechanism", "怎样发现还没完成的查询"), ("boundary", "什么时候不能共享"))],
                "out_of_scope": ["分布式缓存"], "gaps": []}

    def draft_section(self, brief, outline, section, packet, previous, feedback, *, written=None):
        self.record("draft", section=section["id"], previous=previous, feedback=feedback, written=written)
        source_id = next(iter(packet))
        block_id = packet[source_id]["blocks"][0]["id"]
        passage = "先登记正在进行的查询，再调用后端。"
        return {"body": f"作为教学例子，两个用户同时点击查询时，结果可能还没有返回。{passage}"
                        "后来的请求才能发现已经有人开始做相同工作，并等待这次工作结束；如果登记太晚，两个请求都可能访问后端。"
                        "共享还要求两个调用者允许获得同一结果。权限不同而结果不同的时候，只比较参数就不够了。"
                        f"这里讨论的是工作如何共享，没有测量响应延迟。[source:{source_id}]",
                "claims": [{"passage": passage, "kind": "fact", "references": [
                    {"source_id": source_id, "block_id": block_id, "quote": QUOTE}]}]}

    def critique(self, name, brief, outline, sections, packet):
        self.record("review", stage=name, sections=list(sections))
        return copy.deepcopy(PASS)


class ArticleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config_at(self.temp.name)
        self.article = Article(self.config, "test-article")
        self.editor = Editor(None, None, None, None)
        # 状态机测试不依赖运行主机安装的个人写作 skill。
        rules = {name: "仅用于状态机测试的写作规则。" for name in (
            "SKILL.md", "references/style-reference.md", "references/draft.md", "references/review.md")}
        patcher = patch("research_weekly.article.rules_snapshot", return_value=rules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, **kwargs):
        return self.article.create("查询合并的机制与边界", urls=[SEED], search=False, **kwargs)

    def resume(self, editor=None, fetcher=fetch):
        return self.article.resume(editor_factory=lambda *_: editor or self.editor, fetcher=fetcher)

    def outline_gate(self):
        self.start()
        status = self.resume()
        self.assertEqual(status["phase"], "awaiting_outline_approval")
        return status

    def draft_gate(self):
        status = self.outline_gate()
        self.article.approve("outline", status["outline"]["sha256"], "测试夹具模拟用户确认框架")
        status = self.resume()
        self.assertEqual(status["phase"], "awaiting_draft_approval")
        return status

    def approved(self):
        status = self.draft_gate()
        self.article.approve("draft", status["draft"]["sha256"], "测试夹具模拟用户确认成稿")
        return status["draft"]["sha256"]

    def test_both_confirmation_gates_are_idle_and_never_publish(self):
        self.start()
        with patch("research_weekly.article_publish.publish") as remote:
            status = self.resume()
            before = self.editor.calls
            self.resume()
            self.assertEqual(self.editor.calls, before)
            self.assertFalse(self.article.state["sections"])
            self.article.approve("outline", status["outline"]["sha256"], "测试确认")
            status = self.resume()
            self.assertEqual(status["phase"], "awaiting_draft_approval")
            before = self.editor.calls
            self.resume()
            self.assertEqual(self.editor.calls, before)
            remote.assert_not_called()
        self.assertIn(SEED, path_in(self.article.root, status["draft"]["markdown"]).read_text())
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_wrong_hash_blank_note_and_out_of_order_approval_fail(self):
        status = self.outline_gate()
        for stage, sha, note in (("outline", "old", "确认"), ("outline", status["outline"]["sha256"], " "),
                                 ("draft", status["outline"]["sha256"], "确认")):
            with self.subTest(stage=stage, note=note), self.assertRaises(ValueError):
                self.article.approve(stage, sha, note)

    def test_snapshot_and_markdown_tampering_invalidates_confirmation(self):
        status = self.draft_gate()
        md = path_in(self.article.root, status["draft"]["markdown"])
        atomic_text(md, md.read_text() + "改动")
        with self.assertRaisesRegex(ValueError, "版本|不一致"):
            self.article.approve("draft", status["draft"]["sha256"], "测试确认")

    def test_raw_tampering_is_not_accepted_as_evidence(self):
        self.outline_gate()
        self.article.load()
        source = next(iter(self.article.source_data().values()))
        atomic_text(self.article.root / source["evidence"]["raw_path"], "tampered")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.article.packet()

    def test_outline_cannot_embed_active_html(self):
        self.start()
        original = self.editor.outline

        def unsafe(*args):
            result = original(*args)
            result["entry"] = "<script>alert(1)</script>"
            return result

        with patch.object(self.editor, "outline", side_effect=unsafe), self.assertRaisesRegex(ValueError, "HTML"):
            self.resume()
        self.assertIsNone(self.article.state["outline"])

    def test_resume_after_section_failure_preserves_finished_work(self):
        status = self.outline_gate()
        self.article.approve("outline", status["outline"]["sha256"], "测试确认")
        original = self.editor.draft_section

        def interrupted(*args, **kwargs):
            if args[2]["id"] == "boundary":
                raise RuntimeError("simulated model interruption")
            return original(*args, **kwargs)

        with patch.object(self.editor, "draft_section", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.resume()
        self.article.load()
        first = self.article.state["sections"]["mechanism"]
        status = self.resume()
        self.assertEqual(status["phase"], "awaiting_draft_approval")
        self.assertEqual(self.article.state["sections"]["mechanism"], first)
        self.assertEqual(sum(e["name"] == "read" for e in self.editor.events), 1)
        self.assertEqual(sum(e["name"] == "draft" and e["section"] == "mechanism" for e in self.editor.events), 1)

    def test_model_budget_failure_keeps_pending_source_for_resume(self):
        self.start()
        with patch.object(self.editor, "read_source", side_effect=RuntimeError("budget exhausted")), self.assertRaises(RuntimeError):
            self.resume()
        self.article.load()
        source = next(iter(self.article.state["sources"].values()))
        self.assertEqual(source["status"], "pending")
        self.assertTrue(source["snapshot"])
        with patch("research_weekly.article.fetch_evidence", side_effect=AssertionError("must reuse snapshot")):
            self.assertEqual(self.resume(fetcher=lambda *_: self.fail("refetched"))["phase"], "awaiting_outline_approval")

    def test_resume_saves_new_run_before_waiting_for_model(self):
        self.start()
        self.article.load()
        self.article.state["error"] = "prior timeout"
        self.article.save("test-error")
        original = self.editor.read_source

        def inspect_state(*args):
            state = json.loads(self.article.state_path.read_text())
            self.assertNotIn("error", state)
            self.assertIn("run-started", [h["event"] for h in state["history"]])
            return original(*args)

        with patch.object(self.editor, "read_source", side_effect=inspect_state):
            self.resume()

    def test_selective_revision_changes_only_target_and_revokes_draft(self):
        self.approved()
        before = copy.deepcopy(self.article.state)
        self.article.revise("补清权限条件", "boundary")
        self.resume()
        after = self.article.state
        self.assertEqual(before["sections"]["mechanism"], after["sections"]["mechanism"])
        self.assertNotEqual(before["sections"]["boundary"], after["sections"]["boundary"])
        self.assertEqual(before["outline_approval"], after["outline_approval"])
        self.assertIsNone(after["draft_approval"])
        latest = [e for e in self.editor.events if e["name"] == "draft"][-1]
        self.assertTrue(latest["previous"])
        self.assertIn("mechanism", latest["written"])
        self.assertIn("补清权限条件", json.dumps(latest["feedback"], ensure_ascii=False))

    def test_structural_revision_needs_new_outline_confirmation(self):
        self.approved()
        old = self.article.state["outline"]["sha256"]
        self.article.revise("主问题改成并发工作如何协调")
        self.assertIsNone(self.article.state["outline_approval"])
        self.assertFalse(self.article.state["sections"])
        self.resume()
        self.assertEqual(self.article.state["phase"], "awaiting_outline_approval")
        # 相同框架 JSON 可以有相同内容哈希，但确认仍已撤销，必须明确重新确认。
        self.assertIsNone(self.article.state["outline_approval"])
        self.assertTrue(old)

    def test_added_source_finishes_missing_sections_before_review(self):
        status = self.outline_gate()
        self.article.approve("outline", status["outline"]["sha256"], "测试确认")
        self.article.add_source(LINK)
        self.assertEqual(self.resume()["phase"], "awaiting_draft_approval")
        self.assertEqual(len(self.article.state["sections"]), 2)
        self.assertEqual(self.article.read_count(), 2)

    def test_added_source_revokes_final_confirmation(self):
        sha = self.approved()
        self.article.add_source(LINK)
        with self.assertRaises(ValueError):
            export(self.article, sha)
        self.resume()
        self.assertIsNone(self.article.state["draft_approval"])

    def test_exhausted_source_budget_requires_explicit_extension(self):
        self.start(max_sources=1)
        self.resume()
        with self.assertRaisesRegex(ValueError, "预算"):
            self.article.add_source(LINK)
        self.article.extend_research("补齐实现", rounds=0, max_sources=2)
        self.article.add_source(LINK)
        self.resume()
        self.assertEqual(self.article.read_count(), 2)
        with self.assertRaisesRegex(ValueError, "最多|预算"):
            self.article.extend_research("补查", max_sources=21)

    def test_multi_round_reading_links_gaps_and_classics_feed_search(self):
        self.article.create("查询合并", urls=[SEED], rounds=3, max_sources=6)
        original = self.editor.read_source

        def note(*args):
            result = original(*args)
            result["followup_urls"] = [LINK, "https://unobserved.example/fabrication"]
            result["gaps"] = [{"question": "缺少实现代码", "status": "unread"},
                              {"question": "作者未披露延迟", "status": "not_disclosed"}]
            return result

        searches = self.editor.discover_article

        def search(brief, context, index):
            result = searches(brief, context, index)
            result["items"] = [{"url": f"https://example.com/round-{index}", "title": "Foundational source", "published_at": "1998-01-01"}]
            return result

        with patch.object(self.editor, "read_source", side_effect=note), patch.object(self.editor, "discover_article", side_effect=search):
            self.resume()
        contexts = [e["context"] for e in self.editor.events if e["name"] == "search"]
        self.assertTrue(contexts[0]["read_materials"])
        self.assertTrue(any(s["url"] == LINK for s in contexts[0]["items"]))
        self.assertTrue(any("缺少实现代码" in t["query"] for t in contexts[0]["followups"]))
        self.assertFalse(any("未披露延迟" in t["query"] for t in contexts[0]["followups"]))
        self.assertFalse(any("unobserved" in s["url"] for s in self.article.state["sources"].values()))
        self.assertTrue(any(s["url"].endswith("round-2") and s["status"] == "read" for s in self.article.state["sources"].values()))

    def test_irrelevant_source_does_not_support_outline(self):
        self.start()
        original = self.editor.read_source

        def irrelevant(*args):
            result = original(*args)
            result.update(relevant=False, claims=[], followup_urls=[LINK])
            return result

        with patch.object(self.editor, "read_source", side_effect=irrelevant):
            self.assertEqual(self.resume()["phase"], "needs_evidence")
        self.assertFalse(self.article.packet())
        self.assertFalse(self.article.state["outline"])

    def test_bounded_repair_and_missing_evidence_preserve_review_feedback(self):
        status = self.outline_gate()
        self.article.approve("outline", status["outline"]["sha256"], "测试确认")

        def critique(name, *_):
            return {"verdict": "needs_evidence", "issues": [{"section_id": "boundary", "kind": "evidence",
                    "problem": "权限条件原文不足", "change": "追查权限实现"}]} if name == "explanation" else copy.deepcopy(PASS)

        with patch.object(self.editor, "critique", side_effect=critique):
            self.assertEqual(self.resume()["phase"], "needs_evidence")
        self.assertIn("追查权限实现", json.dumps(self.article.state["feedback"], ensure_ascii=False))
        self.article.extend_research("权限实现", rounds=1)
        self.resume()
        search = next(e for e in self.editor.events if e["name"] == "search")
        self.assertIn("追查权限实现", json.dumps(search["context"]["review_feedback"], ensure_ascii=False))

    def test_auto_repair_stops_at_budget_without_whole_article_regeneration(self):
        status = self.outline_gate()
        self.article.approve("outline", status["outline"]["sha256"], "测试确认")

        def critique(name, *_):
            return {"verdict": "revise", "issues": [{"section_id": "boundary", "kind": "explanation",
                    "problem": "条件不清", "change": "解释权限为何改变共享条件"}]} if name == "explanation" else copy.deepcopy(PASS)

        with patch.object(self.editor, "critique", side_effect=critique):
            self.assertEqual(self.resume()["phase"], "revision_needed")
        drafted = [e["section"] for e in self.editor.events if e["name"] == "draft"]
        self.assertEqual(drafted.count("mechanism"), 1)
        self.assertEqual(drafted.count("boundary"), 3)

    def test_invalid_citations_and_unsafe_markdown_are_rejected(self):
        self.draft_gate()
        valid = self.article.read(self.article.state["sections"]["mechanism"])
        for suffix in ("<script>alert(1)</script>", "[x](javascript:alert(1))", "\n## other", "\nTitle\n---",
                       "[source:unknown]", "\n```py\n<script>", "\n```bad`info\n<script>\n```",
                       "\n\n- list\n  ```text\n<script>alert(1)</script>\n  ```", "\n> # nested heading"):
            item = copy.deepcopy(valid)
            item["body"] += suffix
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.article.validate_section(item)
        for key, value in (("quote", "invented quote without original evidence"), ("source_id", "0" * 16), ("block_id", "unknown")):
            item = copy.deepcopy(valid)
            item["claims"][0]["references"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.article.validate_section(item)
        valid["body"] += '\n```python\nif a < b:\n    print("https://example.com")\n```\n'
        self.article.validate_section(valid)

    def test_citations_cannot_hide_in_code_or_escaped_markdown(self):
        self.draft_gate()
        data = self.article.read(self.article.state["sections"]["mechanism"])
        marker = f"[source:{next(iter(self.article.source_data()))}]"
        for replacement in ("`" + marker + "`", "\\" + marker, "\n```text\n" + marker + "\n```"):
            item = copy.deepcopy(data)
            item["body"] = item["body"].replace(marker, replacement)
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.article.validate_section(item)

    def test_paths_and_overlapping_runs_fail_closed(self):
        for value in ("../escape", "a/b", "UPPER", "a", "a" * 71):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_slug(value)
        with self.assertRaises(ValueError):
            path_in(self.root, "../escape")
        self.start()
        with run_lock(self.article.root), self.assertRaises(RuntimeError):
            self.resume()

    def test_export_requires_approval_and_excludes_private_evidence(self):
        status = self.draft_gate()
        sha = status["draft"]["sha256"]
        with self.assertRaises(ValueError):
            export(self.article, sha)
        self.article.approve("draft", sha, "测试确认")
        result = export(self.article, sha)
        manifest = (Path(result["directory"]) / "sources.json").read_text()
        self.assertIn(SEED, manifest)
        for private in (str(self.root), "raw_path", "prompt", QUOTE):
            self.assertNotIn(private, manifest)
        self.assertEqual(export(self.article, sha), result)
        atomic_text(Path(result["directory"]) / "article.md", "user modified draft")
        with self.assertRaisesRegex(ValueError, "不覆盖"):
            export(self.article, sha)

    def publication_settings(self):
        self.config["publication"] = {"enabled": True, "repository": "SaltAdamW/blog", "branch": "main",
                                       "site_url": "https://saltadamw.github.io/blog/", "checkout": str(self.root / "blog")}

    def test_publication_guards_precede_any_external_action(self):
        self.start()
        with patch("research_weekly.article_publish.git") as git:
            with self.assertRaisesRegex(ValueError, "confirm"):
                publish(self.article, "bad")
            self.publication_settings()
            with self.assertRaisesRegex(ValueError, "确认"):
                publish(self.article, "bad", confirm=True)
            self.config["publication"]["repository"] = "someone/else"
            with self.assertRaisesRegex(ValueError, "目标"):
                publish(self.article, "bad", confirm=True)
            git.assert_not_called()

    def test_dirty_blog_and_wrong_remote_stop_before_worktree_or_push(self):
        sha = self.approved()
        self.publication_settings()
        for responses, error in ((["https://github.com/SaltAdamW/blog.git", " M draft.md"], "未提交"), (["https://github.com/else/blog.git"], "origin")):
            with patch("research_weekly.article_publish.git", side_effect=responses) as git, self.assertRaisesRegex((ValueError, RuntimeError), error):
                publish(self.article, sha, confirm=True)
            self.assertLessEqual(git.call_count, 2)

    def test_pushed_publication_resumes_verification_without_building_or_pushing(self):
        sha = self.approved()
        self.publication_settings()
        ledger = self.article.root / "publication" / sha / "status.json"
        atomic_json(ledger, {"status": "pushed", "draft_hash": sha, "base": "base", "commit": "new",
                             "worktree": str(self.root / "absent"), "files": {"index.html": "hash"}, "error": "old timeout"})

        def git(_root, *args):
            if args[:2] == ("remote", "get-url"):
                return "https://github.com/SaltAdamW/blog.git"
            if args[0] == "ls-remote":
                return "new\trefs/heads/main"
            if args[0] == "rev-parse":
                return "new"
            self.fail("Unexpected git: " + repr(args))

        with patch("research_weekly.article_publish.git", side_effect=git) as commands, \
             patch("research_weekly.article_publish.command", side_effect=AssertionError("must not build")), \
             patch("research_weekly.article_publish.online_check", return_value={"pages_run": 123, "verified_files": ["index.html"]}) as online:
            result = publish(self.article, sha, confirm=True)
            self.assertEqual(result["status"], "verified")
            self.assertNotIn("error", result)
            before = commands.call_count
            publish(self.article, sha, confirm=True)
            self.assertEqual(commands.call_count, before)
            self.assertEqual(online.call_count, 1)
        self.assertEqual(self.article.status()["phase"], "published")

    def test_new_post_does_not_overwrite_existing_body_or_slug(self):
        self.approved()
        blog = self.root / "blog"
        atomic_json(blog / "posts.json", [{"slug": "other", "source": "old.md"}])
        atomic_text(blog / "old.md", "unchanged original")
        add_post(blog, self.article, self.article.delivery())
        self.assertEqual((blog / "old.md").read_text(), "unchanged original")
        with self.assertRaisesRegex(ValueError, "不覆盖"):
            add_post(blog, self.article, self.article.delivery())

    def test_pending_publication_freezes_revision_and_research(self):
        sha = self.approved()
        atomic_json(self.article.root / "publication" / sha / "status.json", {"status": "pushed", "commit": "test-only"})
        for action in (lambda: self.article.revise("change"), lambda: self.article.add_source(LINK),
                       lambda: self.article.extend_research("more evidence")):
            with self.assertRaisesRegex(ValueError, "未完成的发布"):
                action()

    def test_full_publication_uses_isolated_local_git_and_resumes_failed_readback(self):
        sha = self.approved()
        self.publication_settings()
        root, remote = self.root / "blog", self.root / "remote.git"
        command(["git", "init", "--bare", str(remote)])
        command(["git", "init", "-b", "main", str(root)])
        local_git(root, "config", "user.name", "Local test")
        local_git(root, "config", "user.email", "test@example.invalid")
        atomic_json(root / "posts.json", [])
        atomic_text(root / "original.md", "unchanged")
        local_git(root, "add", ".")
        local_git(root, "commit", "-m", "测试基线")
        local_git(root, "remote", "add", "origin", str(remote))
        local_git(root, "push", "-u", "origin", "main")

        def git(work, *args):
            if args[:3] == ("remote", "get-url", "origin"):
                return "https://github.com/SaltAdamW/blog.git"
            # 实际 origin 是临时本地 bare 仓库；测试不访问真实远端。
            return local_git(work, *args)

        def build(_args, *, cwd, **_kwargs):
            self.assertNotEqual(cwd, root)
            atomic_text(cwd / "index.html", "local generated index")
            atomic_text(cwd / "posts/test-article/index.html", "local generated article")
            return "simulated build", ""

        with patch("research_weekly.article_publish.git", side_effect=git) as calls, \
             patch("research_weekly.article_publish.command", side_effect=build) as build_calls, \
             patch("research_weekly.article_publish.online_check", side_effect=[RuntimeError("simulated readback timeout"), {"pages_run": 1}]):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                publish(self.article, sha, confirm=True)
            self.assertEqual(self.article.status()["phase"], "ready_to_publish")
            ledger_path = self.article.root / "publication" / sha / "status.json"
            ledger = json.loads(ledger_path.read_text())
            self.assertEqual(ledger["status"], "pushed")
            pushed = local_git(remote, "rev-parse", "refs/heads/main")
            self.assertEqual(pushed, ledger["commit"])
            self.assertEqual(local_git(root, "rev-parse", "HEAD"), pushed)
            outcome = publish(self.article, sha, confirm=True)
            self.assertEqual(outcome["status"], "verified")
            self.assertEqual(build_calls.call_count, 4)
            self.assertEqual(sum("push" in c.args for c in calls.call_args_list), 1)
        self.assertEqual((root / "original.md").read_text(), "unchanged")
        self.assertFalse(Path(outcome["worktree"]).exists())

    @unittest.skipUnless(os.environ.get("ARTICLE_BLOG_CHECKOUT"), "显式指定博客检出才运行隔离构建集成测试")
    def test_real_blog_build_accepts_article_in_isolated_clone(self):
        self.approved()
        original = Path(os.environ["ARTICLE_BLOG_CHECKOUT"]).resolve()
        clone = self.root / "isolated-blog"
        before = local_git(original, "rev-parse", "HEAD"), local_git(original, "status", "--porcelain")
        command(["git", "clone", "--quiet", "--no-hardlinks", str(original), str(clone)])
        add_post(clone, self.article, self.article.delivery())
        for args in (["/usr/bin/python3", "build.py"], ["/usr/bin/python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
                     ["node", "--test", "tests/test_analytics.cjs"]):
            command(args, cwd=clone, timeout=120)
        page = (clone / "posts/test-article/index.html").read_text()
        self.assertIn(SEED, page)
        self.assertNotIn("[source:", page)
        self.assertEqual(before, (local_git(original, "rev-parse", "HEAD"), local_git(original, "status", "--porcelain")))


class ModelBoundaryTests(unittest.TestCase):
    def test_source_reading_keeps_evidence_without_unrelated_writing_examples(self):
        with tempfile.TemporaryDirectory() as temp:
            editor = ArticleEditor(config_at(temp), temp, time.monotonic() + 60, {
                "SKILL.md": "WRITING-RULES", "references/style-reference.md": "UNRELATED-STYLE-EXAMPLE",
                "references/draft.md": "DRAFT-GUIDE"})
            shown = [{"id": "p1", "text": TEXT}]
            with patch.object(editor, "ask", return_value={}) as ask:
                editor.read_source({"topic": "coalescing"}, {"id": "source-id", "url": SEED}, shown, [])
            prompt = ask.call_args.args[1]
            self.assertIn(TEXT, prompt)
            self.assertIn("逐字 quote", prompt)
            self.assertNotIn("UNRELATED-STYLE-EXAMPLE", prompt)
            self.assertNotIn("DRAFT-GUIDE", prompt)

    def test_article_external_search_has_no_weekly_date_window(self):
        with tempfile.TemporaryDirectory() as temp:
            response = json.dumps({"content": [{"type": "text", "text": "URL: https://example.com/source"}]})
            with patch("research_weekly.discovery.command", return_value=(response, "")) as command:
                external_search(config_at(temp), None, "2026-09-18", 0, {}, Path(temp), time.monotonic() + 60, historical=True)
            args = command.call_args.args[0]
            payload = json.loads(args[args.index("--args") + 1])
            self.assertNotIn("published between", payload["objective"])
            self.assertIn("foundational", payload["objective"])
            self.assertNotIn("None", payload["query"])

    def test_empty_search_recovers_only_observed_originals(self):
        with tempfile.TemporaryDirectory() as temp:
            editor = ArticleEditor(config_at(temp), temp, time.monotonic() + 60, {})
            external = [{"status": "ok", "query": "observed query", "text": "URL: " + SEED}]
            responses = [{"items": [], "notes": [], "followups": [], "search_complete": False},
                         {"items": [{"url": SEED}, {"url": LINK}], "notes": [], "followups": [], "search_complete": False}]
            with patch("research_weekly.article_editor.external_search", return_value=external), patch.object(editor, "ask", side_effect=responses):
                editor.last_search = [{"action": {"queries": ["native query"]}}]
                result = editor.discover_article({"topic": "coalescing", "as_of": "2026-09-18"}, {}, 0)
            self.assertEqual(result["items"], [{"url": SEED}])
            self.assertEqual(result["queries"], ["native query", "observed query"])
            self.assertFalse(result["search_complete"])

    def test_task_does_not_silently_clip_large_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            config = config_at(temp)
            config["codex"]["max_context_chars"] = 10
            editor = ArticleEditor(config, temp, time.monotonic() + 60, {
                "SKILL.md": "rules", "references/style-reference.md": "style", "references/draft.md": "draft"})
            with patch.object(editor, "ask") as ask, self.assertRaisesRegex(ValueError, "不静默截断"):
                editor.task("test", "instruction", {"body": "x" * 100}, {})
            ask.assert_not_called()

    def test_fence_validation_matches_closing_length_and_character(self):
        self.assertEqual(prose_only("start\n````py\n```\n<tag>\n`````\nend"), "start\nend")
        with self.assertRaises(ValueError):
            prose_only("```py\nvalue\n~~~")


if __name__ == "__main__":
    unittest.main()
