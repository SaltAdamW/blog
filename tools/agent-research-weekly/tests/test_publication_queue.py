import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_weekly.cli import main
from research_weekly.core import Store, atomic_json, atomic_text, digest, run_lock
from research_weekly.publish import publish, record_publication
from research_weekly.questioning import ReviewBlocked, prepare
from test_questioning import ScriptedEditor
from test_weekly import config_at, result


class PublicationQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = config_at(temporary.name)
        self.config["publication"] = {
            "enabled": True, "repository": "SaltAdamW/blog", "branch": "main",
            "site_url": "https://saltadamw.github.io/blog/", "checkout": str(self.root / "blog"),
        }
        text = "Published September 15, 2026. The root coordinates the review while workers read documents."
        raw = self.root / "evidence" / "source.html"
        atomic_text(raw, text)
        finding = {
            **result()["findings"][0], "id": "finding-one", "candidate_id": "candidate-one",
            "url": "https://example.com/research", "source_title": "Controlled task",
            "verified_date": "2026-09-15", "audit_verified": True,
            "date_evidence": [{"block_id": "p1-b1", "quote": "Published September 15, 2026."}],
            "source": {"raw_path": str(raw), "sha256": digest(text), "blocks": [{"id": "p1-b1", "text": text}]},
        }
        self.bundle = {
            "version": 1, "run_id": "test", "scope": "weekly", "start": "2026-09-11", "end": "2026-09-17",
            "discovery_rounds": [{"status": "ok", "queries": [f"q{i}"]} for i in range(3)], "findings": [finding],
        }
        self.path = self.root / "runs" / "test" / "bundle.json"
        self.ledger_path = self.root / "publications" / "test" / "status.json"
        atomic_json(self.path, self.bundle)
        for name in ("git", "api", "command", "prepare_questioning", "online_check", "sync_drafts"):
            guard = patch("research_weekly.publish." + name, side_effect=AssertionError("unexpected " + name))
            guard.start()
            self.addCleanup(guard.stop)

    def contend(self, **kwargs):
        with self.assertRaises(RuntimeError) as error:
            publish(self.config, self.path, **kwargs)
        self.assertNotIsInstance(error.exception, ReviewBlocked)

    def test_lock_contention_keeps_validated_publication_pending(self):
        with run_lock(self.root / "publish-lock"):
            self.contend(allow_supplement=True)
        ledger = json.loads(self.ledger_path.read_text())
        self.assertEqual(ledger["status"], "pending")
        self.assertEqual(ledger["bundle"], str(self.path))
        self.assertEqual(ledger["bundle_sha256"], digest(self.path.read_bytes()))
        self.assertTrue(ledger["allow_supplement"])
        self.assertEqual(ledger["deferred"], [])
        self.assertNotIn("questioning_review", ledger)
        self.assertNotIn("error", ledger)

    def test_deliver_lock_contention_is_discovered_by_retry(self):
        with patch("research_weekly.cli.load_config", return_value=self.config), \
             patch("research_weekly.cli.run", return_value={"bundle": str(self.path)}) as run, \
             patch("research_weekly.cli.print_json"):
            with run_lock(self.root / "publish-lock"), self.assertRaises(SystemExit) as error:
                main(["deliver"])
            self.assertEqual(error.exception.code, 1)
            run.assert_called_once()
            with patch("research_weekly.cli.publish", return_value={"status": "pending"}) as retry:
                main(["retry"])
            retry.assert_called_once_with(self.config, str(self.path), allow_supplement=False)

    def test_invalid_bundle_does_not_enqueue_or_acquire_lock(self):
        for change in ({"audit_verified": False}, {"paragraphs": ["incomplete"]}):
            with self.subTest(change=change):
                bundle = copy.deepcopy(self.bundle)
                bundle["findings"][0].update(change)
                atomic_json(self.path, bundle)
                with patch("research_weekly.publish.run_lock") as lock, self.assertRaises(ValueError):
                    publish(self.config, self.path)
                lock.assert_not_called()
                self.assertFalse(self.ledger_path.exists())

    def test_existing_ledger_is_never_overwritten_before_lock(self):
        for status in ("pending", "preparing", "committed", "pushed", "verified", "needs_revision", "verified_pending_history"):
            with self.subTest(status=status):
                atomic_json(self.ledger_path, {
                    "status": status, "bundle": str(self.path), "bundle_sha256": "previous",
                    "allow_supplement": False, "previous_reviews": [{"error": "preserve me"}],
                })
                before = self.ledger_path.read_bytes()
                with run_lock(self.root / "publish-lock"):
                    self.contend(allow_supplement=True)
                self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_invalid_bundle_preserves_existing_ledger(self):
        atomic_json(self.ledger_path, {"status": "needs_revision", "error": "preserve me"})
        before = self.ledger_path.read_bytes()
        self.bundle["findings"][0]["audit_verified"] = False
        atomic_json(self.path, self.bundle)
        with self.assertRaises(ValueError):
            publish(self.config, self.path)
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_content_block_after_lock_contention_is_not_a_lock_failure(self):
        with run_lock(self.root / "publish-lock"):
            self.contend()
        self.assertEqual(json.loads(self.ledger_path.read_text())["status"], "pending")
        commands = ["", "https://github.com/SaltAdamW/blog.git", "base", "", "base"]
        with patch("research_weekly.publish.git", side_effect=commands), \
             patch("research_weekly.publish.prepare_questioning", side_effect=ReviewBlocked("content rejected")):
            with self.assertRaises(ReviewBlocked):
                publish(self.config, self.path)
        ledger = json.loads(self.ledger_path.read_text())
        self.assertEqual(ledger["status"], "needs_revision")
        self.assertEqual(ledger["error"], "content rejected")
        with patch("research_weekly.cli.load_config", return_value=self.config), \
             patch("research_weekly.cli.publish") as retry, patch("research_weekly.cli.print_json"):
            main(["retry"])
        retry.assert_not_called()

    def test_concurrent_enqueues_publish_one_complete_ledger(self):
        real_link = os.link
        barrier = threading.Barrier(2)
        winners = []

        def link(source, destination):
            intent = json.loads(Path(source).read_text())
            self.assertEqual(intent["status"], "pending")
            barrier.wait(timeout=5)
            real_link(source, destination)
            winners.append(intent)

        def contender(allow_supplement):
            try:
                publish(self.config, self.path, allow_supplement=allow_supplement)
            except RuntimeError as error:
                return error
            self.fail("publication unexpectedly acquired the lock")

        with run_lock(self.root / "publish-lock"), patch("research_weekly.publish.os.link", side_effect=link):
            with ThreadPoolExecutor(max_workers=2) as pool:
                failures = list(pool.map(contender, (False, True)))
        self.assertEqual(len(winners), 1)
        self.assertEqual(json.loads(self.ledger_path.read_text()), winners[0])
        self.assertTrue(all(not isinstance(error, ReviewBlocked) for error in failures))
        self.assertEqual(list(self.ledger_path.parent.iterdir()), [self.ledger_path])

    def test_ledger_created_during_enqueue_is_not_overwritten(self):
        real_link = os.link
        existing = {"status": "committed", "commit": "preserve-commit", "files": {"index.html": "hash"}}

        def link(source, destination):
            atomic_json(destination, existing)
            real_link(source, destination)

        with run_lock(self.root / "publish-lock"), patch("research_weekly.publish.os.link", side_effect=link):
            self.contend()
        self.assertEqual(json.loads(self.ledger_path.read_text()), existing)
        self.assertEqual(list(self.ledger_path.parent.iterdir()), [self.ledger_path])

    def test_enqueue_io_failure_does_not_leave_a_partial_ledger(self):
        with patch("research_weekly.publish.os.link", side_effect=OSError("disk failure")), \
             patch("research_weekly.publish.run_lock") as lock, self.assertRaises(OSError):
            publish(self.config, self.path)
        lock.assert_not_called()
        self.assertFalse(self.ledger_path.exists())
        self.assertEqual(list(self.ledger_path.parent.iterdir()), [])

    def seed_ledger(self, status, **fields):
        ledger = {"status": status, "bundle": str(self.path), "bundle_sha256": digest(self.path.read_bytes()),
                  "allow_supplement": False, "deferred": [], **fields}
        atomic_json(self.ledger_path, ledger)
        return ledger

    def revise(self):
        self.bundle["findings"][0]["paragraphs"][0] = "The task definition was revised for another review."
        atomic_json(self.path, self.bundle)

    def preflight(self, root, *args):
        if args[0] in ("status", "fetch"):
            return ""
        if args[0] == "remote":
            return "https://github.com/SaltAdamW/blog.git"
        if args[0] == "rev-parse":
            return "base"
        self.fail("unexpected git operation: " + repr(args))

    def reviewed_bundle(self):
        class Editor(ScriptedEditor):
            requests = []
            with_issue = True

            def ask(self, name, prompt, schema):
                answer = super().ask(name, prompt, schema)
                if name.startswith("revision-"):
                    answer["patches"][0]["conclusion"] = "Reviewed conclusion with explicit limits."
                return answer

        return prepare(self.config, self.path, editor_factory=Editor)

    def test_changed_pending_or_blocked_input_is_persisted_before_model_timeout(self):
        for status in ("pending", "needs_revision"):
            with self.subTest(status=status):
                atomic_json(self.path, self.bundle)
                previous = self.seed_ledger(status, error="previous failure")
                self.bundle["findings"][0]["title"] += " revised"
                atomic_json(self.path, self.bundle)

                def timeout(*args):
                    ledger = json.loads(self.ledger_path.read_text())
                    self.assertEqual(ledger["status"], "pending")
                    self.assertEqual(ledger["bundle_sha256"], digest(self.path.read_bytes()))
                    self.assertEqual(ledger["previous_reviews"][-1]["bundle_sha256"], previous["bundle_sha256"])
                    self.assertEqual(ledger["previous_reviews"][-1]["error"], "previous failure")
                    raise TimeoutError("model timeout")

                with patch("research_weekly.publish.git", side_effect=self.preflight), \
                     patch("research_weekly.publish.prepare_questioning", side_effect=timeout) as review:
                    with self.assertRaises(TimeoutError):
                        publish(self.config, self.path)
                    with patch("research_weekly.cli.load_config", return_value=self.config), \
                         patch("research_weekly.cli.print_json"), self.assertRaises(SystemExit):
                        main(["retry"])
                self.assertEqual(review.call_count, 2)

    def test_revision_preserves_old_worktree_attempt_and_review_artifacts(self):
        reviewed = self.reviewed_bundle()
        artifacts = {p: p.read_bytes() for p in (self.path.parent / "questioning").rglob("*") if p.is_file()}
        work = self.root / "old-worktree"
        atomic_text(work / "draft.md", "uncommitted previous draft")
        self.seed_ledger("preparing", base="base", worktree=str(work), attempt=3,
                         questioning_review=reviewed["questioning_review"])
        self.revise()
        checked = []

        def git(root, *args):
            if Path(root) == work:
                self.assertEqual(args, ("rev-parse", "HEAD"))
                checked.append(True)
                return "base"
            return self.preflight(root, *args)

        def timeout(*args):
            self.assertTrue(checked)
            ledger = json.loads(self.ledger_path.read_text())
            self.assertEqual(ledger["worktree"], str(work))
            self.assertEqual(ledger["attempt"], 3)
            self.assertNotIn("questioning_review", ledger)
            self.assertEqual(ledger["previous_reviews"][-1]["questioning_review"], reviewed["questioning_review"])
            raise TimeoutError("model timeout")

        with patch("research_weekly.publish.git", side_effect=git), \
             patch("research_weekly.publish.prepare_questioning", side_effect=timeout), self.assertRaises(TimeoutError):
            publish(self.config, self.path)
        self.assertEqual((work / "draft.md").read_text(), "uncommitted previous draft")
        self.assertTrue(all(path.read_bytes() == data for path, data in artifacts.items()))

    def test_revision_rejects_unregistered_commit_before_changing_ledger(self):
        work = self.root / "old-worktree"
        work.mkdir()
        self.seed_ledger("preparing", base="base", worktree=str(work), attempt=2)
        before = self.ledger_path.read_bytes()
        self.revise()
        with patch("research_weekly.publish.git", return_value="unregistered-commit") as git, \
             self.assertRaises(RuntimeError):
            publish(self.config, self.path)
        git.assert_called_once_with(work, "rev-parse", "HEAD")
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_revision_cannot_rewrite_registered_commit(self):
        self.seed_ledger("needs_revision", base="base", commit="frozen-commit")
        before = self.ledger_path.read_bytes()
        self.revise()
        with self.assertRaises(ValueError):
            publish(self.config, self.path)
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_legacy_unpushed_commit_is_isolated_and_next_delivery_skips_it(self):
        previous = self.seed_ledger("committed", base="base", commit="old-commit", worktree=str(self.root / "absent"))
        with patch("research_weekly.publish.api", return_value={"sha": "base"}), self.assertRaises(RuntimeError):
            publish(self.config, self.path)
        ledger = json.loads(self.ledger_path.read_text())
        self.assertEqual(ledger["status"], "needs_revision")
        for key in ("commit", "base", "bundle_sha256", "worktree"):
            self.assertEqual(ledger[key], previous[key])
        self.assertTrue(ledger["error"])
        with patch("research_weekly.cli.load_config", return_value=self.config), \
             patch("research_weekly.cli.run", return_value={"bundle": "new-bundle"}), \
             patch("research_weekly.cli.publish") as retry, patch("research_weekly.cli.print_json"):
            main(["deliver"])
        retry.assert_called_once_with(self.config, "new-bundle")

    def test_certified_input_resumes_committed_pushed_and_history_states(self):
        reviewed = self.reviewed_bundle()
        atomic_json(self.path, reviewed)
        for status in ("committed", "pushed", "verified_pending_history", "verified"):
            with self.subTest(status=status):
                self.seed_ledger(status, base="base", commit="reviewed-commit", worktree=str(self.root / "absent"),
                                 files={"index.html": "hash"}, finding_ids=["finding-one"],
                                 questioning_review=reviewed["questioning_review"])
                with patch("research_weekly.publish.api", return_value={"sha": "reviewed-commit"}) as api, \
                     patch("research_weekly.publish.git", return_value="reviewed-commit") as git, \
                     patch("research_weekly.publish.online_check", return_value={"pages_run": 123}) as online, \
                     patch("research_weekly.publish.sync_drafts", return_value=[]):
                    outcome = publish(self.config, self.path)
                self.assertEqual(outcome["status"], "verified")
                self.assertFalse(any("push" in call.args for call in git.call_args_list))
                if status in ("verified_pending_history", "verified"):
                    api.assert_not_called()
                    online.assert_not_called()
                if status == "verified":
                    git.assert_not_called()
        store = Store(self.root)
        try:
            self.assertEqual(store.history()[0]["conclusion"], reviewed["findings"][0]["conclusion"])
        finally:
            store.close()

    def test_certified_input_must_exactly_match_frozen_output(self):
        reviewed = self.reviewed_bundle()
        for altered in (copy.deepcopy(reviewed), copy.deepcopy(self.bundle)):
            altered["questioning_review"] = reviewed["questioning_review"]
            altered["findings"][0]["conclusion"] = "Different submitted conclusion."
            atomic_json(self.path, altered)
            self.seed_ledger("verified_pending_history", questioning_review=reviewed["questioning_review"],
                             finding_ids=["finding-one"])
            with patch("research_weekly.publish.record_publication") as record, self.assertRaises(ValueError):
                publish(self.config, self.path)
            record.assert_not_called()

    def test_history_failure_is_retried_without_model_push_or_online_check(self):
        reviewed = self.reviewed_bundle()
        work = self.root / "publication-worktree"
        work.mkdir()
        root = Path(self.config["publication"]["checkout"])
        heads = {root: "base", work: "reviewed-commit"}
        self.seed_ledger("pushed", base="base", commit="reviewed-commit", worktree=str(work),
                         files={"index.html": "hash"}, finding_ids=["finding-one"],
                         questioning_review=reviewed["questioning_review"])

        def unavailable(config, bundle, ledger):
            self.assertEqual(ledger["status"], "verified_pending_history")
            self.assertEqual(json.loads(self.ledger_path.read_text())["status"], "verified_pending_history")
            raise sqlite3.OperationalError("database is locked")

        with patch("research_weekly.publish.api", return_value={"sha": "reviewed-commit"}), \
             patch("research_weekly.publish.online_check", return_value={"pages_run": 123}), \
             patch("research_weekly.publish.record_publication", side_effect=unavailable), self.assertRaises(sqlite3.OperationalError):
            publish(self.config, self.path)
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "verified_pending_history")
        self.assertEqual(heads[root], "base")

        def git(directory, *args):
            directory = Path(directory)
            if args[0] == "rev-parse":
                return "reviewed-commit" if args[1] == "origin/main" else heads[directory]
            if args[0] in ("status", "fetch"):
                return ""
            if args[0] == "remote":
                return "https://github.com/SaltAdamW/blog.git"
            if args[0] == "merge":
                self.assertEqual(args, ("merge", "--ff-only", "reviewed-commit"))
                heads[directory] = args[-1]
                return ""
            if args[:2] == ("worktree", "remove"):
                self.assertEqual(args[2], str(work))
                work.rmdir()
                return ""
            self.fail("unexpected recovery operation: " + repr(args))

        with patch("research_weekly.publish.git", side_effect=git) as commands, \
             patch("research_weekly.publish.sync_drafts", return_value=[]) as drafts:
            with patch("research_weekly.cli.load_config", return_value=self.config), patch("research_weekly.cli.print_json"):
                main(["retry"])
            self.assertEqual(heads[root], "reviewed-commit")
            self.assertFalse(work.exists())
            drafts.assert_called_once()
            self.assertFalse(any("push" in call.args for call in commands.call_args_list))
            next_path = self.root / "runs" / "next" / "bundle.json"
            atomic_json(next_path, {**self.bundle, "run_id": "next"})
            with patch("research_weekly.publish.prepare_questioning", side_effect=TimeoutError("next review reached")) as review:
                with self.assertRaises(TimeoutError):
                    publish(self.config, next_path)
            review.assert_called_once_with(self.config, next_path)
        completed = json.loads(self.ledger_path.read_text())
        self.assertEqual(completed["status"], "verified")
        self.assertEqual(completed["verified_at"], saved["verified_at"])
        store = Store(self.root)
        try:
            self.assertEqual(store.history()[0]["conclusion"], reviewed["findings"][0]["conclusion"])
        finally:
            store.close()

    def test_pending_history_cleanup_preserves_dirty_or_unregistered_work(self):
        reviewed = self.reviewed_bundle()
        work = self.root / "publication-worktree"
        work.mkdir()
        for head, work_status, root_status in (("unregistered", "", ""), ("reviewed-commit", " M draft.md", ""),
                                              ("reviewed-commit", "", " M user.md")):
            with self.subTest(head=head, work_status=work_status, root_status=root_status):
                self.seed_ledger("verified_pending_history", base="base", commit="reviewed-commit", worktree=str(work),
                                 finding_ids=["finding-one"], questioning_review=reviewed["questioning_review"])

                def git(directory, *args):
                    if args[0] == "rev-parse":
                        return head if Path(directory) == work else "base"
                    if args[0] == "status":
                        return work_status if Path(directory) == work else root_status
                    self.fail("unsafe cleanup operation: " + repr(args))

                with patch("research_weekly.publish.git", side_effect=git), self.assertRaises(RuntimeError):
                    publish(self.config, self.path)
                self.assertEqual(json.loads(self.ledger_path.read_text())["status"], "verified_pending_history")
                self.assertTrue(work.is_dir())

    def test_pending_history_recording_is_idempotent(self):
        ledger = {"status": "verified_pending_history", "finding_ids": ["finding-one"]}
        record_publication(self.config, self.bundle, ledger)
        record_publication(self.config, self.bundle, ledger)
        store = Store(self.root)
        try:
            self.assertEqual(len(store.history()), 1)
        finally:
            store.close()

    def test_verified_replay_keeps_later_checkout_even_after_history_failure(self):
        reviewed = self.reviewed_bundle()
        self.seed_ledger("verified", base="base", commit="old-reviewed-commit", worktree=str(self.root / "old-worktree"),
                         finding_ids=["finding-one"], questioning_review=reviewed["questioning_review"])
        with patch("research_weekly.publish.git", return_value="later-article-commit") as git:
            self.assertEqual(publish(self.config, self.path)["status"], "verified")
            with patch("research_weekly.publish.record_publication", side_effect=sqlite3.OperationalError("database is locked")), \
                 self.assertRaises(sqlite3.OperationalError):
                publish(self.config, self.path)
            self.assertEqual(json.loads(self.ledger_path.read_text())["status"], "verified")
            self.assertEqual(publish(self.config, self.path)["status"], "verified")
            git.assert_not_called()
        store = Store(self.root)
        try:
            self.assertEqual(len(store.history()), 1)
            self.assertEqual(store.history()[0]["conclusion"], reviewed["findings"][0]["conclusion"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
