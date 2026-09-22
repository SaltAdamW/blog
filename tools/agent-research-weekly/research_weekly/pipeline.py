import datetime as dt
from collections import deque
import html
import json
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .codex import Codex
from .core import Store, atomic_json, atomic_text, digest, run_lock, utcnow
from .fetch import FetchError, discover_source, fetch_evidence
from .discovery import scan
from .scope import (EXPERIENCE_ORIGIN, check_experience, check_prose, eligible_date,
                    excluded as excluded_scope, is_experience, window)
from .publish import document_key, source_urls


def due(candidate, config, now):
    if candidate["retry_at"] and candidate["retry_at"] > now.isoformat():
        return False
    if candidate["status"] in ("pending", "needs_evidence", "ready"):
        return True
    if not candidate["fetched_at"]:
        return True
    return now - dt.datetime.fromisoformat(candidate["fetched_at"]) >= dt.timedelta(days=config["fetch"]["refresh_days"])


def select_queue(candidates, maximum):
    retries = [c for c in candidates if c["status"] == "needs_evidence"]
    others = sorted([c for c in candidates if c["status"] != "needs_evidence"],
                    key=lambda c: (c["status"] not in ("pending", "ready"), c.get("fetched_at") or "", c.get("discovered_at", "")))

    def diverse(items):
        groups = {}
        for candidate in items:
            # 新候选先于旧文刷新；同一层内轮转域名，避免高频信源占满预选池。
            tier = candidate["status"] not in ("pending", "ready", "needs_evidence")
            key = (tier, urlsplit(candidate.get("url", "")).hostname)
            groups.setdefault(key, deque()).append(candidate)
        result = []
        for tier in (False, True):
            queues = [q for (old, _), q in groups.items() if old == tier]
            while queues:
                for queue in queues:
                    result.append(queue.popleft())
                queues = [q for q in queues if q]
        return result

    retries = diverse(retries)
    first = retries[:max(1, maximum // 2)]
    return (first + diverse(others) + retries[len(first):])[:maximum]


def obtain_evidence(candidate, config, store, editor, fetcher, run_dir):
    try:
        return fetcher(candidate, config, store.root)
    except FetchError as first:
        if not config["codex"].get("search"):
            raise
        try:
            alternative = editor.alternatives(candidate, first.attempts)
            atomic_json(run_dir / f"recovery-{candidate['id']}.json", alternative)
            if not alternative["urls"]:
                raise FetchError("替代入口搜索无结果：" + alternative["reason"], first.attempts)
            store.add({"url": candidate["url"], "origin": "codex-recovery", "alternatives": alternative["urls"]})
            updated = store.get(candidate["id"])
            updated["_skip_routes"] = [a["url"] for a in first.attempts]
            evidence = fetcher(updated, config, store.root)
            evidence["attempts"] = first.attempts + evidence["attempts"]
            atomic_json(store.root / "evidence" / evidence["sha256"] / "evidence.json", evidence)
            return evidence
        except FetchError as second:
            raise FetchError(str(second), first.attempts + second.attempts) from second
        except Exception as exc:
            raise FetchError(f"下载失败且替代入口搜索失败：{exc}", first.attempts) from exc


def escaped(value):
    return re.sub(r"([\[\]`*_])", r"\\\1", html.escape(str(value), quote=False))


def render_report(run_id, start, end, selected, detail):
    weekly = detail["scope"] == "weekly"
    scope = {"weekly": "周报发现与筛选", "queue": "已有候选补充（未运行新信源发现）", "targeted": "定向处理（仅限指定候选）"}[detail["scope"]]
    sources = detail["source_results"]
    queried = [s for s in sources if s["kind"] not in ("search", "url") and s["status"] != "not_run"]
    successes = sum(s["status"] == "ok" for s in queried)
    direct = sum(s["status"] == "direct" for s in sources)
    search = next((s["status"] for s in sources if s["kind"] == "search"), "未运行")
    coverage_summary = (f"固定信源 {detail['configured_sources']} 个；实际查询 {len(queried)} 个，成功 {successes} 个，"
                        f"固定页登记 {direct} 个；联网搜索：{search}。"
                        f"本轮去重候选 {detail['discovered']} 条，新增入库 {detail['new_candidates']} 条。")
    lines = [f"# Agent 研究{'周报' if weekly else '补充报告'} | {start} 至 {end}", "",
             f"运行：`{run_id}`。重点 {len(selected)} 条。状态：{detail['status']}。", "",
             f"**运行范围：{scope}。**", "", coverage_summary, "",
             f"尝试处理 {detail['processed']} 篇；完成审稿 {detail['reviewed']} 篇（复用缓存 {detail['cached_reviews']} 篇）；"
             f"最终入选 {len({f['candidate_id'] for f in selected})} 篇的 {len(selected)} 条发现；"
             f"本轮可处理队列尚余 {detail['remaining']} 篇。", "",
             "选题经 Codex 审稿和单独事实复核，引用经过原文片段匹配；这不等于实验独立复现或消除了模型误判。", ""]
    if not weekly:
        lines += ["这是一份限定范围的补充报告，不能用入选数量推断本周研究数量或信源覆盖。", ""]
    groups = [("本期发现", lambda f: f["published_at"] and f["published_at"] >= start),
              ("历史经验", lambda f: f["published_at"] and f["published_at"] < start and is_experience(f)),
              ("经典补课", lambda f: f["published_at"] and f["published_at"] < start and not is_experience(f)),
              ("发布日期未核实", lambda f: not f["published_at"])]
    for title, predicate in groups:
        items = [f for f in selected if predicate(f)]
        if not items:
            continue
        lines += [f"## {title}", ""]
        for finding in items:
            lines += [f"### {escaped(finding['title'])}", "", f"发现 ID：`{finding['id']}`", "",
                      f"**核心发现**：{escaped(finding['conclusion'])}", "",
                      f"**问题**：{escaped(finding['problem'])}", "",
                      f"**机制与证据**：{escaped(finding['mechanism'])}", "",
                      "**局限与负结果**：" + "；".join(escaped(v.rstrip("。；;")) for v in finding["limitations"]) + "。", "",
                      f"**工程启示（推论）**：{escaped(finding['implication'])}", "",
                      f"**最小验证（建议）**：{escaped(finding['experiment'])}", ""]
            blocks = {b["id"]: b for b in finding["source"]["blocks"]}
            locators = []
            for ev in finding["evidence"]:
                block = blocks[ev["block_id"]]
                locators.append(f"p{block['page']} / {block['id']}" if finding["source"]["kind"] == "pdf" else block["id"])
            raw = Path(finding["source"]["raw_path"])
            lines += [f"原文：[来源](<{finding['url']}>)；[本地快照](../evidence/{finding['source']['sha256']}/{raw.name})。", "",
                      f"证据定位：{', '.join(dict.fromkeys(locators))}。SHA-256：`{finding['source']['sha256'][:16]}`。", "",
                      f"发布日期：{finding['published_at'] or '未核实'}；采集时间：{finding['source']['retrieved_at']}。", ""]
            if finding["source"]["notes"]:
                lines += ["提取限制：" + "；".join(escaped(n) for n in finding["source"]["notes"]), ""]
            coverage = finding.get("reading_coverage")
            if coverage:
                lines += [f"阅读范围：{coverage['shown_blocks']}/{coverage['total_blocks']} 个正文块；全文快照保留供回查。", ""]
    if not selected:
        lines += ["本轮未发布新发现；未处理和待补证候选不代表低质量。不用低质量条目补足数量。", ""]
    lines += ["## 覆盖与待补证", "", f"本轮处理 {detail['processed']} 条候选；队列剩余 {detail['remaining']} 条。", ""]
    if sources:
        lines += ["| 信源 | 类别 | 状态 | 采集条目 | 符合日期范围或日期未知 | 新增入库 |",
                  "| --- | --- | --- | ---: | ---: | ---: |"]
        for source in sources:
            name = escaped(source["source"]).replace("|", "\\|").replace("\n", " ")
            lines += [f"| {name} | {escaped(source['group'])} | {source['status']} | {source['collected']} | {source['eligible']} | {source['added']} |"]
        lines += ["", "采集条目受每源上限限制，各源条目可重复；固定页登记（direct）不是抓取成功。日期未知的条目保留候选身份，不等于本周新作；历史经验只限制截止日，不限起始日。", ""]
    counts = detail["review_counts"]
    lines += [f"审稿决定：入选候选 {counts['select']}，观察 {counts['watch']}，不入选 {counts['reject']}，要求补证 {counts['needs_evidence']}。", ""]
    for item in detail.get("unprocessed", [])[:10]:
        lines += [f"- 排队未处理：{escaped(item['title'])} (`{item['id']}`)"]
    if detail["remaining"] > 10:
        lines += [f"- 另有 {detail['remaining'] - 10} 条未列出，完整队列见运行 manifest。"]
    for error in detail["source_errors"]:
        lines += [f"- 信源缺口：{escaped(error['source'])}：{escaped(error['error'])}"]
    for pending in detail["pending"]:
        lines += [f"- 待补证：{escaped(pending['title'])} (`{pending['id']}`)：{escaped(pending['reason'])}"]
    for item in detail.get("editorial_followup", []):
        lines += [f"- 编辑跟踪：{escaped(item['title'])}：{escaped(item['reason'])}"]
    if not detail["source_errors"] and not detail["pending"]:
        lines += ["本轮已处理范围内无已知抓取缺口；不代表覆盖了互联网所有相关研究。"]
    if detail.get("discovery_skipped"):
        lines += ["", "本轮跳过新信源发现，仅处理已有候选。"]
    lines += ["", f"Codex 调用：{detail['codex_calls']}；观测到的 token 用量：`{json.dumps(detail['usage'], ensure_ascii=False)}`。",
              "费用取决于本机配置的模型与计费方式，本工具不把 token 数伪装成精确费用。", ""]
    return "\n".join(lines)


def run(config, *, discover=True, force=False, limit=None, only=None, now=None,
        editor_factory=Codex, fetcher=fetch_evidence, source_reader=discover_source, start=None, end=None, record_reported=True):
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(ZoneInfo(config["timezone"]))
    week = f"{local.isocalendar().year}-W{local.isocalendar().week:02d}"
    start, end = window(local, start, end)
    scope = "targeted" if only else "weekly" if discover else "queue"
    experiences_enabled = bool(config.get("discovery", {}).get("experience_topics"))
    signature = digest(json.dumps({"scope": scope, "only": sorted(only or []), "discover": discover,
                                   "sources": config["sources"] if discover else [],
                                   "topics": config["topics"] if discover else [],
                                   "search": config["codex"].get("search"),
                                   "limit": limit or config["limits"]["candidates"], "start": start, "end": end,
                                   "discovery": config.get("discovery", {}),
                                   "rules": digest((Path(__file__).parents[1] / "editorial.md").read_bytes()),
                                   "record_reported": record_reported,
                                   "format": 4}, sort_keys=True))
    with run_lock(config["state_dir"]):
        store = Store(config["state_dir"])
        try:
            previous = next((r for r in store.db.execute(
                "SELECT * FROM runs WHERE week=? AND status IN ('complete','partial') ORDER BY started_at DESC", (week,))
                if json.loads(r["detail"]).get("signature") == signature), None)
            if previous and not force:
                return {"status": "already_done", "run_id": previous["id"], "report": previous["report"],
                        "bundle": str(store.root / "runs" / previous["id"] / "bundle.json")}
            # 拿到 OS 锁说明旧 running 记录已无持锁进程，只标记中断，不回放外部动作。
            with store.db:
                store.db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (utcnow(),))
            run_id = f"{week}-{local.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
            run_dir = store.root / "runs" / run_id
            run_dir.mkdir(parents=True, mode=0o700)
            with store.db:
                store.db.execute("INSERT INTO runs(id,week,status,started_at) VALUES(?,?,'running',?)", (run_id, week, utcnow()))
            deadline = time.monotonic() + config["limits"]["run_seconds"]
            editor = editor_factory(config, run_dir / "codex", deadline)
            detail = {"scope": scope, "signature": signature, "source_errors": [], "source_results": [],
                      "configured_sources": len(config["sources"]), "discovered": 0, "new_candidates": 0,
                      "pending": [], "processed": 0, "reviewed": 0, "cached_reviews": 0, "remaining": 0,
                      "review_counts": {key: 0 for key in ("select", "watch", "reject", "needs_evidence")},
                      "discovery_skipped": not discover, "start": start, "end": end, "candidate_decisions": []}
            known = {c["id"] for c in store.list()}
            discovered = set()

            def ingest(items, record):
                for item in items:
                    record["collected"] += 1
                    disposition = "queued"
                    if excluded_scope(item.get("title", "") + " " + item.get("focus", "")):
                        disposition = "excluded_scope"
                    experience = experiences_enabled and is_experience(item)
                    candidate_date = item.get("published_at")
                    if disposition == "queued" and not eligible_date(candidate_date[:10] if candidate_date else None,
                            start, end, experience=experience):
                        disposition = "outside_window"
                    decision = {"url": item["url"], "title": item.get("title", ""), "round": record["source"],
                                "track": "experience" if experience else "current", "disposition": disposition}
                    detail["candidate_decisions"].append(decision)
                    if disposition != "queued":
                        continue
                    record["eligible"] += 1
                    identifier = store.add({**item, "origin": EXPERIENCE_ORIGIN} if experience else item)
                    discovered.add(identifier)
                    if identifier not in known:
                        known.add(identifier)
                        record["added"] += 1
                        detail["new_candidates"] += 1
                detail["discovered"] = len(discovered)

            try:
                if discover:
                    for source in config["sources"]:
                        record = {"source": source["name"], "kind": source.get("kind", "unknown"),
                                  "group": source.get("group", "未分类"), "url": source.get("url", ""),
                                  "status": "not_run", "collected": 0, "eligible": 0, "added": 0}
                        detail["source_results"].append(record)
                        try:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("本轮预算已耗尽，尚未查询此信源")
                            record["status"] = "failed"
                            ingest(source_reader(source, config), record)
                            record["status"] = "direct" if record["kind"] == "url" else "ok"
                        except Exception as exc:
                            detail["source_errors"].append({"source": source["name"], "error": str(exc)[:1500]})
                        atomic_json(run_dir / "discovery.json", detail)
                        print(f"[source] {source['name']}: {record['status']} / {record['collected']} 条 / 新增 {record['added']}", flush=True)
                    if config["codex"].get("search"):
                        recent = [{**{k: c[k] for k in ("title", "url", "focus", "status", "published_at")},
                                   "track": "experience" if is_experience(c) else "current"}
                                  for c in store.list() if c["published_at"] and eligible_date(c["published_at"], start, end,
                                      experience=experiences_enabled and is_experience(c))]
                        detail["discovery_context_omitted"] = max(0, len(recent) - 100)
                        # 检索上下文不是全库；去重和重试仍依据持久化状态，不丢弃未传入的候选。
                        recent = recent[-100:]
                        scan(editor, config, start, end, run_dir, recent, ingest, detail)
                publishing = not record_reported and config.get("publication", {}).get("enabled")
                candidates = [c for c in store.list() if (c["id"] in only if only else
                              due(c, config, now) or publishing and c["status"] == "selected")]
                if only and set(only) - {c["id"] for c in candidates}:
                    raise ValueError("指定的候选 ID 不存在")
                if discover and not only:
                    candidates = [c for c in candidates if eligible_date(c["published_at"], start, end,
                                  experience=experiences_enabled and is_experience(c))]
                    published = source_urls(Path(config["publication"]["checkout"])) if config.get("publication", {}).get("enabled") else set()
                    for candidate in candidates:
                        if document_key(candidate["url"]) in published:
                            detail["candidate_decisions"].append({"url": candidate["url"], "disposition": "already_published"})
                    candidates = [c for c in candidates if document_key(c["url"]) not in published]
                kept = []
                for candidate in candidates:
                    if excluded_scope(candidate["title"] + " " + candidate["focus"]):
                        detail["candidate_decisions"].append({"url": candidate["url"], "disposition": "excluded_scope"})
                    else:
                        kept.append(candidate)
                candidates = kept
                maximum = limit or config["limits"]["candidates"]
                queue = select_queue(candidates, maximum)
                if len(candidates) > maximum and not only:
                    # 元数据预选不替代精读判断；队列太大时先提供最多100条，余项仍保留。
                    current = [c for c in candidates if c["id"] in discovered]
                    backlog = [c for c in candidates if c["id"] not in discovered]
                    planning_pool = (select_queue(current, 100) + select_queue(backlog, 100))[:100]
                    try:
                        plan = editor.prioritize(planning_pool, maximum)
                        by_candidate = {c["id"]: c for c in candidates}
                        queue = [by_candidate[p["id"]] for p in plan["priority"]]
                        atomic_json(run_dir / "priority.json", plan)
                    except Exception as exc:
                        detail["source_errors"].append({"source": "codex-prioritize", "error": str(exc)[:1500]})
                requested = [c for c in candidates if "user-request" in json.loads(c["origins"])]
                requested_ids = {c["id"] for c in requested}
                queue = (requested + [c for c in queue if c["id"] not in requested_ids])[:maximum]
                findings = []
                history = store.history()
                reported = {r[0] for r in store.db.execute("SELECT finding_id FROM reported")}
                if publishing:
                    published = source_urls(Path(config["publication"]["checkout"]))
                    published_ids = {c["id"] for c in store.list() if document_key(c["url"]) in published}
                    history = [r for r in history if r["candidate_id"] in published_ids]
                    reported = {r["finding_id"] for r in store.db.execute("SELECT finding_id,candidate_id FROM reported")
                                if r["candidate_id"] in published_ids}
                feedback = store.feedback()
                processed_ids = set()
                for candidate in queue:
                    if time.monotonic() >= deadline:
                        break
                    try:
                        fresh = candidate["fetched_at"] and now - dt.datetime.fromisoformat(candidate["fetched_at"]) < dt.timedelta(days=config["fetch"]["refresh_days"])
                        if fresh and candidate["evidence_hash"]:
                            evidence = json.loads((store.root / "evidence" / candidate["evidence_hash"] / "evidence.json").read_text())
                            if digest(Path(evidence["raw_path"]).read_bytes()) != candidate["evidence_hash"]:
                                store.update(candidate["id"], evidence_hash=None, fetched_at=None, reviewed_hash=None)
                                raise ValueError("本地证据快照 hash 不匹配")
                        else:
                            evidence = obtain_evidence(candidate, config, store, editor, fetcher, run_dir)
                            store.update(candidate["id"], evidence_hash=evidence["sha256"], fetched_at=utcnow(), status="ready")
                        atomic_json(run_dir / f"fetch-{candidate['id']}.json", evidence["attempts"])
                        cached = json.loads(candidate["review"]) if candidate["review"] else None
                        rules_hash = digest(editor.rules) if hasattr(editor, "rules") else "test"
                        if (candidate["reviewed_hash"] == evidence["sha256"] and cached
                                and cached.get("rules_hash") == rules_hash
                                and cached.get("experience_candidate", False) == is_experience(candidate)
                                and (cached["decision"] != "select" or cached.get("audit_verified"))):
                            review = cached
                            detail["cached_reviews"] += 1
                        else:
                            review = editor.review(candidate, evidence, feedback)
                        review["rules_hash"] = rules_hash
                        review["experience_candidate"] = is_experience(candidate)
                        for finding in review["findings"]:
                            check_prose(finding["title"], finding["paragraphs"])
                            if is_experience(candidate):
                                check_experience(finding)
                        decision = review["decision"]
                        detail["review_counts"][decision] += 1
                        if decision != "needs_evidence":
                            detail["reviewed"] += 1
                        store.update(candidate["id"], status={"select": "selected", "reject": "rejected"}.get(decision, decision),
                                     review=json.dumps(review, ensure_ascii=False),
                                     reviewed_hash=evidence["sha256"] if decision != "needs_evidence" else None,
                                     reason=review["reason"], failures=0,
                                     retry_at=(now + dt.timedelta(hours=config["fetch"]["retry_hours"])).isoformat() if decision == "needs_evidence" else None)
                        atomic_json(run_dir / f"review-{candidate['id']}.json", review)
                        for finding in review["findings"]:
                            fid = digest(candidate["id"] + evidence["sha256"] + finding["key"])[:24]
                            if fid in reported:
                                continue
                            experience = is_experience(candidate) or experiences_enabled and finding["category"] == "practice"
                            verified_date = review.get("published_at")
                            if verified_date and not eligible_date(verified_date, start, end,
                                    experience=experience or scope != "weekly"):
                                detail["candidate_decisions"].append({"url": candidate["url"],
                                    "disposition": "verified_date_outside_scope", "verified_date": verified_date})
                                continue
                            findings.append({**finding, "id": fid, "candidate_id": candidate["id"], "url": candidate["url"],
                                             "track": "experience" if experience else "current",
                                             "published_at": verified_date, "source": evidence,
                                             "verified_date": review.get("published_at"),
                                             "date_evidence": review.get("date_evidence", []),
                                             "source_title": candidate["title"],
                                             "audit_verified": review.get("audit_verified", False),
                                             "reading_coverage": review.get("coverage")})
                    except Exception as exc:
                        reason = f"{type(exc).__name__}: {exc}"[:2000]
                        store.update(candidate["id"], status="needs_evidence", reason=reason,
                                     failures=candidate["failures"] + 1,
                                     retry_at=(now + dt.timedelta(hours=config["fetch"]["retry_hours"])).isoformat())
                        atomic_json(run_dir / f"failure-{candidate['id']}.json", {"reason": reason, "attempts": getattr(exc, "attempts", [])})
                    detail["processed"] += 1
                    processed_ids.add(candidate["id"])
                    current = store.get(candidate["id"])
                    detail["candidate_decisions"].append({"url": candidate["url"], "disposition": current["status"], "reason": current["reason"]})
                    print(f"[{detail['processed']}/{len(queue)}] {store.get(candidate['id'])['status']}: {candidate['title']}", flush=True)
                detail["remaining"] = max(0, len(candidates) - detail["processed"])
                detail["unprocessed"] = [{k: c[k] for k in ("id", "title", "status")} for c in candidates if c["id"] not in processed_ids]
                editing = editor.edit(findings, history, feedback) if findings else {"selected": [], "excluded": []}
                by_id = {f["id"]: f for f in findings}
                selected = [by_id[i] for i in editing["selected"]]
                for item in editing["excluded"]:
                    detail["candidate_decisions"].append({"url": by_id[item["id"]]["url"], **item})
                detail["editorial_followup"] = []
                for excluded in editing["excluded"]:
                    finding = by_id[excluded["id"]]
                    if excluded["disposition"] in ("watch", "needs_evidence"):
                        detail["editorial_followup"].append({"title": finding["title"], "reason": excluded["reason"]})
                    if excluded["disposition"] == "needs_evidence":
                        store.update(finding["candidate_id"], status="needs_evidence", reviewed_hash=None,
                                     reason="主编要求补证：" + excluded["reason"],
                                     retry_at=(now + dt.timedelta(hours=config["fetch"]["retry_hours"])).isoformat())
                detail["pending"] = [{k: c[k] for k in ("id", "title", "reason")} for c in store.list() if c["status"] == "needs_evidence"]
                detail["status"] = "partial" if detail["pending"] or detail["source_errors"] or detail["remaining"] else "complete"
                detail["codex_calls"] = editor.calls
                detail["usage"] = {key: sum(u.get(key, 0) for u in editor.usage) for key in ("input_tokens", "cached_input_tokens", "output_tokens")}
                atomic_json(run_dir / "editor.json", editing)
                bundle = run_dir / "bundle.json"
                atomic_json(bundle, {"version": 1, "run_id": run_id, "start": start, "end": end,
                                     "scope": scope, "findings": selected,
                                     "discovery_rounds": detail.get("discovery_rounds", []),
                                     "source_errors": detail["source_errors"], "remaining": detail["remaining"]})
                atomic_json(run_dir / "manifest.json", {"run_id": run_id, "selected": [f["id"] for f in selected], **detail})
                report = store.root / "reports" / f"{run_id}.md"
                body = render_report(run_id, start, end, selected, detail)
                atomic_text(report, body)
                with store.db:
                    for finding in selected:
                        if not record_reported:
                            continue
                        store.db.execute("INSERT OR IGNORE INTO reported VALUES(?,?,?,?,?,?)", (
                            finding["id"], run_id, finding["candidate_id"], finding["source"]["sha256"], finding["title"], finding["conclusion"]))
                    store.db.execute("UPDATE runs SET status=?,finished_at=?,report=?,detail=? WHERE id=?", (
                        detail["status"], utcnow(), str(report), json.dumps(detail, ensure_ascii=False), run_id))
                atomic_text(store.root / "reports" / "latest.md", body)
                atomic_text(store.root / "reports" / ("latest-weekly.md" if scope == "weekly" else "latest-supplement.md"), body)
                return {"run_id": run_id, "status": detail["status"], "report": str(report), "bundle": str(bundle), "selected": len(selected), **detail}
            except BaseException as exc:
                with store.db:
                    store.db.execute("UPDATE runs SET status='failed',finished_at=?,detail=? WHERE id=?", (
                        utcnow(), json.dumps({"error": str(exc)[:2000]}, ensure_ascii=False), run_id))
                raise
        finally:
            store.close()
