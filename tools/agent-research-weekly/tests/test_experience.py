import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from research_weekly.codex import Codex
from research_weekly.core import Store, atomic_json, atomic_text, digest, load_config
from research_weekly.discovery import external_search
from research_weekly.pipeline import run
from research_weekly.publish import merge_issue, validate_bundle
from research_weekly.scope import eligible_date, is_experience
from test_weekly import FakeEditor, config_at, fake_fetch, result


class ExperienceEditor(FakeEditor):
    def review(self, candidate, evidence, feedback):
        answer = super().review(candidate, evidence, feedback)
        answer["published_at"] = candidate["published_at"]
        answer["findings"][0]["category"] = "practice"
        return answer


def experience_config(root):
    config = config_at(str(root))
    config["discovery"]["experience_topics"] = ["support agent actual task attempts feedback"]
    return config


class ExperienceDiscoveryTests(unittest.TestCase):
    def test_extra_query_preserves_current_queries_and_article_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            config = experience_config(temp)
            response = json.dumps({"content": [{"type": "text", "text": "URL: https://example.com/experience"}]})
            with patch("research_weekly.discovery.command", return_value=(response, "")) as command:
                records = external_search(config, "2026-09-11", "2026-09-17", 0, {}, Path(temp), time.monotonic() + 60)
            self.assertEqual([r["track"] for r in records], ["current", "current", "experience"])
            arguments = command.call_args.args[0]
            payload = json.loads(arguments[arguments.index("--args") + 1])
            self.assertIn("without a lower date bound", payload["objective"])
            self.assertIn("before 2026-09-17", payload["query"])
            self.assertNotIn("2026-09-11", payload["query"])
            self.assertEqual(json.loads((Path(temp) / "external-1-3.json").read_text())["track"], "experience")
            with patch("research_weekly.discovery.command", return_value=(response, "")):
                article = external_search(config, None, "2026-09-17", 0, {}, Path(temp), time.monotonic() + 60, historical=True)
            self.assertEqual(len(article), 2)

    def test_experience_query_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            response = json.dumps({"content": [{"type": "text", "text": "URL: https://example.com/a"}]})
            with patch("research_weekly.discovery.command", side_effect=[(response, ""), (response, ""), RuntimeError("offline")]):
                records = external_search(experience_config(temp), "2026-09-11", "2026-09-17", 0, {}, Path(temp), time.monotonic() + 60)
            self.assertEqual(records[-1]["status"], "failed")
            self.assertEqual(records[-1]["track"], "experience")
            self.assertIn("offline", records[-1]["error"])

    def test_prompt_and_persistent_origin_distinguish_history(self):
        with tempfile.TemporaryDirectory() as temp:
            editor = Codex(experience_config(temp), temp)
            response = {"items": [{"url": "https://example.com/past", "track": "experience"}],
                        "notes": [], "followups": [], "search_complete": True}
            with patch("research_weekly.codex.external_search", return_value=[]), patch.object(editor, "ask", return_value=response) as ask:
                editor.last_search = [{"action": {"queries": ["actual task retrospective"]}}]
                found = editor.discover("2026-09-11", "2026-09-17")
            self.assertIn("不限当周", ask.call_args.args[1])
            self.assertIn("没有真实经历", ask.call_args.args[1])
            self.assertIn("track", ask.call_args.args[2]["properties"]["items"]["items"]["required"])
            store = Store(temp)
            try:
                identifier = store.add(found["items"][0])
                self.assertTrue(is_experience(store.get(identifier)))
            finally:
                store.close()

    def test_disabled_track_cannot_request_old_material(self):
        with tempfile.TemporaryDirectory() as temp:
            editor = Codex(config_at(temp), temp)
            response = {"items": [{"url": "https://example.com/past", "track": "experience"}],
                        "notes": [], "followups": [], "search_complete": True}
            with patch("research_weekly.codex.external_search", return_value=[]), patch.object(editor, "ask", return_value=response):
                editor.last_search = [{"action": {"queries": ["actual search"]}}]
                found = editor.discover("2026-09-11", "2026-09-17")
            self.assertEqual(found["items"][0]["track"], "current")
            self.assertFalse(is_experience(found["items"][0]))

    def test_recovery_keeps_experience_track_without_inventing_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            editor = Codex(experience_config(temp), temp)
            external = [{"backend": "exa", "query": "task history", "track": "experience", "status": "ok",
                         "text": "URL: https://example.com/past"}]
            empty = {"items": [], "notes": [], "followups": [], "search_complete": False}
            recovered = {**empty, "items": [{"url": "https://example.com/past", "track": "experience"},
                                           {"url": "https://invented.example/past", "track": "experience"}]}
            with patch("research_weekly.codex.external_search", return_value=external), patch.object(editor, "ask", side_effect=[empty, recovered]):
                found = editor.discover("2026-09-11", "2026-09-17")
            self.assertEqual(len(found["items"]), 1)
            self.assertTrue(is_experience(found["items"][0]))
            self.assertFalse(found["search_complete"])


class ExperiencePipelineTests(unittest.TestCase):
    def test_history_survives_both_filters_but_old_release_and_future_do_not(self):
        with tempfile.TemporaryDirectory() as temp:
            config = experience_config(temp)
            config["sources"] = [{"name": "fixture", "kind": "rss"}]
            items = [
                {"url": "https://example.com/experience", "title": "Task retrospective", "published_at": "2025-06-23", "track": "experience"},
                {"url": "https://example.com/old-launch", "published_at": "2025-06-23"},
                {"url": "https://example.com/future", "published_at": "2026-09-18", "track": "experience"},
                {"url": "https://example.com/out-of-scope", "title": "Medical task", "published_at": "2025-06-23", "track": "experience"},
            ]
            outcome = run(config, editor_factory=ExperienceEditor, fetcher=fake_fetch, source_reader=lambda *_: items,
                          now=dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc), start="2026-09-11", end="2026-09-17")
            self.assertEqual(outcome["processed"], 1)
            self.assertEqual(outcome["selected"], 1)
            bundle = json.loads(Path(outcome["bundle"]).read_text())
            self.assertEqual(bundle["findings"][0]["track"], "experience")
            self.assertIn("## 历史经验", Path(outcome["report"]).read_text())
            reasons = {d["url"]: d["disposition"] for d in outcome["candidate_decisions"]}
            self.assertEqual(reasons["https://example.com/old-launch"], "outside_window")
            self.assertEqual(reasons["https://example.com/future"], "outside_window")
            self.assertEqual(reasons["https://example.com/out-of-scope"], "excluded_scope")

    def test_disabling_history_keeps_existing_queue_without_processing_old_items(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            try:
                identifier = store.add({"url": "https://example.com/past", "published_at": "2025-06-23", "origin": "experience-search"})
                outcome = run(config_at(temp), editor_factory=ExperienceEditor, fetcher=fake_fetch)
                self.assertEqual(outcome["processed"], 0)
                self.assertIsNotNone(store.get(identifier))
            finally:
                store.close()

    def test_old_candidate_metadata_cannot_become_verified_date(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add({"url": "https://example.com/past", "published_at": "2025-06-23", "origin": "experience-search"})
            store.close()

            class Undated(ExperienceEditor):
                def review(self, *args):
                    answer = super().review(*args)
                    answer["published_at"] = None
                    return answer

            outcome = run(experience_config(temp), editor_factory=Undated, fetcher=fake_fetch)
            self.assertIn("## 发布日期未核实", Path(outcome["report"]).read_text())
            self.assertNotIn("## 历史经验", Path(outcome["report"]).read_text())

    def test_non_practice_cannot_pass_experience_track(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add({"url": "https://example.com/past", "published_at": "2025-06-23", "origin": "experience-search"})
            store.close()
            outcome = run(experience_config(temp), editor_factory=FakeEditor, fetcher=fake_fetch)
            self.assertEqual(outcome["selected"], 0)
            self.assertIn("practice", outcome["pending"][0]["reason"])

    def test_changed_verified_date_does_not_leak_future_into_report(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add({"url": "https://example.com/past", "published_at": "2025-06-23", "origin": "experience-search"})
            store.close()

            class Future(ExperienceEditor):
                def review(self, *args):
                    answer = super().review(*args)
                    answer["published_at"] = "2026-09-23"
                    return answer

            outcome = run(experience_config(temp), editor_factory=Future, fetcher=fake_fetch,
                          now=dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc))
            self.assertEqual(outcome["selected"], 0)
            self.assertTrue(any(d["disposition"] == "verified_date_outside_scope" for d in outcome["candidate_decisions"]))

    def test_switching_candidate_track_invalidates_prior_review(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            try:
                identifier = store.add({"url": "https://example.com/past", "published_at": "2025-06-23"})
                config = experience_config(temp)
                run(config, discover=False, editor_factory=FakeEditor, fetcher=fake_fetch, record_reported=False)
                store.add({"url": "https://example.com/past", "origin": "experience-search"})
                outcome = run(config, discover=False, force=True, only=[identifier], editor_factory=ExperienceEditor,
                              fetcher=fake_fetch, record_reported=False)
                self.assertEqual(outcome["cached_reviews"], 0)
                self.assertEqual(outcome["selected"], 1)
                self.assertTrue(json.loads(store.get(identifier)["review"])["experience_candidate"])
            finally:
                store.close()


class ExperiencePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "evidence" / "example" / "source.html"
        text = "Published June 23, 2025. The root coordinates the review while workers read documents."
        atomic_text(self.raw, text)
        self.finding = {**result()["findings"][0], "category": "practice", "track": "experience", "audit_verified": True,
                        "id": "lesson", "url": "https://example.com/past", "source_title": "Task retrospective",
                        "verified_date": "2025-06-23", "date_evidence": [{"block_id": "p1-b1", "quote": "Published June 23, 2025."}],
                        "source": {"raw_path": str(self.raw), "sha256": digest(text), "blocks": [{"id": "p1-b1", "text": text}]}}
        self.bundle = {"version": 1, "run_id": "sample", "scope": "weekly", "start": "2026-09-11", "end": "2026-09-17",
                       "discovery_rounds": [{"status": "ok", "queries": [f"q{i}"]} for i in range(3)], "findings": [self.finding]}

    def test_explicit_old_experience_can_pass_without_relaxing_current_window(self):
        accepted, deferred = validate_bundle(self.bundle, self.root)
        self.assertEqual(accepted, [self.finding])
        self.assertFalse(deferred)
        self.finding["track"] = "current"
        self.assertFalse(validate_bundle(self.bundle, self.root)[0])

    def test_future_and_unverified_experiences_are_deferred(self):
        for date in (None, "2026-09-18"):
            with self.subTest(date=date):
                self.finding["verified_date"] = date
                self.assertFalse(validate_bundle(self.bundle, self.root)[0])
        self.finding["verified_date"] = "2025-06-23"
        self.finding["date_evidence"] = []
        self.assertFalse(validate_bundle(self.bundle, self.root)[0])

    def test_malformed_date_cannot_hide_trailing_public_text(self):
        self.finding["verified_date"] = "2025-06-23\n## injected heading"
        with self.assertRaises(ValueError):
            validate_bundle(self.bundle, self.root)

    def test_release_or_incomplete_lesson_cannot_use_history_exemption(self):
        original = copy.deepcopy(self.finding)
        for change in ({"category": "release"}, {"problem": ""}, {"evidence": []}, {"limitations": [""]}, {"audit_verified": False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.bundle["findings"] = [{**original, **change}]
                validate_bundle(self.bundle, self.root)

    def test_historical_label_and_manifest_preserve_date_and_deduplication(self):
        atomic_json(self.root / "posts.json", [])
        merged = merge_issue(self.root, self.bundle, [self.finding])
        body = (self.root / merged["source"]).read_text()
        self.assertIn("2025-06-23 · 历史经验，非本周新作", body)
        manifest = json.loads((self.root / merged["source_manifest"]).read_text())
        self.assertEqual(manifest["sources"][0]["selection_track"], "experience")
        self.assertTrue(manifest["sources"][0]["historical"])
        self.assertIsNone(merge_issue(self.root, self.bundle, [self.finding]))


class ExperienceContractTests(unittest.TestCase):
    def test_date_and_marker_boundaries(self):
        self.assertTrue(eligible_date(None, "2026-09-11", "2026-09-17"))
        self.assertTrue(eligible_date("2025-06-23", "2026-09-11", "2026-09-17", experience=True))
        self.assertFalse(eligible_date("2026-09-18", "2026-09-11", "2026-09-17", experience=True))
        self.assertFalse(is_experience({"track": "current", "origin": "experience-search"}))
        self.assertTrue(is_experience({"origins": '["experience-search"]'}))

    def test_invalid_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            config = experience_config(temp)
            for topics in ("all", [""], [1], ["topic"] * 13):
                config["discovery"]["experience_topics"] = topics
                path = Path(temp) / "config.json"
                atomic_json(path, config)
                with self.subTest(topics=topics), self.assertRaisesRegex(ValueError, "experience_topics"):
                    load_config(path)

    def test_edit_receives_conditions_and_actions_not_only_headlines(self):
        with tempfile.TemporaryDirectory() as temp:
            editor = Codex(experience_config(temp), temp)
            finding = {**result()["findings"][0], "id": "one", "candidate_id": "candidate"}
            with patch.object(editor, "ask", return_value={"selected": ["one"], "excluded": []}) as ask:
                editor.edit([finding], [], [])
            prompt = ask.call_args.args[1]
            for key in ("problem", "mechanism", "implication"):
                self.assertIn(json.dumps(key), prompt)
            self.assertIn("未报告改后效果", editor.rules)

    def test_review_rejects_release_mislabeled_as_experience_before_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            config = experience_config(temp)
            config["codex"]["repair_rounds"] = 0
            editor = Codex(config, temp)
            candidate = {"id": "one", "title": "Launch", "url": "https://example.com/a", "published_at": None,
                         "focus": "", "origin": "experience-search"}
            evidence = fake_fetch(candidate, config, temp)
            answer = result(next(b["id"] for b in evidence["blocks"] if "The root coordinates" in b["text"]))
            answer["findings"][0]["category"] = "release"
            with patch.object(editor, "ask", return_value=answer) as ask, self.assertRaisesRegex(ValueError, "practice"):
                editor.review(candidate, evidence, [])
            self.assertEqual(ask.call_count, 1)

    def test_practice_audit_checks_proposed_fixes_and_version_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            config = experience_config(temp)
            editor = Codex(config, temp)
            candidate = {"id": "one", "title": "Experience", "url": "https://example.com/a", "published_at": None,
                         "focus": "", "origin": "experience-search"}
            evidence = fake_fetch(candidate, config, temp)
            answer = result(next(b["id"] for b in evidence["blocks"] if "The root coordinates" in b["text"]))
            answer["findings"][0]["category"] = "practice"
            with patch.object(editor, "ask", side_effect=[answer, {"verified": True, "issues": []}]) as ask:
                reviewed = editor.review(candidate, evidence, [])
            self.assertTrue(reviewed["audit_verified"])
            audit_prompt = ask.call_args.args[1]
            self.assertIn("把建议修复写成已成功", audit_prompt)
            self.assertIn("历史模型/工具版本", audit_prompt)


if __name__ == "__main__":
    unittest.main()
