import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from research_weekly.cli import schedule_files
from research_weekly.codex import Codex
from research_weekly.core import Store, atomic_json, atomic_text, digest
from research_weekly.discovery import scan
from research_weekly.pipeline import run
from research_weekly.publish import document_key, merge_issue, online_check, publish, record_publication, safe_path, validate_bundle
from research_weekly.scope import check_prose, excluded, window
from test_weekly import FakeEditor, config_at, fake_fetch


class DiscoveryTests(unittest.TestCase):
    def test_local_report_does_not_suppress_later_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add({"url": "https://example.com/new", "title": "New"})
            store.close()
            config = config_at(temp)
            first = run(config, discover=False, editor_factory=FakeEditor, fetcher=fake_fetch)
            atomic_json(Path(temp) / "blog" / "posts.json", [])
            config["publication"] = {"enabled": True, "checkout": str(Path(temp) / "blog")}
            second = run(config, discover=False, record_reported=False, editor_factory=FakeEditor, fetcher=fake_fetch)
            self.assertEqual(first["selected"], 1)
            self.assertEqual(second["selected"], 1)

    def test_retry_review_does_not_mark_unpublished_findings_as_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add({"url": "https://example.com/new", "title": "New"})
            run(config_at(temp), discover=False, record_reported=False, editor_factory=FakeEditor, fetcher=fake_fetch)
            self.assertFalse(store.history())
            store.close()

    def test_three_rounds_expand_previous_results_and_keep_query_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = config_at(temp)
            observed = []

            class Editor:
                def discover(self, start, end, index, known):
                    observed.append(copy.deepcopy(known))
                    return {"search_complete": True, "queries": [f"query-{index}"], "notes": [],
                            "items": [{"title": f"item-{index}", "url": f"https://example.com/{index}"}]}

            detail = {"source_results": [], "source_errors": [], "discovered": 0}

            def ingest(items, record):
                record["collected"] += len(items)
                detail["discovered"] += len(items)

            scan(Editor(), config, "2026-09-11", "2026-09-17", root, [], ingest, detail)
            self.assertEqual(len(observed), 3)
            self.assertEqual(len(observed[2]["items"]), 2)
            self.assertEqual(observed[2]["rounds"][0]["queries"], ["query-0"])
            self.assertEqual(json.loads((root / "discovery-round-3.json").read_text())["queries"], ["query-2"])

    def test_unexecuted_followup_and_failure_notes_survive_rounds(self):
        with tempfile.TemporaryDirectory() as temp:
            observed = []
            task = {"query": "new-project independent experiment", "source_url": "https://example.com/launch",
                    "role": "independent", "reason": "缺少独立实测"}

            class Editor:
                def discover(self, start, end, index, context):
                    observed.append(copy.deepcopy(context))
                    if index == 1:
                        raise RuntimeError("temporary search failure")
                    return {"search_complete": True, "queries": ["open launch search"],
                            "items": [], "notes": ["尚未查询独立实现"], "followups": [task] if index == 0 else []}

            detail = {"source_results": [], "source_errors": [], "discovered": 0}
            scan(Editor(), config_at(temp), "2026-09-11", "2026-09-17", Path(temp), [], lambda *_: None, detail)
            self.assertEqual(observed[2]["followups"], [task])
            self.assertIn("temporary search failure", observed[2]["rounds"][1]["notes"])
            self.assertEqual(detail["discovery_followups"], [task])
            saved = json.loads((Path(temp) / "discovery.json").read_text())
            self.assertEqual(saved["source_errors"][-1]["source"], "discovery-followups")
            self.assertIn("discovery_stop", saved)

    def test_executed_followup_is_not_repeated_but_no_result_note_survives(self):
        with tempfile.TemporaryDirectory() as temp:
            observed = []
            task = {"query": "project independent trial", "source_url": "https://example.com/a",
                    "role": "independent", "reason": "待查实测"}

            class Editor:
                def discover(self, start, end, index, context):
                    observed.append(copy.deepcopy(context))
                    return {"search_complete": True, "queries": [task["query"] if index == 1 else "broad query"],
                            "items": [], "notes": ["查询过但未找到独立原文"] if index == 1 else [],
                            "followups": [task] if index == 0 else []}

            detail = {"source_results": [], "source_errors": [], "discovered": 0}
            scan(Editor(), config_at(temp), "2026-09-11", "2026-09-17", Path(temp), [], lambda *_: None, detail)
            self.assertFalse(observed[2]["followups"])
            self.assertEqual(observed[2]["rounds"][1]["notes"], ["查询过但未找到独立原文"])

    def test_no_early_stagnation_stop_with_unexecuted_followup(self):
        with tempfile.TemporaryDirectory() as temp:
            config = config_at(temp)
            config["discovery"]["max_rounds"] = 5

            class Editor:
                def discover(self, *args):
                    return {"search_complete": True, "queries": ["open search"], "items": [], "notes": [],
                            "followups": [{"query": "unqueried project implementation", "source_url": "https://example.com/a",
                                           "role": "implementation", "reason": "仍需追代码"}]}

            detail = {"source_results": [], "source_errors": [], "discovered": 0}
            scan(Editor(), config, "2026-09-11", "2026-09-17", Path(temp), [], lambda *_: None, detail)
            self.assertEqual(len(detail["discovery_rounds"]), 5)

    def test_external_search_is_fixed_command_and_keeps_raw_result(self):
        from research_weekly.discovery import external_search
        with tempfile.TemporaryDirectory() as temp:
            config = config_at(temp)
            task = {"query": 'project $(touch /tmp/never-execute)', "role": "implementation",
                    "source_url": "https://example.com/a", "reason": "追查代码"}
            response = json.dumps({"content": [{"type": "text", "text": "Title: Example\nURL: https://example.com/try"}]})
            with patch("research_weekly.discovery.command", return_value=(response, "")) as mocked:
                records = external_search(config, "2026-09-11", "2026-09-17", 1, {"followups": [task]},
                                          Path(temp), time.monotonic() + 60)
            self.assertEqual(len(records), 2)
            args = mocked.call_args_list[1].args[0]
            self.assertEqual(args[:3], ["mcporter", "call", "exa.web_search_exa"])
            self.assertEqual(json.loads(args[args.index("--args") + 1])["query"], task["query"])
            self.assertNotIn("shell", mocked.call_args.kwargs)
            self.assertEqual(records[1]["status"], "ok")
            self.assertTrue(list(Path(temp).glob("external-*.json")))

    def test_external_failure_not_silently_treated_as_no_results(self):
        from research_weekly.discovery import external_search
        with tempfile.TemporaryDirectory() as temp:
            with patch("research_weekly.discovery.command", side_effect=RuntimeError("backend unavailable")):
                records = external_search(config_at(temp), "2026-09-11", "2026-09-17", 0, {},
                                          Path(temp), time.monotonic() + 60)
            self.assertTrue(all(r["status"] == "failed" for r in records))
            self.assertIn("backend unavailable", records[0]["error"])

    def test_discovery_prompt_carries_gaps_and_external_leads(self):
        with tempfile.TemporaryDirectory() as temp:
            editor = Codex(config_at(temp), temp)
            context = {"items": [], "rounds": [{"queries": ["prior query"], "notes": ["missing trial"]}],
                       "followups": []}
            external = [{"backend": "exa", "query": "open query", "status": "ok", "text": "new external implementation"}]
            with patch("research_weekly.codex.external_search", return_value=external), \
                 patch.object(editor, "ask", return_value={"items": [{"url": "https://example.com/a"}], "notes": [], "followups": [], "search_complete": True}) as ask:
                editor.last_search = [{"action": {"queries": ["actual web query"]}}]
                result = editor.discover("2026-09-11", "2026-09-17", 1, context)
            prompt = ask.call_args.args[1]
            for value in ("prior query", "missing trial", "new external implementation"):
                self.assertIn(value, prompt)
            self.assertEqual(result["queries"], ["actual web query", "open query"])

    def test_empty_search_output_recovers_only_observed_urls_and_keeps_queries(self):
        with tempfile.TemporaryDirectory() as temp:
            editor = Codex(config_at(temp), temp)
            external = [{"backend": "exa", "query": "open query", "status": "ok",
                         "text": "Title: Trial\nURL: https://example.com/trial\nFirsthand experiment"}]
            replies = [{"items": [], "notes": ["budget reached"], "followups": [], "search_complete": False},
                       {"items": [{"url": "https://example.com/trial"}, {"url": "https://made-up.example/a"}],
                        "notes": [], "followups": [], "search_complete": True}]
            with patch("research_weekly.codex.external_search", return_value=external), \
                 patch.object(editor, "ask", side_effect=replies) as ask:
                editor.last_search = [{"action": {"queries": ["actual web query"]}}]
                result = editor.discover("2026-09-11", "2026-09-17")
            self.assertEqual([item["url"] for item in result["items"]], ["https://example.com/trial"])
            self.assertFalse(result["search_complete"])
            self.assertEqual(result["queries"], ["actual web query", "open query"])
            self.assertFalse(ask.call_args.kwargs.get("search", False))
            self.assertIn("4至8条", ask.call_args_list[0].args[1])

    def test_current_scan_candidates_reach_priority_pool_before_old_backlog(self):
        with tempfile.TemporaryDirectory() as temp:
            config = config_at(temp)
            config["codex"]["search"] = True
            store = Store(temp)
            for index in range(105):
                store.add({"url": f"https://old-{index}.example/item", "title": f"old-{index}"})
            store.close()
            pools = []

            class Editor(FakeEditor):
                def discover(self, *_):
                    return {"search_complete": True, "queries": ["actual search"], "notes": [],
                            "items": [{"url": "https://new.example/practice", "title": "fresh implementation"}]}

                def prioritize(self, candidates, maximum):
                    pools.append(candidates)
                    return super().prioritize(candidates, maximum)

            run(config, limit=1, editor_factory=Editor, fetcher=fake_fetch)
            self.assertEqual(pools[0][0]["url"], "https://new.example/practice")

    def test_failed_search_is_a_gap_not_a_success(self):
        with tempfile.TemporaryDirectory() as temp:
            class Editor:
                def discover(self, *_):
                    raise RuntimeError("network unavailable")
            detail = {"source_results": [], "source_errors": [], "discovered": 0}
            scan(Editor(), config_at(temp), "2026-09-11", "2026-09-17", Path(temp), [], lambda *_: None, detail)
            self.assertEqual(len(detail["source_errors"]), 3)
            self.assertTrue(all(r["status"] == "failed" for r in detail["discovery_rounds"]))

    def test_scope_excluded_before_reading_and_logged(self):
        with tempfile.TemporaryDirectory() as temp:
            config = config_at(temp)
            config["sources"] = [{"name": "test", "kind": "rss"}]
            outcome = run(config, editor_factory=FakeEditor, fetcher=fake_fetch,
                          source_reader=lambda *_: [{"url": "https://example.com/paper", "title": "Medical agent research"}])
            self.assertEqual(outcome["processed"], 0)
            self.assertEqual(outcome["candidate_decisions"][0]["disposition"], "excluded_scope")

    def test_user_requested_material_is_not_dropped_by_metadata_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            config = config_at(temp)
            store = Store(temp)
            store.add({"url": "https://first.example/a", "title": "first"})
            requested = store.add({"url": "https://other.example/jev", "title": "Jev", "origin": "user-request"})
            store.close()
            outcome = run(config, discover=False, limit=1, editor_factory=FakeEditor, fetcher=fake_fetch)
            bundle = json.loads(Path(outcome["bundle"]).read_text())
            self.assertEqual(bundle["findings"][0]["candidate_id"], requested)


class ScopeTests(unittest.TestCase):
    def test_arxiv_abstract_and_full_text_are_one_reported_document(self):
        keys = {document_key(url) for url in ("https://arxiv.org/abs/2609.12345", "https://arxiv.org/html/2609.12345v1", "https://arxiv.org/pdf/2609.12345v1.pdf")}
        self.assertEqual(keys, {"arxiv:2609.12345v1"})
        self.assertNotIn(document_key("https://arxiv.org/html/2609.12345v2"), keys)

    def test_default_window_is_seven_completed_days(self):
        now = dt.datetime(2026, 9, 21, 9)
        self.assertEqual(window(now), ("2026-09-14", "2026-09-20"))
        with self.assertRaises(ValueError):
            window(now, end="2026-09-21")

    def test_structured_decision_not_required_to_use_agent_keyword(self):
        self.assertFalse(excluded("Jev type-safe probabilistic decisions"))
        for title in ("临床模型", "genomics medical agents", "病理报告"):
            self.assertTrue(excluded(title))

    def test_public_text_rejects_embedded_commands_links_and_html(self):
        for value in ("<script>alert(1)</script>", "[x](javascript:alert(1))", "# 标题", "医疗应用"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_prose("标题", [value, "第二段。", "第三段。"])

    def test_timer_runs_delivery_not_legacy_generator(self):
        units = schedule_files(config_at("/tmp/test-only"))
        self.assertIn('"deliver"', units["agent-research-weekly.service"])
        self.assertNotIn(".codex/tmp", units["agent-research-weekly.service"])
        self.assertIn("Mon *-*-* 09:00:00 Asia/Shanghai", units["agent-research-weekly.timer"])


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "evidence" / "hash" / "source.html"
        text = "Published September 15, 2026. Type-safe choices are not guaranteed to be correct decisions."
        atomic_text(self.raw, text)
        self.finding = {
            "id": "finding-jev", "title": "Jev：从生成文本到选择动作", "url": "https://typesafe.ai/blog/jev",
            "source_title": "Introducing Jev", "verified_date": "2026-09-15", "audit_verified": True,
            "paragraphs": ["工作流需要在有限选项里做判断。", "模型返回结构化选项和概率。", "类型正确不保证判断正确。"],
            "date_evidence": [{"block_id": "p1", "quote": "Published September 15, 2026."}],
            "evidence": [{"block_id": "p1", "quote": "Type-safe choices are not guaranteed"}],
            "source": {"raw_path": str(self.raw), "sha256": digest(text), "blocks": [{"id": "p1", "text": text}]}
        }
        self.bundle = {"version": 1, "run_id": "test", "scope": "weekly", "start": "2026-09-11", "end": "2026-09-17",
                       "discovery_rounds": [{"status": "ok", "queries": [f"q{i}"]} for i in range(3)], "findings": [self.finding]}

    def test_delivery_history_is_recorded_only_after_online_verification(self):
        self.finding.update(candidate_id="candidate", conclusion="结论")
        config = config_at(str(self.root))
        for status in ("preparing", "committed", "pushed"):
            record_publication(config, self.bundle, {"status": status, "finding_ids": [self.finding["id"]]})
        store = Store(self.root)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM reported").fetchone()[0], 0)
        store.close()
        ledger = {"status": "verified", "finding_ids": [self.finding["id"]]}
        record_publication(config, self.bundle, ledger)
        record_publication(config, self.bundle, ledger)
        store = Store(self.root)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM reported").fetchone()[0], 1)
        store.close()

    def test_missing_date_does_not_publish_as_this_week(self):
        self.finding["verified_date"] = None
        accepted, deferred = validate_bundle(self.bundle, self.root)
        self.assertFalse(accepted)
        self.assertEqual(len(deferred), 1)

    def test_insufficient_search_and_missing_audit_fail_closed(self):
        self.bundle["discovery_rounds"] = []
        with self.assertRaisesRegex(ValueError, "三轮"):
            validate_bundle(self.bundle, self.root)
        self.bundle["scope"] = "targeted"
        with self.assertRaisesRegex(ValueError, "限定"):
            validate_bundle(self.bundle, self.root)
        self.finding["audit_verified"] = False
        with self.assertRaisesRegex(ValueError, "复核"):
            validate_bundle(self.bundle, self.root, allow_supplement=True)

    def test_snapshot_tampering_fails_closed(self):
        self.raw.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "哈希"):
            validate_bundle(self.bundle, self.root)

    def test_citation_fabrication_fails_closed(self):
        self.finding["evidence"][0]["quote"] = "does not exist"
        with self.assertRaisesRegex(ValueError, "引用"):
            validate_bundle(self.bundle, self.root)

    def test_existing_report_is_appended_not_rewritten_and_retries_deduplicate(self):
        title = "Research 周报：2026 年 9 月 11 日至 9 月 17 日"
        original = f"# {title}\n\n原始导语。\n\n## 原有内容\n\n原有段落逐字保留。\n"
        atomic_text(self.root / "issue.md", original)
        atomic_json(self.root / "issue.sources.json", {"window": {"start": "2026-09-11", "end": "2026-09-17"}, "sources": []})
        atomic_json(self.root / "posts.json", [{"slug": "weekly-agent-research-2026-09-17", "title": title,
                     "source": "issue.md", "source_manifest": "issue.sources.json", "description": "原摘要。", "deck": "原导语。"}])
        merged = merge_issue(self.root, self.bundle, [self.finding])
        self.assertTrue((self.root / "issue.md").read_text().startswith(original))
        self.assertEqual(merged["added"], 1)
        after = (self.root / "issue.md").read_text()
        self.assertIsNone(merge_issue(self.root, self.bundle, [self.finding]))
        self.assertEqual((self.root / "issue.md").read_text(), after)
        manifest = json.loads((self.root / "issue.sources.json").read_text())
        self.assertNotIn("raw_path", json.dumps(manifest))

    def test_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            safe_path(self.root, "../outside.md")

    def test_targeted_supplement_cannot_create_a_new_weekly_issue(self):
        self.bundle["scope"] = "targeted"
        atomic_json(self.root / "posts.json", [])
        with self.assertRaisesRegex(ValueError, "只能追加"):
            merge_issue(self.root, self.bundle, [self.finding])

    def publication_config(self):
        bundle = self.root / "runs" / "test" / "bundle.json"
        atomic_json(bundle, self.bundle)
        config = config_at(str(self.root))
        config["publication"] = {"enabled": True, "repository": "SaltAdamW/blog", "branch": "main",
                                  "site_url": "https://saltadamw.github.io/blog/", "checkout": str(self.root / "blog")}
        return config, bundle

    def test_dirty_blog_stops_before_worktree_or_push(self):
        config, bundle = self.publication_config()
        with patch("research_weekly.publish.git", return_value=" M user-draft.md") as mocked:
            with self.assertRaisesRegex(RuntimeError, "未提交改动"):
                publish(config, bundle)
        self.assertEqual(mocked.call_count, 1)

    def test_wrong_remote_target_is_rejected(self):
        config, bundle = self.publication_config()
        config["publication"]["repository"] = "someone/else"
        with self.assertRaisesRegex(ValueError, "目标"):
            publish(config, bundle)

    def test_pushed_commit_resumes_readback_without_rebuilding(self):
        config, bundle = self.publication_config()
        ledger_path = self.root / "publications" / "test" / "status.json"
        atomic_json(ledger_path, {"status": "pushed", "bundle": str(bundle), "base": "base", "commit": "new",
                                "worktree": str(self.root / "absent"), "files": {"index.html": "hash"}, "error": "prior timeout"})
        with patch("research_weekly.publish.api", return_value={"sha": "new"}), \
             patch("research_weekly.publish.online_check", return_value={"pages_run": 123, "verified_files": ["index.html"]}) as online, \
             patch("research_weekly.publish.sync_drafts", return_value=[]), \
             patch("research_weekly.publish.git", return_value="new") as commands:
            outcome = publish(config, bundle)
        self.assertEqual(outcome["status"], "verified")
        self.assertNotIn("error", outcome)
        self.assertEqual(online.call_count, 1)
        self.assertFalse(any("push" in call.args for call in commands.call_args_list))

    def test_ci_success_is_not_enough_when_online_content_differs(self):
        settings = {"repository": "test", "branch": "main", "site_url": "https://example.com/", "deploy_timeout_seconds": 1}
        run = {"name": "pages build and deployment", "path": "dynamic/pages/pages-build-deployment", "head_sha": "abc", "status": "completed", "conclusion": "success", "id": 1, "html_url": "https://example.com/run"}

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self):
                return b"old content"

        with patch("research_weekly.publish.api", side_effect=[{"workflow_runs": [run]}, {"sha": "abc"}]), \
             patch("research_weekly.publish.urlopen", return_value=Response()), patch("research_weekly.publish.time.sleep"):
            with self.assertRaisesRegex(ValueError, "不匹配"):
                online_check(settings, "abc", {"index.html": digest("new content")})


if __name__ == "__main__":
    unittest.main()
