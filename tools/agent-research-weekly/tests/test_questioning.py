"""逐轮审稿使用明确标识的模型替身，不执行真实发布。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_weekly.cli import main, schedule_files
from research_weekly.core import atomic_json, atomic_text, digest, load_config
from research_weekly.publish import publish, validate_bundle
from research_weekly.questioning import ANSWER, PATCH_FIELDS, ReviewBlocked, Session, check_answer, content_hash, packet_for, prepare
from test_weekly import config_at, result


class ScriptedEditor:
    requests = []
    fail_at = None
    with_issue = False
    bad_quote = False
    facts_pass = True
    resolves = True
    blank_revision = False

    def __init__(self, config, directory, deadline):
        self.calls = 0

    def ask(self, name, prompt, schema):
        self.calls += 1
        type(self).requests.append((name, prompt))
        if name == self.fail_at:
            raise TimeoutError("模拟中断")
        data = json.loads(prompt.split("<UNTRUSTED_REVIEW_DATA>", 1)[1].split("</UNTRUSTED_REVIEW_DATA>", 1)[0])
        finding = data["packet"]["findings"][0]
        if name.endswith("-question"):
            return {"question": "这个任务在什么条件下可以采用已有的划分方式？", "finding_ids": data["focus_ids"] or [finding["id"]]}
        if name.endswith("-answer"):
            if "answers" in schema["properties"]:
                return {"answers": [{"finding_id": item["id"],
                                     "answer": "材料只说明任务划分方式，没有独立测量它对任务完成率的影响。",
                                     "evidence": [{"block_id": "p1-b1", "quote": "The root coordinates the review while workers read documents."}],
                                     "gaps": []} for item in data["packet"]["findings"]]}
            return {"answer": "材料只说明任务划分方式，没有独立测量它对任务完成率的影响。",
                    "evidence": [{"finding_id": finding["id"], "block_id": "p1-b1",
                                  "quote": "fabricated evidence does not exist" if self.bad_quote else
                                  "The root coordinates the review while workers read documents."}], "gaps": []}
        if name == "issues":
            return {"issues": [{"priority": "medium", "finding_ids": [finding["id"]], "question_ids": ["Q01"],
                                "problem": "正文需要说明任务划分只是一种已有做法而不是独立实验结论。"}] if self.with_issue else []}
        if name.endswith("-deepen"):
            return {"problem": "任务划分与质量收益需要区分。", "options": [
                {"action": "补清已有流程。", "tradeoff": "不能据此声称任务收益。"},
                {"action": "追加完整实验。", "tradeoff": "本轮没有新实验环境。"}],
                "recommendation": "先说明流程和证据边界。", "validation": ["核对原文。", "检查没有编造收益。"],
                "replacement": "主节点划分任务，工作节点读取材料；这并不证明端到端质量提高。", "next_step": "局部返修后复核。"}
        if name.startswith("revision-"):
            revision = {k: copy.deepcopy(finding[k]) for k in PATCH_FIELDS}
            revision["paragraphs"][1] = "主节点划分任务，工作节点读取材料；这并不证明端到端质量提高。"
            return {"patches": [] if self.blank_revision else [{"finding_id": finding["id"], **revision}]}
        if name.startswith("final-facts-"):
            return {"verified": self.facts_pass, "issues": [] if self.facts_pass else ["新增结论缺少原文支持。"]}
        if name.startswith("final-closure"):
            return {"verified": self.resolves, "issues": [] if self.resolves else ["解释问题尚未解决。"],
                    "resolutions": [{"issue_id": i["id"], "resolved": self.resolves, "reason": "已核对最终段落。"} for i in data["issues"]]}
        raise AssertionError(name)


class QuestioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config_at(str(self.root))
        self.model = type("FreshScriptedEditor", (ScriptedEditor,), {"requests": []})
        text = "Published September 15, 2026. The root coordinates the review while workers read documents."
        self.raw = self.root / "evidence" / "source.html"
        atomic_text(self.raw, text)
        self.finding = {**result()["findings"][0], "id": "finding-one", "candidate_id": "candidate-one",
                        "url": "https://example.com/research", "source_title": "A controlled task",
                        "verified_date": "2026-09-15", "audit_verified": True,
                        "date_evidence": [{"block_id": "p1-b1", "quote": "Published September 15, 2026."}],
                        "source": {"raw_path": str(self.raw), "sha256": digest(text),
                                   "blocks": [{"id": "p1-b1", "text": text}]}}
        self.bundle = {"version": 1, "run_id": "test", "scope": "weekly", "start": "2026-09-11", "end": "2026-09-17",
                       "discovery_rounds": [{"status": "ok", "queries": [f"q{i}"]} for i in range(3)], "findings": [self.finding]}
        self.path = self.root / "runs" / "test" / "bundle.json"
        atomic_json(self.path, self.bundle)

    def run_review(self):
        return prepare(self.config, self.path, editor_factory=self.model)

    def directory(self):
        return self.path.parent / "questioning" / content_hash(self.bundle)

    def test_ten_actual_question_answer_pairs_and_three_reports(self):
        before = self.path.read_bytes()
        reviewed = self.run_review()
        self.assertEqual(len(self.model.requests), 23)
        self.assertEqual([n for n, _ in self.model.requests[:4]], ["Q01-question", "Q01-answer", "Q02-question", "Q02-answer"])
        self.assertIn("材料只说明任务划分方式", self.model.requests[2][1])
        self.assertNotIn("当前层：", self.model.requests[1][1])
        self.assertIn("questioning_review", reviewed)
        self.assertEqual(self.path.read_bytes(), before)
        for name in ("qa-record.md", "deepening-book.md", "core-summary.md"):
            self.assertTrue((self.directory() / name).read_text().strip())
        self.assertEqual(validate_bundle(reviewed, self.root)[0][0]["id"], "finding-one")

    def test_completed_review_replays_without_model_calls(self):
        first = self.run_review()
        calls = len(self.model.requests)
        self.assertEqual(self.run_review(), first)
        self.assertEqual(len(self.model.requests), calls)

    def test_resume_reuses_question_after_answer_timeout(self):
        self.model.fail_at = "Q02-answer"
        with self.assertRaises(TimeoutError):
            self.run_review()
        self.assertFalse((self.directory() / "result.json").exists())
        self.model.fail_at = None
        self.model.requests.clear()
        self.run_review()
        self.assertEqual(self.model.requests[0][0], "Q02-answer")
        self.assertEqual(len(self.model.requests), 20)

    def test_changed_draft_creates_new_review(self):
        first = self.run_review()
        self.bundle["findings"][0]["paragraphs"][0] = "另一种任务划分需要重新检查。"
        atomic_json(self.path, self.bundle)
        second = self.run_review()
        self.assertNotEqual(first["questioning_review"], second["questioning_review"])
        self.assertEqual(len(self.model.requests), 46)

    def test_forged_certificate_does_not_skip_dialogue(self):
        self.bundle["questioning_review"] = {"input_sha256": "0" * 64, "protocol": "questioning-v1", "result_sha256": "fake"}
        atomic_json(self.path, self.bundle)
        with self.assertRaisesRegex(ValueError, "没有已完成"):
            self.run_review()

    def test_modified_certified_draft_is_rejected(self):
        reviewed = self.run_review()
        reviewed["findings"][0]["paragraphs"][0] = "审稿之后擅自改变了结论。"
        atomic_json(self.path, reviewed)
        with self.assertRaisesRegex(ValueError, "提交稿"):
            self.run_review()

    def test_modified_report_is_not_silently_regenerated(self):
        self.run_review()
        (self.directory() / "qa-record.md").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "报告"):
            self.run_review()

    def test_missing_step_fails_closed_after_sealing(self):
        self.run_review()
        (self.directory() / "steps" / "Q01-answer.json").unlink()
        with self.assertRaisesRegex(ValueError, "缺少步骤"):
            self.run_review()

    def test_changed_checkpoint_response_fails(self):
        self.run_review()
        path = self.directory() / "steps" / "Q01-answer.json"
        record = json.loads(path.read_text())
        record["response"]["answer"] = "这是一条被篡改的模型回答。"
        atomic_json(path, record)
        with self.assertRaisesRegex(ValueError, "断点"):
            self.run_review()

    def test_fabricated_quote_stops_before_next_question(self):
        self.model.bad_quote = True
        with self.assertRaisesRegex(ValueError, "逐字匹配"):
            self.run_review()
        self.assertEqual(len(self.model.requests), 2)

    def test_whitespace_question_or_answer_cannot_be_certified(self):
        base_model = self.model
        for invalid_role in ("question", "answer"):
            with self.subTest(role=invalid_role):
                class BlankResponse(base_model):
                    def ask(self, name, prompt, schema):
                        response = super().ask(name, prompt, schema)
                        if name.endswith("-" + invalid_role):
                            response[invalid_role] = " " * 10
                        return response

                self.model = BlankResponse
                with self.assertRaisesRegex(ValueError, "空白"):
                    self.run_review()
                self.assertFalse((self.directory() / "result.json").exists())

    def test_multiple_findings_require_individual_answers_and_evidence_or_gaps(self):
        self.bundle["findings"] = [{**copy.deepcopy(self.finding), "id": f"finding-{i}"} for i in range(24)]
        atomic_json(self.path, self.bundle)
        base_model = self.model

        class IncompleteAnswer(base_model):
            def ask(self, name, prompt, schema):
                response = super().ask(name, prompt, schema)
                if name == "Q01-answer":
                    response["answers"] = response["answers"][:2]
                return response

        self.model = IncompleteAnswer
        with self.assertRaisesRegex(ValueError, "未逐条覆盖"):
            self.run_review()
        self.assertFalse((self.directory() / "steps" / "Q01-answer.json").exists())

        class UnsupportedAnswer(base_model):
            def ask(self, name, prompt, schema):
                response = super().ask(name, prompt, schema)
                if name == "Q01-answer":
                    response["answers"][0].update(evidence=[], gaps=[])
                return response

        self.model = UnsupportedAnswer
        with self.assertRaisesRegex(ValueError, "原文证据或明确缺口"):
            self.run_review()

    def test_invalid_response_is_not_saved_as_completed_checkpoint(self):
        self.model.bad_quote = True
        with self.assertRaisesRegex(ValueError, "逐字匹配"):
            self.run_review()
        self.assertFalse((self.directory() / "steps" / "Q01-answer.json").exists())
        self.model.bad_quote = False
        self.model.requests.clear()
        self.run_review()
        self.assertEqual(self.model.requests[0][0], "Q01-answer")

    def test_full_issue_uses_scoped_evidence_and_covers_every_finding(self):
        findings = []
        for i in range(13):
            item = copy.deepcopy(self.finding)
            item["id"] = "finding-" + str(i)
            text = item["source"]["blocks"][0]["text"] + " Additional original context." * 500
            raw = self.root / "evidence" / f"source-{i}.html"
            atomic_text(raw, text)
            item["source"] = {"raw_path": str(raw), "sha256": digest(text), "blocks": [{"id": "p1-b1", "text": text}]}
            findings.append(item)
        self.bundle["findings"] = findings
        atomic_json(self.path, self.bundle)
        self.run_review()
        self.assertEqual(len(self.model.requests), 35)
        self.assertTrue(all(len(prompt) <= self.config["codex"]["max_context_chars"] for _, prompt in self.model.requests))
        covered = set()
        for name, prompt in self.model.requests:
            if name.endswith("-question"):
                data = json.loads(prompt.split("<UNTRUSTED_REVIEW_DATA>")[1].split("</UNTRUSTED_REVIEW_DATA>")[0])
                covered.update(data["focus_ids"])
                self.assertNotIn("source_blocks", data["packet"]["findings"][0])
        self.assertEqual(covered, {f["id"] for f in findings})

    def test_repair_is_followed_by_independent_facts_and_closure(self):
        self.model.with_issue = True
        reviewed = self.run_review()
        self.assertIn("并不证明", reviewed["findings"][0]["paragraphs"][1])
        self.assertEqual([n for n, _ in self.model.requests[-4:]], ["D1-deepen", "revision-0", "final-facts-0", "final-closure"])

    def test_24_long_findings_fit_scoped_questions_and_complete_closure(self):
        findings = []
        for index in range(24):
            finding = copy.deepcopy(self.finding)
            finding["id"] = f"finding-{index}"
            finding["paragraphs"] = ["这是保留完整正文而非截断后的段落。" * 80] * 3
            findings.append(finding)
        self.bundle["findings"] = findings
        atomic_json(self.path, self.bundle)
        self.run_review()
        self.assertTrue(all(len(prompt) <= self.config["codex"]["max_context_chars"] for _, prompt in self.model.requests))
        covered = set()
        batches = []
        for name, prompt in self.model.requests:
            data = json.loads(prompt.split("<UNTRUSTED_REVIEW_DATA>")[1].split("</UNTRUSTED_REVIEW_DATA>")[0])
            if name.endswith("-question"):
                covered.update(data["focus_ids"])
            if name.startswith("final-closure-batch-"):
                batches.extend(f["id"] for f in data["packet"]["findings"])
        self.assertEqual(covered, {f["id"] for f in findings})
        self.assertEqual(set(batches), covered)
        calls = len(self.model.requests)
        self.run_review()
        self.assertEqual(len(self.model.requests), calls)

    def test_wide_source_context_keeps_all_cited_blocks_within_budget(self):
        findings = []
        for index in range(24):
            finding = copy.deepcopy(self.finding)
            finding["id"] = f"finding-{index}"
            finding["source"]["blocks"] = [{"id": "p1-b1", "text": self.raw.read_text()}]
            finding["evidence"] = []
            for block in range(18):
                block_id = f"context-{block}"
                finding["source"]["blocks"].append({"id": block_id, "text": f"Original excerpt {block}. " + "x" * 1900})
                if block % 3 == 1:
                    finding["evidence"].append({"block_id": block_id, "quote": f"Original excerpt {block}."})
            findings.append(finding)
        self.bundle["findings"] = findings
        atomic_json(self.path, self.bundle)
        self.run_review()
        for name, prompt in self.model.requests:
            self.assertLessEqual(len(prompt), self.config["codex"]["max_context_chars"])
            if name.endswith("-answer"):
                data = json.loads(prompt.split("<UNTRUSTED_REVIEW_DATA>")[1].split("</UNTRUSTED_REVIEW_DATA>")[0])
                for finding in data["packet"]["findings"]:
                    shown = {b["id"] for b in finding["source_blocks"]}
                    self.assertTrue({r["block_id"] for r in finding["evidence"]}.issubset(shown))
                    self.assertIn("p1-b1", shown)

    def test_failed_scoped_closure_cannot_be_overruled_by_summary(self):
        self.bundle["findings"][0]["paragraphs"] = ["完整正文。" * 400] * 6
        self.bundle["findings"] = [{**copy.deepcopy(self.bundle["findings"][0]), "id": f"finding-{i}"}
                                   for i in range(24)]
        atomic_json(self.path, self.bundle)
        base_model = self.model

        class FailedBatch(base_model):
            def ask(self, name, prompt, schema):
                response = super().ask(name, prompt, schema)
                if name == "final-closure-batch-0":
                    response.update(verified=False, issues=["本条仍有解释问题。"])
                return response

        self.model = FailedBatch
        with self.assertRaisesRegex(ReviewBlocked, "未通过"):
            self.run_review()
        self.assertFalse((self.directory() / "result.json").exists())

    def test_maximum_length_dialogue_is_not_lost_before_issue_extraction(self):
        base_model = self.model

        class LongDialogue(base_model):
            def ask(self, name, prompt, schema):
                response = super().ask(name, prompt, schema)
                if name.endswith("-question"):
                    response["question"] = "问题" * 500
                elif name.endswith("-answer"):
                    response["answer"] = "回答" * 1300
                    response["gaps"] = [str(i) + "缺" * 599 for i in range(6)]
                    response["evidence"] = [response["evidence"][0]] * 8
                return response

        self.model = LongDialogue
        self.run_review()
        prompt = next(p for n, p in self.model.requests if n == "issues")
        data = json.loads(prompt.split("<UNTRUSTED_REVIEW_DATA>")[1].split("</UNTRUSTED_REVIEW_DATA>")[0])
        self.assertEqual(len(data["transcript"]), 10)
        for entry in data["transcript"]:
            self.assertEqual(len(entry["answer"]["answer"]), 2600)
            self.assertEqual(len(entry["answer"]["gaps"]), 6)

    def test_context_reduction_preserves_new_revision_references(self):
        self.config["codex"]["max_context_chars"] = 4000
        packet = packet_for(self.bundle)
        finding = packet["findings"][0]
        finding["source_blocks"] += [{"id": "new-reference", "text": "A newly cited original sentence."},
                                      {"id": "optional", "text": "Adjacent context. " * 600}]
        finding["evidence"] = [{"block_id": "new-reference", "quote": "A newly cited original sentence."}]
        session = Session(self.config, self.directory(), self.model)
        session.required_blocks = {finding["id"]: {"p1-b1"}}
        fitted = session.fit_data("final-facts-0", "事实复核者", "核对事实。", {"packet": packet})
        self.assertEqual({b["id"] for b in fitted["packet"]["findings"][0]["source_blocks"]},
                         {"p1-b1", "new-reference"})

    def test_answer_cannot_cite_original_context_omitted_from_actual_request(self):
        self.config["codex"]["max_context_chars"] = 4000
        packet = packet_for(self.bundle)
        packet["findings"][0]["source_blocks"].append({"id": "optional", "text": "Omitted original context. " * 400})
        base_model = self.model

        class CitesOmitted(base_model):
            def ask(self, name, prompt, schema):
                response = super().ask(name, prompt, schema)
                response["evidence"] = [{"finding_id": "finding-one", "block_id": "optional", "quote": "Omitted original context."}]
                return response

        session = Session(self.config, self.directory(), CitesOmitted)
        session.required_blocks = {"finding-one": {"p1-b1"}}
        with self.assertRaisesRegex(ValueError, "逐字匹配"):
            session.step("Q01-answer", "回答者", "请回答。", {"packet": packet}, ANSWER,
                         lambda result, sent: check_answer(result, sent["packet"]), with_data=True)
        self.assertFalse((self.directory() / "steps" / "Q01-answer.json").exists())

    def test_unresolved_issue_blocks_publication_artifact(self):
        self.model.with_issue = True
        self.model.resolves = False
        with self.assertRaisesRegex(ValueError, "未通过"):
            self.run_review()
        self.assertFalse((self.directory() / "result.json").exists())
        self.assertFalse((self.directory() / "reviewed-bundle.json").exists())

    def test_failed_fact_check_is_not_retried_until_lucky(self):
        self.model.facts_pass = False
        with self.assertRaisesRegex(ValueError, "未通过"):
            self.run_review()
        calls = len(self.model.requests)
        self.model.facts_pass = True
        with self.assertRaisesRegex(ValueError, "未通过"):
            self.run_review()
        self.assertEqual(len(self.model.requests), calls)

    def test_required_repair_cannot_be_empty(self):
        self.model.with_issue = True
        self.model.blank_revision = True
        with self.assertRaisesRegex(ValueError, "未提供返修"):
            self.run_review()

    def test_invalid_revised_metadata_is_not_cached_or_sealed(self):
        base_model = self.model

        class InvalidRevision(base_model):
            with_issue = True

            def ask(self, name, prompt, schema):
                response = super().ask(name, prompt, schema)
                if name == "revision-0":
                    response["patches"][0]["problem"] = ""
                return response

        self.model = InvalidRevision
        with self.assertRaisesRegex(ValueError, "problem 不能为空"):
            self.run_review()
        self.assertFalse((self.directory() / "steps" / "revision-0.json").exists())
        self.assertFalse((self.directory() / "result.json").exists())

    def test_deepening_can_recommend_deleting_unsupported_text(self):
        base_model = self.model

        class DeleteParagraph(base_model):
            with_issue = True

            def ask(self, name, prompt, schema):
                response = super().ask(name, prompt, schema)
                if name.endswith("-deepen"):
                    response["replacement"] = ""
                    response["next_step"] = "删除未获支持的原段，保留其他正文；删除后仍须独立复核。"
                return response

        self.model = DeleteParagraph
        self.run_review()
        self.assertIn("建议删除原段，不插入占位文字", (self.directory() / "deepening-book.md").read_text())

    def test_context_overflow_never_truncates_or_calls_model(self):
        self.config["codex"]["max_context_chars"] = 50
        with self.assertRaisesRegex(ValueError, "不静默截断"):
            self.run_review()
        self.assertFalse(self.model.requests)

    def test_config_cannot_disable_gate(self):
        config = copy.deepcopy(self.config)
        config["questioning_review"] = {"enabled": False}
        path = self.root / "config.json"
        atomic_json(path, config)
        with self.assertRaisesRegex(ValueError, "关闭追问"):
            load_config(path)

    def test_timer_timeout_accounts_for_review_budget(self):
        self.assertIn("TimeoutStartSec=13500", schedule_files(self.config)["agent-research-weekly.service"])

    def publication_config(self):
        self.config["publication"] = {"enabled": True, "repository": "SaltAdamW/blog", "branch": "main",
                                      "site_url": "https://saltadamw.github.io/blog/", "checkout": str(self.root / "blog")}

    def test_new_publish_cannot_create_worktree_before_review_passes(self):
        self.publication_config()

        def git(_root, *args):
            if args[0] == "status":
                return ""
            if args[0] == "remote":
                return "https://github.com/SaltAdamW/blog.git"
            if args[0] == "rev-parse":
                return "base"
            if args[0] == "fetch":
                return ""
            raise AssertionError("Should not act before review: " + repr(args))

        with patch("research_weekly.publish.git", side_effect=git), \
             patch("research_weekly.publish.prepare_questioning", side_effect=ValueError("追问未完成")) as review:
            with self.assertRaisesRegex(ValueError, "追问未完成"):
                publish(self.config, self.path)
        review.assert_called_once_with(self.config, self.path)

    def test_content_block_is_terminal_for_scheduler_but_allows_explicit_revision(self):
        self.publication_config()
        commands = ["", "https://github.com/SaltAdamW/blog.git", "base", "", "base"]
        with patch("research_weekly.publish.git", side_effect=commands), \
             patch("research_weekly.publish.prepare_questioning", side_effect=ReviewBlocked("缺少原文")):
            with self.assertRaises(ReviewBlocked):
                publish(self.config, self.path)
        ledger_path = self.root / "publications" / "test" / "status.json"
        self.assertEqual(json.loads(ledger_path.read_text())["status"], "needs_revision")
        self.bundle["findings"][0]["paragraphs"][0] = "这是一份有明确边界的修订草稿。"
        atomic_json(self.path, self.bundle)
        with patch("research_weekly.publish.git", side_effect=commands), \
             patch("research_weekly.publish.prepare_questioning", side_effect=ReviewBlocked("另一个待处理问题")) as review:
            with self.assertRaisesRegex(ReviewBlocked, "另一个"):
                publish(self.config, self.path)
        self.assertEqual(review.call_count, 1)
        self.assertEqual(len(json.loads(ledger_path.read_text())["previous_reviews"]), 1)

    def test_schedulers_do_not_replay_content_blocked_publication(self):
        atomic_json(self.root / "publications" / "test" / "status.json",
                    {"status": "needs_revision", "bundle": str(self.path)})
        with patch("research_weekly.cli.load_config", return_value=self.config), \
             patch("research_weekly.cli.publish") as publish_mock, \
             patch("research_weekly.cli.run", return_value={"bundle": "new-bundle"}) as run_mock, \
             patch("research_weekly.cli.print_json"):
            main(["deliver"])
            self.assertEqual(run_mock.call_count, 1)
            publish_mock.assert_called_once_with(self.config, "new-bundle")
            publish_mock.reset_mock()
            main(["retry"])
            publish_mock.assert_not_called()

    def test_verified_history_uses_frozen_revised_content(self):
        self.publication_config()
        self.model.with_issue = True
        reviewed = self.run_review()
        ledger = {"status": "verified", "bundle": str(self.path), "questioning_review": reviewed["questioning_review"],
                  "finding_ids": [self.finding["id"]]}
        atomic_json(self.root / "publications" / "test" / "status.json", ledger)
        with patch("research_weekly.publish.record_publication") as record:
            publish(self.config, self.path)
        self.assertEqual(record.call_args.args[1], reviewed)

    def test_old_unpushed_commit_cannot_use_recovery_to_bypass_review(self):
        self.publication_config()
        work = self.root / "worktree"
        work.mkdir()
        atomic_json(self.root / "publications" / "test" / "status.json",
                    {"status": "committed", "bundle": str(self.path), "base": "base", "commit": "new", "worktree": str(work)})
        with patch("research_weekly.publish.api", return_value={"sha": "base"}), \
             patch("research_weekly.publish.git", return_value="new") as git:
            with self.assertRaisesRegex(RuntimeError, "没有追问记录"):
                publish(self.config, self.path)
        self.assertFalse(any("push" in c.args for c in git.call_args_list))


if __name__ == "__main__":
    unittest.main()
