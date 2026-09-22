import copy
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from research_weekly.cli import main, schedule_files
from research_weekly.codex import Codex, REVIEW_SCHEMA, choose_blocks, validate, verify_review
from research_weekly.core import ROOT, Store, canonical_url, command, load_config, run_lock
from research_weekly.fetch import ArticleParser, FetchError, blocks_from_pages, discover_source, fetch_evidence, public_addresses, routes
from research_weekly.pipeline import due, run, select_queue

PARAGRAPH = "The root coordinates the review while workers read documents. This is a controlled test with explicit limitations. " * 5


def config_at(directory):
    config = load_config(ROOT / ("config.json" if (ROOT / "config.json").exists() else "config.example.json"))
    config["state_dir"] = directory
    config["sources"] = []
    config["codex"]["search"] = False
    config["publication"] = {"enabled": False}
    config["discovery"] = {"min_rounds": 3, "max_rounds": 3, "stagnant_rounds": 2}
    return config


def result(block_id="p1-b1"):
    return {"decision": "select", "reason": "解释了任务划分的机制和边界", "requested_blocks": [], "published_at": None, "date_evidence": [], "findings": [{
        "key": "root-coordination", "title": "任务划分影响覆盖", "category": "framework",
        "conclusion": "任务划分需要单独评估。", "problem": "资料覆盖不完整。", "mechanism": "root 划分工作。",
        "evidence": [{"block_id": block_id, "quote": "The root coordinates the review while workers read documents."}],
        "limitations": ["测试材料不代表所有任务。"], "implication": "应分别验证划分策略。", "experiment": "固定材料对照策略。",
        "paragraphs": ["任务拆分影响材料覆盖。", "主管划分工作，执行者读取文档。", "该测试不能推广到所有任务。"]
    }]}


class FakeEditor:
    def __init__(self, config, directory, deadline):
        self.calls, self.usage = 0, []

    def review(self, candidate, evidence, feedback):
        self.calls += 1
        answer = result(next(b["id"] for b in evidence["blocks"] if "The root coordinates" in b["text"]))
        verify_review(answer, evidence["blocks"])
        answer["audit_verified"] = True
        return answer

    def edit(self, findings, history, feedback):
        self.calls += 1
        return {"selected": [f["id"] for f in findings], "excluded": []}

    def prioritize(self, candidates, maximum):
        return {"priority": [{"id": c["id"], "reason": "模拟排序"} for c in candidates[:maximum]]}


def html_transport(url, settings):
    return (f"<html><article><h1>Research</h1><p>{PARAGRAPH}</p></article></html>".encode(), "text/html", url)


def fake_fetch(candidate, config, state):
    return fetch_evidence(candidate, config, state, transport=html_transport)


class URLTests(unittest.TestCase):
    def test_canonical_tracking_and_fragment(self):
        self.assertEqual(canonical_url("https://EXAMPLE.com/a?utm_source=x&b=2#a"), "https://example.com/a?b=2")

    def test_hf_blob_pdf(self):
        self.assertEqual(canonical_url("https://huggingface.co/a/b/blob/main/report.pdf"), "https://huggingface.co/a/b/resolve/main/report.pdf")

    def test_credentials_and_protocol_rejected(self):
        for url in ("file:///etc/passwd", "https://u:p@example.com/a", "https://example.com/?token=secret", "http://example.com:8123/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                canonical_url(url)

    def test_dns_private_mixed_and_ipv6(self):
        for addresses in (["127.0.0.1"], ["169.254.169.254"], ["::1"], ["::ffff:127.0.0.1"], ["8.8.8.8", "10.0.0.1"]):
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]
            with self.subTest(addresses=addresses), patch("socket.getaddrinfo", return_value=records), self.assertRaises(FetchError):
                public_addresses("example.com", 443)


class FetchTests(unittest.TestCase):
    def test_arxiv_abstract_is_not_treated_as_full_paper(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = {"url": "https://arxiv.org/abs/2609.00000", "title": "A research finding"}
            config = config_at(directory)
            self.assertEqual(routes(candidate, config["fetch"])[0], "https://arxiv.org/html/2609.00000")

            def transport(url, settings):
                if "/abs/" not in url:
                    raise FetchError("full text unavailable")
                return html_transport(url, settings)

            with self.assertRaisesRegex(FetchError, "落地页"):
                fetch_evidence(candidate, config, directory, transport)

    def test_index_ignores_navigation_when_main_present(self):
        raw = b'<html><nav><a href="/blog/ad">Ad</a></nav><main><a href="/blog/research">Research</a></main></html>'
        source = {"kind": "index", "url": "https://example.com/", "name": "Research", "path_contains": "/blog/"}
        items = discover_source(source, config_at("/tmp/not-created"), lambda *_: (raw, "text/html", source["url"]))
        self.assertEqual([i["title"] for i in items], ["Research"])

    def test_index_keeps_empty_card_links_and_ignores_pagination(self):
        raw = b'<main><a href="/blog/article"></a><a href="/blog?page=2">2</a></main>'
        source = {"kind": "index", "url": "https://example.com/blog", "name": "Research", "path_contains": "/blog/"}
        items = discover_source(source, config_at("/tmp/not-created"), lambda *_: (raw, "text/html", source["url"]))
        self.assertEqual([i["url"] for i in items], ["https://example.com/blog/article"])

    def test_index_falls_back_to_filtered_navigation_when_main_has_no_match(self):
        raw = b'<nav><a href="/news/research">News</a><a href="/docs">Docs</a></nav><main><a href="/api">API</a></main>'
        source = {"kind": "index", "url": "https://example.com/", "name": "Research", "path_contains": "/news/"}
        items = discover_source(source, config_at("/tmp/not-created"), lambda *_: (raw, "text/html", source["url"]))
        self.assertEqual([i["url"] for i in items], ["https://example.com/news/research"])

    def test_source_download_retries_transient_failure(self):
        calls = []

        def transport(url, config):
            calls.append(url)
            if len(calls) == 1:
                raise FetchError("timeout")
            return b'<rss><channel><item><link>https://example.com/a</link></item></channel></rss>', "text/xml", url

        items = discover_source({"kind": "rss", "name": "source", "url": "https://example.com/feed"},
                                config_at("/tmp/not-created"), transport)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 2)

    def test_fallback_and_evidence_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_at(directory)
            urls = []

            def transport(url, settings):
                urls.append(url)
                if "huggingface.co" in url:
                    raise FetchError("connect timeout")
                return html_transport(url, settings)

            candidate = {"url": "https://huggingface.co/a/b/resolve/main/x", "title": "research", "focus": "", "alternatives": "[]"}
            evidence = fetch_evidence(candidate, config, directory, transport)
            self.assertEqual(len(urls), 3)
            self.assertEqual([a["status"] for a in evidence["attempts"]], ["failed", "failed", "ok"])
            self.assertTrue(Path(evidence["raw_path"]).exists())
            self.assertIn("hf-mirror.com", evidence["retrieved_url"])

    def test_all_routes_fail_preserves_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_at(directory)
            candidate = {"url": "https://example.com/a", "title": "Research", "alternatives": ["https://other.example/a"]}
            with self.assertRaises(FetchError) as caught:
                fetch_evidence(candidate, config, directory, lambda *_: (_ for _ in ()).throw(FetchError("timeout")))
            self.assertEqual(len(caught.exception.attempts), 4)

    def test_html_only_article_and_entities(self):
        p = ArticleParser()
        p.feed(f"<html><nav>navigation</nav><article>{PARAGRAPH}&amp; evidence<script>bad instruction</script></article><footer>footer</footer></html>")
        self.assertNotIn("navigation", p.text())
        self.assertNotIn("bad instruction", p.text())
        self.assertIn("& evidence", p.text())

    def test_pdf_magic_not_mime(self):
        from research_weekly.fetch import extract
        with patch("research_weekly.fetch.command", return_value=(PARAGRAPH + "\f" + PARAGRAPH, "")):
            data = extract(b"%PDF-1.5 fake", "application/octet-stream", "https://example.com/a", {"ocr_pages": 4})
        self.assertEqual(data["kind"], "pdf")
        self.assertEqual({b["page"] for b in data["blocks"]}, {1, 2})

    def test_report_landing_page_is_not_full_report(self):
        with tempfile.TemporaryDirectory() as directory:
            def transport(url, settings):
                if url.endswith(".pdf"):
                    raise FetchError("timeout")
                return (f'<html><article>{PARAGRAPH}<a href="report.pdf">Technical Report</a></article></html>'.encode(), "text/html", url)
            with self.assertRaisesRegex(FetchError, "落地页"):
                fetch_evidence({"url": "https://example.com/a", "title": "Technical Report"}, config_at(directory), directory, transport)

    def test_public_proxy_opt_in(self):
        config = config_at("/tmp/not-created")
        candidate = {"url": "https://example.com/a", "alternatives": []}
        self.assertFalse(any("jina.ai" in u for u in routes(candidate, config["fetch"])))

    def test_rss_and_atom_dates(self):
        config = config_at("/tmp/not-created")
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="/a"/><published>2026-09-17T00:00:00Z</published></entry></feed>'
        items = discover_source({"kind": "rss", "url": "https://example.com/feed", "name": "DSH"}, config, lambda *_: (atom, "text/xml", "https://example.com/feed"))
        self.assertEqual(items[0]["published_at"], "2026-09-17")
        self.assertEqual(items[0]["url"], "https://example.com/a")

    def test_empty_or_entity_feed_is_failure(self):
        config = config_at("/tmp/not-created")
        for raw in (b"<rss/>", b'<!DOCTYPE rss><rss/>'):
            with self.assertRaises(FetchError):
                discover_source({"kind": "rss", "url": "https://example.com/feed", "name": "X"}, config, lambda *_: (raw, "text/xml", "https://example.com/feed"))


class ReviewTests(unittest.TestCase):
    def test_separate_fact_audit_and_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_at(directory)

            class Scripted(Codex):
                def __init__(self):
                    super().__init__(config, directory)
                    self.names = []
                    self.answers = iter([result(), {"verified": False, "issues": ["请限定条件"]}, result(), {"verified": True, "issues": []}])

                def ask(self, name, prompt, schema, search=False):
                    self.names.append(name)
                    if name.startswith("audit"):
                        self.asserted_context = "Document identity background" in prompt
                    return next(self.answers)

            editor = Scripted()
            candidate = {"id": "test", "title": "Research", "url": "https://example.com/a", "published_at": None, "focus": ""}
            evidence = {"blocks": [{"id": "p1-b1", "page": 1, "text": PARAGRAPH},
                                   {"id": "p1-b2", "page": 1, "text": "Document identity background"}],
                        "notes": [], "retrieved_url": candidate["url"]}
            answer = editor.review(candidate, evidence, [])
            self.assertTrue(answer["audit_verified"])
            self.assertTrue(editor.asserted_context)
            self.assertEqual(editor.names, ["review-test", "audit-test", "review-test", "audit-test"])

    def test_unresolved_fact_audit_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_at(directory)

            class Scripted(Codex):
                def ask(self, name, prompt, schema, search=False):
                    return {"verified": False, "issues": ["结论不受证据支持"]} if name.startswith("audit") else result()

            editor = Scripted(config, directory)
            candidate = {"id": "test", "title": "Research", "url": "https://example.com/a", "published_at": None, "focus": ""}
            evidence = {"blocks": [{"id": "p1-b1", "page": 1, "text": PARAGRAPH}], "notes": [], "retrieved_url": candidate["url"]}
            with self.assertRaisesRegex(ValueError, "事实复核"):
                editor.review(candidate, evidence, [])

    def test_citations_match_and_invalid_quote_rejected(self):
        blocks = [{"id": "p1-b1", "page": 1, "text": PARAGRAPH}]
        verify_review(result(), blocks)
        answer = result()
        answer["findings"][0]["evidence"][0]["quote"] = "A fabricated statistic of 99.9 percent."
        with self.assertRaisesRegex(ValueError, "逐字匹配"):
            verify_review(answer, blocks)

    def test_unread_block_rejected(self):
        with self.assertRaises(ValueError):
            verify_review(result("p36-b9"), [{"id": "p1-b1", "text": PARAGRAPH}])

    def test_select_requires_findings_and_limits(self):
        for change in ({"findings": []}, {"requested_blocks": ["p1-b1"]}, {"decision": "reject"}):
            answer = {**result(), **change}
            with self.assertRaises(ValueError):
                verify_review(answer, [{"id": "p1-b1", "text": PARAGRAPH}])

    def test_extra_fields_rejected(self):
        with self.assertRaises(ValueError):
            validate({**result(), "command": "run something"}, REVIEW_SCHEMA)

    def test_late_report_section_survives_context_selection(self):
        blocks = [{"id": f"p{i}-b{i}", "page": i, "text": "Architecture unrelated details. " * 20} for i in range(1, 51)]
        blocks[35]["text"] = "5.3.5 Multi-Agent collaboration reward latency orchestration " * 10
        shown = choose_blocks(blocks, "multi-agent", 4000)
        self.assertIn("p36-b36", [b["id"] for b in shown])
        self.assertLessEqual(sum(len(b["text"]) for b in shown), 4000)


class StateTests(unittest.TestCase):
    def test_origin_merge_and_no_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            a = store.add({"url": "https://example.com/a?utm_source=x", "origin": "codex"})
            b = store.add({"url": "https://example.com/a", "origin": "dsh"})
            self.assertEqual(a, b)
            self.assertEqual(json.loads(store.get(a)["origins"]), ["codex", "dsh"])
            self.assertEqual(len(store.list()), 1)
            store.close()

    def test_lock_excludes_concurrent_run(self):
        with tempfile.TemporaryDirectory() as directory, run_lock(directory):
            with self.assertRaises(RuntimeError), run_lock(directory):
                pass

    def test_retry_queue_does_not_starve_new_candidates(self):
        retries = [{"id": i, "status": "needs_evidence"} for i in range(10)]
        others = [{"id": 20, "status": "pending"}]
        self.assertIn(others[0], select_queue(retries + others, 4))

    def test_command_timeout(self):
        with self.assertRaisesRegex(RuntimeError, "超时"):
            command(["sleep", "20"], timeout=0.1)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = config_at(self.temp.name)

    def test_schedule_uses_user_home_without_inheriting_arbitrary_path(self):
        with patch("research_weekly.cli.Path.home", return_value=Path("/home/reviewer")), \
             patch.dict(os.environ, {"PATH": ":.:relative:/tmp/untrusted"}):
            units = schedule_files(self.config)
        for name, body in units.items():
            if name.endswith(".service"):
                self.assertIn('Environment="PATH=/home/reviewer/.local/bin:/usr/local/bin:/usr/bin:/bin"', body)
                self.assertNotIn("/root/.local/bin", body)
                self.assertNotIn("/tmp/untrusted", body)

    def test_schedule_root_path_and_other_unit_content_stay_unchanged(self):
        with patch("research_weekly.cli.Path.home", return_value=Path("/root")):
            root_units = schedule_files(self.config)
        with patch("research_weekly.cli.Path.home", return_value=Path("/home/reviewer")):
            user_units = schedule_files(self.config)
        for name, body in root_units.items():
            self.assertEqual(body, user_units[name].replace("/home/reviewer/.local/bin", "/root/.local/bin"))
            if name.endswith(".service"):
                self.assertIn('Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"', body)

    def test_schedule_escapes_user_home_in_environment(self):
        with patch("research_weekly.cli.Path.home", return_value=Path("/home/reviewer 50%")):
            units = schedule_files(self.config)
        self.assertIn('Environment="PATH=/home/reviewer 50%%/.local/bin:/usr/local/bin:/usr/bin:/bin"',
                      units["agent-research-weekly.service"])

    def doctor(self, markdown_available):
        with patch("research_weekly.cli.load_config", return_value=self.config), \
             patch("research_weekly.cli.Path.home", return_value=self.home), \
             patch("research_weekly.cli.Path.read_text", side_effect=AssertionError("doctor 不应读取 skill 内容")), \
             patch("research_weekly.cli.shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=object() if markdown_available else None), \
             patch("research_weekly.cli.Codex") as model, \
             patch("research_weekly.cli.Store") as store, \
             patch("research_weekly.cli.print_json") as output:
            main(["doctor"])
        model.assert_not_called()
        store.assert_not_called()
        return output.call_args.args[0]

    def test_doctor_missing_article_dependencies_are_optional(self):
        result = self.doctor(markdown_available=False)
        article = result["optional_dependencies"]["article"]
        self.assertFalse(article["required_for_weekly"])
        self.assertFalse(article["markdown_it_available"])
        self.assertFalse(article["skill_available"])
        self.assertEqual(article["skill_path"], str(self.home / ".codex/skills/deep-tech-writing"))
        self.assertEqual(article["missing_skill_files"],
                         ["SKILL.md", "references/style-reference.md", "references/draft.md", "references/review.md"])
        self.assertFalse(result["real_model_verified"])
        self.assertFalse((self.home / ".codex").exists())

    def test_doctor_checks_complete_article_skill_without_reading_contents(self):
        skill = self.home / ".codex/skills/deep-tech-writing"
        (skill / "references").mkdir(parents=True)
        for name in ("SKILL.md", "references/style-reference.md", "references/draft.md"):
            (skill / name).write_text("private fixture content")
        result = self.doctor(markdown_available=True)
        article = result["optional_dependencies"]["article"]
        self.assertTrue(article["markdown_it_available"])
        self.assertFalse(article["skill_available"])
        self.assertEqual(article["missing_skill_files"], ["references/review.md"])
        (skill / "references/review.md").write_text("private fixture content")
        result = self.doctor(markdown_available=True)
        self.assertTrue(result["optional_dependencies"]["article"]["skill_available"])
        self.assertEqual(result["optional_dependencies"]["article"]["missing_skill_files"], [])
        self.assertNotIn("private fixture content", json.dumps(result))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = config_at(self.temp.name)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.identifier = self.store.add({"url": "https://example.com/article", "title": "Research", "published_at": "2025-06-23"})

    def execute(self, **kwargs):
        return run(self.config, discover=False, editor_factory=FakeEditor, fetcher=fake_fetch, **kwargs)

    def test_end_to_end_idempotency_and_unverified_date_label(self):
        result1 = self.execute()
        self.assertEqual(result1["status"], "complete")
        self.assertEqual(result1["selected"], 1)
        self.assertIn("发布日期未核实", Path(result1["report"]).read_text())
        self.assertNotIn("## 经典补课", Path(result1["report"]).read_text())
        result2 = self.execute()
        self.assertEqual(result2["status"], "already_done")
        self.assertEqual(result1["report"], result2["report"])
        result3 = self.execute(force=True, only=[self.identifier])
        self.assertEqual(result3["selected"], 0)

    def test_fetch_failure_stays_pending_with_audit(self):
        def failure(*_):
            raise FetchError("network failed", [{"status": "failed", "url": "https://example.com/article"}])
        outcome = run(self.config, discover=False, editor_factory=FakeEditor, fetcher=failure)
        self.assertEqual(outcome["status"], "partial")
        candidate = self.store.get(self.identifier)
        self.assertEqual(candidate["status"], "needs_evidence")
        self.assertTrue(candidate["retry_at"])
        record = Path(self.temp.name)/"runs"/outcome["run_id"]/f"failure-{self.identifier}.json"
        self.assertTrue(json.loads(record.read_text())["attempts"])

    def test_successful_retry_recovers_failure(self):
        def failure(*_):
            raise FetchError("network failed")
        run(self.config, discover=False, editor_factory=FakeEditor, fetcher=failure)
        outcome = self.execute(force=True, only=[self.identifier])
        self.assertEqual(outcome["selected"], 1)
        self.assertIsNone(self.store.get(self.identifier)["retry_at"])

    def test_changed_snapshot_not_silently_used(self):
        outcome = self.execute()
        candidate = self.store.get(self.identifier)
        path = Path(self.temp.name)/"evidence"/candidate["evidence_hash"]/"source.html"
        path.write_text("tampered")
        result2 = self.execute(force=True, only=[self.identifier])
        self.assertEqual(result2["status"], "partial")
        self.assertIn("hash", self.store.get(self.identifier)["reason"])

    def test_source_failure_visible_not_no_news(self):
        self.config["sources"] = [{"name": "blocked-source"}]
        outcome = run(self.config, editor_factory=FakeEditor, fetcher=fake_fetch,
                      source_reader=lambda *_: (_ for _ in ()).throw(FetchError("403")))
        self.assertEqual(outcome["status"], "partial")
        self.assertIn("blocked-source", Path(outcome["report"]).read_text())

    def test_sample_does_not_suppress_weekly_discovery(self):
        sample = self.execute()
        self.assertEqual(sample["scope"], "queue")
        self.assertIn("补充报告", Path(sample["report"]).read_text())
        calls = []
        self.config["sources"] = [{"name": "new-source", "kind": "rss"}]

        def reader(source, config):
            calls.append(source["name"])
            return [{"url": "https://new.example/a", "title": "New research"}]

        weekly = run(self.config, editor_factory=FakeEditor, fetcher=fake_fetch, source_reader=reader)
        self.assertEqual(calls, ["new-source"])
        self.assertEqual(weekly["scope"], "weekly")
        self.assertEqual(weekly["discovered"], 1)
        self.assertEqual(weekly["new_candidates"], 1)
        self.assertEqual(weekly["reviewed"], 1)
        self.assertEqual(weekly["source_results"][0]["status"], "ok")
        self.assertTrue((Path(self.temp.name) / "reports" / "latest-weekly.md").exists())

    def test_changed_sources_invalidate_weekly_reuse(self):
        first = run(self.config, editor_factory=FakeEditor, fetcher=fake_fetch)
        self.config["sources"] = [{"name": "added", "kind": "rss"}]
        second = run(self.config, editor_factory=FakeEditor, fetcher=fake_fetch,
                     source_reader=lambda *_: [{"url": "https://added.example/a"}])
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_supplement_does_not_replace_latest_weekly(self):
        weekly = run(self.config, editor_factory=FakeEditor, fetcher=fake_fetch)
        original = Path(weekly["report"]).read_text()
        supplement = self.execute(force=True, only=[self.identifier])
        self.assertEqual(supplement["scope"], "targeted")
        reports = Path(self.temp.name) / "reports"
        self.assertEqual((reports / "latest-weekly.md").read_text(), original)
        self.assertIn("补充报告", (reports / "latest-supplement.md").read_text())

    def test_coverage_distinguishes_duplicates_date_filter_and_failure(self):
        self.config["sources"] = [{"name": name, "kind": "rss"} for name in ("a", "b", "failed")]
        self.config["codex"]["search"] = True

        class SearchEditor(FakeEditor):
            def discover(self, *_):
                return {"search_complete": True, "notes": [], "items": [
                    {"url": "https://new.example/item", "origin": "codex-search"}]}

        def reader(source, config):
            if source["name"] == "failed":
                raise FetchError("403")
            return [{"url": "https://new.example/item"},
                    {"url": "https://old.example/item", "published_at": "2020-01-01"}]

        outcome = run(self.config, editor_factory=SearchEditor, fetcher=fake_fetch, source_reader=reader)
        self.assertEqual(outcome["discovered"], 1)
        self.assertEqual(outcome["new_candidates"], 1)
        self.assertEqual(outcome["reviewed"], 1)
        self.assertEqual(outcome["review_counts"]["select"], 1)
        self.assertEqual([s["status"] for s in outcome["source_results"]], ["ok", "ok", "failed", "ok", "ok", "ok"])
        self.assertEqual(outcome["source_results"][0]["collected"], 2)
        self.assertEqual(outcome["source_results"][0]["eligible"], 1)
        self.assertEqual(outcome["source_results"][1]["added"], 0)
        self.assertIn("去重候选 1", Path(outcome["report"]).read_text())
        self.assertIn("新增入库 |\n| ---", Path(outcome["report"]).read_text())

    def test_fixed_url_is_not_counted_as_successful_fetch(self):
        self.config["sources"] = [{"name": "fixed", "kind": "url", "url": "https://fixed.example/a"}]
        outcome = run(self.config, editor_factory=FakeEditor, fetcher=fake_fetch)
        self.assertEqual(outcome["source_results"][0]["status"], "direct")

    def test_review_failure_is_not_counted_as_completed_reading(self):
        outcome = run(self.config, discover=False, editor_factory=FakeEditor,
                      fetcher=lambda *_: (_ for _ in ()).throw(FetchError("timeout")))
        self.assertEqual(outcome["processed"], 1)
        self.assertEqual(outcome["reviewed"], 0)

    def test_editor_failure_does_not_publish(self):
        class BrokenEditor(FakeEditor):
            def edit(self, *args):
                raise ValueError("invalid editorial output")
        with self.assertRaises(ValueError):
            run(self.config, discover=False, editor_factory=BrokenEditor, fetcher=fake_fetch)
        self.assertFalse(self.store.history())
        self.assertEqual(self.store.db.execute("SELECT status FROM runs").fetchone()[0], "failed")

    def test_editor_followup_enters_retry_queue(self):
        class FollowupEditor(FakeEditor):
            def edit(self, findings, history, feedback):
                return {"selected": [], "excluded": [{"id": f["id"], "disposition": "needs_evidence", "reason": "核心对照表尚未读取"} for f in findings]}
        outcome = run(self.config, discover=False, editor_factory=FollowupEditor, fetcher=fake_fetch)
        self.assertEqual(outcome["status"], "partial")
        current = self.store.get(self.identifier)
        self.assertEqual(current["status"], "needs_evidence")
        self.assertIsNone(current["reviewed_hash"])
        self.assertIn("核心对照表", Path(outcome["report"]).read_text())

    def test_budget_carry_over_visible(self):
        self.store.add({"url": "https://example.com/b", "title": "Other"})
        outcome = self.execute(limit=1)
        self.assertEqual(outcome["remaining"], 1)
        self.assertEqual(outcome["status"], "partial")

    def test_schedule_not_shell_and_retry_is_separate(self):
        units = schedule_files(self.config)
        self.assertEqual(len(units), 4)
        self.assertIn("Asia/Shanghai", units["agent-research-weekly.timer"])
        self.assertIn('"retry"', units["agent-research-weekly-retry.service"])
        self.assertNotIn("bash -c", units["agent-research-weekly.service"])

    def test_automatic_alternative_search_recovers_document(self):
        self.config["codex"]["search"] = True

        class RecoverEditor(FakeEditor):
            def alternatives(self, candidate, attempts):
                return {"urls": ["https://mirror.example/article"], "reason": "作者备份"}

        def transport(url, settings):
            if url.startswith("https://example.com/"):
                raise FetchError("timeout")
            return html_transport(url, settings)

        def fetcher(candidate, config, state):
            return fetch_evidence(candidate, config, state, transport)

        outcome = run(self.config, discover=False, editor_factory=RecoverEditor, fetcher=fetcher)
        self.assertEqual(outcome["selected"], 1)
        current = self.store.get(self.identifier)
        self.assertIn("https://mirror.example/article", json.loads(current["alternatives"]))
        evidence = json.loads((Path(self.temp.name)/"evidence"/current["evidence_hash"]/"evidence.json").read_text())
        self.assertEqual([a["status"] for a in evidence["attempts"]], ["failed", "failed", "ok"])

    def test_unknown_only_id_is_error(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.execute(only=["missing"])

    def test_new_candidate_precedes_old_refresh(self):
        candidates = [{"id": "old", "status": "selected", "fetched_at": "2020"}, {"id": "new", "status": "pending"}]
        self.assertEqual(select_queue(candidates, 1)[0]["id"], "new")

    def test_planning_pool_does_not_exclude_late_sources(self):
        candidates = [{"id": str(i), "url": f"https://early.example/{i}", "status": "pending"}
                      for i in range(150)]
        candidates.append({"id": "late", "url": "https://late.example/a", "status": "pending"})
        self.assertIn("late", [c["id"] for c in select_queue(candidates, 100)])


if __name__ == "__main__":
    unittest.main()
