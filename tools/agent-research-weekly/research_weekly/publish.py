"""把审稿通过的自然段发布到既有博客；本地生成、推送、上线分别留证。"""

import datetime as dt
from html import escape
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .core import Store, atomic_json, atomic_text, canonical_url, command, digest, run_lock, utcnow
from .scope import check_experience, check_prose, eligible_date, excluded, is_experience
from .questioning import ReviewBlocked, content_hash, load_verified, prepare as prepare_questioning


def git(root, *args):
    return command(["git", *args], cwd=root, timeout=90)[0].rstrip()


def api(config, path):
    return json.loads(command(["gh", "api", f"repos/{config['repository']}/{path}"], timeout=60)[0])


def safe_path(root, name):
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError("博客路径超出检出目录")
    return target


def document_key(url):
    canonical = canonical_url(url)
    parts = urlsplit(canonical)
    if parts.hostname in ("arxiv.org", "www.arxiv.org"):
        match = re.fullmatch(r"/(?:abs|html|pdf)/(\d{4}\.\d{4,5})(v\d+)?(?:\.pdf)?/?", parts.path)
        if match:
            return "arxiv:" + match.group(1) + (match.group(2) or "v1")
    return canonical.rstrip("/")


def source_urls(root):
    index = root / "posts.json"
    if not index.exists():
        return set()
    result = set()
    for post in json.loads(index.read_text()):
        if post.get("source_manifest"):
            manifest = json.loads(safe_path(root, post["source_manifest"]).read_text())
            result.update(document_key(s["url"]) for s in manifest["sources"])
    return result


def validate_bundle(bundle, state, allow_supplement=False):
    if bundle["version"] != 1:
        raise ValueError("未知公开稿格式")
    start, end = (dt.date.fromisoformat(bundle[k]) for k in ("start", "end"))
    if start > end:
        raise ValueError("日期窗口倒置")
    if bundle["scope"] != "weekly" and not allow_supplement:
        raise ValueError("限定范围报告不能冒充完整周报")
    if bundle["scope"] == "weekly":
        completed = [r for r in bundle["discovery_rounds"] if r["status"] in ("ok", "partial") and r["queries"]]
        if len(completed) < 3:
            raise ValueError("不足三轮有实际查询记录的开放检索，不能发布周报")
    accepted, deferred = [], []
    for finding in bundle["findings"]:
        check_prose(finding["title"], finding["paragraphs"])
        if finding.get("track", "current") not in ("current", "experience"):
            raise ValueError("未知选读类型")
        if is_experience(finding):
            check_experience(finding)
        if not finding.get("audit_verified"):
            raise ValueError("正文未通过独立事实复核")
        url = canonical_url(finding["url"])
        source = finding["source"]
        raw = Path(source["raw_path"]).resolve()
        if not raw.is_relative_to((state / "evidence").resolve()) or digest(raw.read_bytes()) != source["sha256"]:
            raise ValueError("来源快照路径或内容哈希不匹配")
        blocks = {b["id"]: b["text"] for b in source["blocks"]}
        for reference in finding["evidence"] + finding.get("date_evidence", []):
            if " ".join(reference["quote"].split()) not in " ".join(blocks.get(reference["block_id"], "").split()):
                raise ValueError("公开正文来源引用未匹配")
        date = finding.get("verified_date")
        if not date or not finding.get("date_evidence") or not eligible_date(
                date, bundle["start"], bundle["end"], experience=is_experience(finding)):
            deferred.append({"url": url, "reason": "原始日期未核实或不在本期窗口"})
            continue
        accepted.append(finding)
    return accepted, deferred


def label(value):
    date = dt.date.fromisoformat(value)
    return f"{date.year} 年 {date.month} 月 {date.day} 日"


def markdown_text(value):
    return escape(value, quote=False).replace("[", "\\[").replace("]", "\\]")


def sync_drafts(config, bundle, ledger):
    root = Path(config["publication"]["checkout"])
    source = f"agent-research-weekly-{bundle['end']}.md"
    body = command(["git", "show", f"{ledger['commit']}:{source}"], cwd=root)[0]
    drafts = Path(config["_path"]).parent / "drafts"
    targets = list(drafts.glob(f"*/{bundle['end']}.md"))
    if not targets:
        targets = [drafts / f"{bundle['start']}_{bundle['end']}" / f"{bundle['end']}.md"]
    results = []
    for path in targets:
        if path.exists() and path.read_text() != body:
            previous = command(["git", "show", f"{ledger['base']}:{source}"], cwd=root)[0]
            if path.read_text() != previous:
                results.append({"path": str(path), "status": "kept_user_changes"})
                continue
        atomic_text(path, body)
        results.append({"path": str(path), "status": "synced"})
    return results


def record_publication(config, bundle, ledger):
    if ledger.get("status") not in ("verified", "verified_pending_history") or not ledger.get("finding_ids"):
        return
    store = Store(config["state_dir"])
    try:
        with store.db:
            for finding in bundle["findings"]:
                if finding["id"] in ledger["finding_ids"]:
                    store.db.execute("INSERT OR IGNORE INTO reported VALUES(?,?,?,?,?,?)", (
                        finding["id"], bundle["run_id"], finding["candidate_id"], finding["source"]["sha256"],
                        finding["title"], finding["conclusion"]))
    finally:
        store.close()


def merge_issue(root, bundle, findings):
    posts = json.loads((root / "posts.json").read_text())
    existing_urls = source_urls(root)
    findings = [f for f in findings if document_key(f["url"]) not in existing_urls]
    if not findings:
        return None
    end = bundle["end"]
    slug = f"weekly-agent-research-{end}"
    source_name = f"agent-research-weekly-{end}.md"
    manifest_name = f"agent-research-weekly-{end}.sources.json"
    post = next((p for p in posts if p["slug"] == slug), None)
    if bundle["scope"] != "weekly" and not post:
        raise ValueError("定向补稿只能追加到已有一期，不能创建新的完整周报")
    title = f"Research 周报：{label(bundle['start'])}至 {label(end)}"
    titles = list(dict.fromkeys(f["title"].split("：")[0] for f in findings))
    summary = "本期选读" + "、".join(titles[:8]) + "等材料。"
    if post:
        source_name, manifest_name = post["source"], post["source_manifest"]
        body = safe_path(root, source_name).read_text()
        manifest = json.loads(safe_path(root, manifest_name).read_text())
        if manifest.get("window", {}) != {"start": bundle["start"], "end": end}:
            raise ValueError("已有周报日期窗口不同，拒绝覆盖或混入")
        post["description"] = post["description"].rstrip("。") + "；补读" + "、".join(titles) + "。"
        post["deck"] = post["deck"].rstrip("。") + "；补读" + "、".join(titles) + "。"
        if "toc_labels" in post:
            raise ValueError("已有人工目录标签，需要人工同步，拒绝覆盖")
        manifest["verification"] = "既有条目保留各自核读范围；本次新增条目核读原文，并由另一次模型调用复核事实。所有条目均未复现实验。"
    else:
        body = f"# {title}\n\n{summary}\n"
        manifest = {"window": {"start": bundle["start"], "end": end}, "sources": [],
                    "verification": "各条目核读原文，并由另一次模型调用复核事实，未复现实验。",
                    "coverage": "多轮开放检索后的选读，不预设专题，不声称穷尽。"}
        post = {"slug": slug, "title": title, "date": end, "category": "研究周报", "tags": titles[:6],
                "description": summary, "deck": summary, "source": source_name, "source_manifest": manifest_name}
        posts.append(post)
    for finding in findings:
        body += f"\n## {markdown_text(finding['title'])}\n\n"
        historical = is_experience(finding) and finding["verified_date"] < bundle["start"]
        marker = " · 历史经验，非本周新作" if historical else " · 场景经验" if is_experience(finding) else ""
        body += f"原文：[{markdown_text(finding['source_title'])}](<{canonical_url(finding['url'])}>) · {finding['verified_date']}{marker}\n\n"
        body += "\n\n".join(markdown_text(p) for p in finding["paragraphs"]) + "\n"
        coverage = finding.get("reading_coverage") or {}
        source = {"id": finding["id"], "title": finding["source_title"], "url": canonical_url(finding["url"]),
                  "published_at": finding["verified_date"], "review_status": "primary_sections_read",
                  "reading_scope": f"正文取材范围 {coverage.get('shown_blocks', '?')}/{coverage.get('total_blocks', '?')} 个原文块，由另一次模型调用复核，未复现实验。",
                  "source_sha256": finding["source"]["sha256"]}
        if bundle.get("questioning_review"):
            source["reading_scope"] += " 发布前另经双角色十轮追问、事实复核与解释复查，仍非独立复现。"
        if is_experience(finding):
            source["selection_track"] = "experience"
            source["historical"] = historical
        if source["url"] not in {s["url"] for s in manifest["sources"]}:
            manifest["sources"].append(source)
    if excluded(body):
        raise ValueError("合并后正文含范围外内容")
    post["word_count"] = len(re.findall(r"[\u4e00-\u9fff]", body))
    manifest["checkedAt"] = utcnow()[:10]
    manifest["latest_update"] = {
        "run_id": bundle["run_id"], "scope": bundle["scope"],
        "search_rounds": len(bundle.get("discovery_rounds", [])),
        "recorded_queries": sum(len(r.get("queries", [])) for r in bundle.get("discovery_rounds", [])),
        "unprocessed_candidates": bundle.get("remaining", 0),
        "source_gaps": len(bundle.get("source_errors", [])),
        "note": "选读不等于穷尽；未处理材料不代表低质量。"
    }
    atomic_text(safe_path(root, source_name), body)
    atomic_json(safe_path(root, manifest_name), manifest)
    atomic_json(root / "posts.json", posts)
    return {"source": source_name, "source_manifest": manifest_name, "page": f"posts/{slug}/index.html",
            "added": len(findings), "finding_ids": [f["id"] for f in findings]}


def online_check(settings, commit, files):
    timeout = time.monotonic() + settings.get("deploy_timeout_seconds", 900)
    deployment = None
    while time.monotonic() < timeout:
        runs = api(settings, f"actions/runs?head_sha={commit}&per_page=20")["workflow_runs"]
        matches = [r for r in runs if r["head_sha"] == commit and (
            r.get("path") == "dynamic/pages/pages-build-deployment" or
            not r.get("path") and r.get("name") in ("pages-build-deployment", "pages build and deployment"))]
        if matches:
            deployment = matches[0]
            if deployment["status"] == "completed":
                if deployment["conclusion"] != "success":
                    raise RuntimeError(f"Pages 部署失败：{deployment['html_url']}")
                break
        time.sleep(15)
    else:
        raise RuntimeError("等待 Pages 部署超时，已推送提交保留供恢复")
    if api(settings, "commits/" + settings["branch"])["sha"] != commit:
        raise RuntimeError("线上分支已前进，不能把新版本误认成本次发布")
    verified = []
    for name, expected in files.items():
        error = None
        for attempt in range(4):
            try:
                url = settings["site_url"].rstrip("/") + "/" + name + "?v=" + commit[:12]
                with urlopen(Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "agent-weekly-verifier"}), timeout=40) as response:
                    data = response.read()
                if digest(data) != expected:
                    raise ValueError("线上内容哈希不匹配：" + name)
                verified.append(name)
                error = None
                break
            except (OSError, ValueError) as exc:
                error = exc
                if attempt < 3:
                    time.sleep(5)
        if error:
            raise error
        print("[online] " + name, flush=True)
    return {"pages_run": deployment["id"], "pages_url": deployment["html_url"], "verified_files": verified}


def _queue_publication(path, intent):
    # 完整写入后才原子入队；硬链接不替换已有台账，包括并发发布刚更新的状态。
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".pending-") as pending:
        json.dump(intent, pending, ensure_ascii=False, indent=2)
        pending.write("\n")
        pending.flush()
        os.fsync(pending.fileno())
        try:
            os.link(pending.name, path)
        except FileExistsError:
            pass


def _finish_publication_history(config, bundle, ledger, ledger_path):
    ledger["status"] = "verified_pending_history"
    ledger.pop("error", None)
    atomic_json(ledger_path, ledger)
    try:
        record_publication(config, bundle, ledger)
        if ledger.get("commit"):
            root = Path(config["publication"]["checkout"])
            work = Path(ledger["worktree"]) if ledger.get("worktree") else None
            if work and work.exists():
                if (git(work, "rev-parse", "HEAD") != ledger["commit"]
                        or git(work, "status", "--porcelain", "--untracked-files=all")):
                    raise RuntimeError("发布检出含未登记提交或未提交改动，保留现场，停止收尾")
            head = git(root, "rev-parse", "HEAD")
            if head == ledger.get("base"):
                if git(root, "status", "--porcelain", "--untracked-files=all"):
                    raise RuntimeError("用户检出有未提交改动，停止收尾快进")
                git(root, "merge", "--ff-only", ledger["commit"])
            elif head != ledger["commit"]:
                raise RuntimeError("用户检出与发布基线或已验证提交不一致，停止收尾")
            if work and work.exists():
                git(root, "worktree", "remove", str(work))
            ledger["drafts"] = sync_drafts(config, bundle, ledger)
    except Exception as exc:
        ledger["error"] = str(exc)
        atomic_json(ledger_path, ledger)
        raise
    ledger["status"] = "verified"
    atomic_json(ledger_path, ledger)


def publish(config, bundle_path, *, allow_supplement=False):
    settings = config["publication"]
    if not settings.get("enabled"):
        raise ValueError("未配置自动发布")
    # 发布身份和目标来自人工配置，绝不接受模型返回远端、分支或命令。
    if settings["repository"] != "SaltAdamW/blog" or settings["branch"] != "main" or settings["site_url"] != "https://saltadamw.github.io/blog/":
        raise ValueError("发布目标与已授权博客不一致")
    state = Path(config["state_dir"])
    bundle_path = Path(bundle_path).resolve()
    if not bundle_path.is_relative_to((state / "runs").resolve()):
        raise ValueError("只能发布本工具运行目录中的审稿结果")
    bundle_bytes = bundle_path.read_bytes()
    bundle = json.loads(bundle_bytes)
    accepted, deferred = validate_bundle(bundle, state, allow_supplement)
    publication_dir = state / "publications" / bundle_path.parent.name
    publication_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = publication_dir / "status.json"
    _queue_publication(ledger_path, {
        "status": "pending", "bundle": str(bundle_path), "bundle_sha256": digest(bundle_bytes),
        "deferred": deferred, "allow_supplement": allow_supplement,
    })
    with run_lock(state / "publish-lock"):
        ledger = json.loads(ledger_path.read_text())
        current_bytes = bundle_path.read_bytes()
        if current_bytes != bundle_bytes:
            bundle = json.loads(current_bytes)
            accepted, deferred = validate_bundle(bundle, state, allow_supplement)
        bundle_hash = digest(current_bytes)
        if ledger.get("bundle_sha256", bundle_hash) != bundle_hash:
            if ledger.get("commit"):
                raise ValueError("待发布稿在重试期间发生变化，拒绝复用旧提交")
            previous_work = Path(ledger["worktree"]) if ledger.get("worktree") else None
            if previous_work and previous_work.exists() and git(previous_work, "rev-parse", "HEAD") != ledger.get("base"):
                raise RuntimeError("上次检出含未登记提交，保留现场，需要检查后恢复")
            prior = {k: v for k, v in ledger.items() if k != "previous_reviews"}
            ledger = {"bundle": str(bundle_path), "deferred": deferred, "allow_supplement": allow_supplement,
                      "status": "pending", "previous_reviews": [*ledger.get("previous_reviews", []), prior],
                      **{k: ledger[k] for k in ("base", "worktree", "attempt", "previous_worktrees") if k in ledger}}
        ledger["bundle_sha256"] = bundle_hash
        if ledger.get("status") == "pending":
            atomic_json(ledger_path, ledger)
        if ledger.get("questioning_review") and (ledger.get("commit") or ledger.get("status") in ("verified", "verified_pending_history")):
            reviewed = load_verified(bundle_path, ledger["questioning_review"])
            if bundle.get("questioning_review") is not None:
                if bundle != reviewed:
                    raise ValueError("提交稿与已追问通过的稿件不匹配")
            elif content_hash(bundle) != ledger["questioning_review"]["input_sha256"]:
                raise ValueError("发布原稿与冻结的追问输入不一致")
            bundle = reviewed
            validate_bundle(bundle, state, allow_supplement)
        if ledger.get("status") == "verified":
            record_publication(config, bundle, ledger)
            return ledger
        if ledger.get("status") == "verified_pending_history":
            _finish_publication_history(config, bundle, ledger, ledger_path)
            return ledger
        if ledger.get("commit"):
            remote = api(settings, "commits/" + settings["branch"])["sha"]
            if remote == ledger.get("base") and not ledger.get("questioning_review"):
                error = "旧版待推送提交没有追问记录，禁止恢复推送；需以新运行重新审稿"
                ledger.update(status="needs_revision", error=error, checked_at=utcnow())
                atomic_json(ledger_path, ledger)
                raise RuntimeError(error)
            work = Path(ledger["worktree"])
            if remote == ledger.get("base") and work.is_dir() and git(work, "rev-parse", "HEAD") == ledger["commit"]:
                if git(work, "status", "--porcelain", "--untracked-files=all"):
                    raise RuntimeError("待恢复发布检出有未提交改动，停止推送")
                root = Path(settings["checkout"])
                if git(root, "rev-parse", "HEAD") != ledger["base"] or git(root, "status", "--porcelain", "--untracked-files=all"):
                    raise RuntimeError("用户检出与发布基线不一致，停止恢复推送")
                git(work, "push", "origin", "HEAD:refs/heads/" + settings["branch"])
                remote = api(settings, "commits/" + settings["branch"])["sha"]
            if remote != ledger["commit"]:
                raise RuntimeError("上次发布已生成提交但远端不一致，需要检查后恢复，禁止重复生成或覆盖")
            ledger.update(online_check(settings, ledger["commit"], ledger["files"]))
            ledger["verified_at"] = utcnow()
            _finish_publication_history(config, bundle, ledger, ledger_path)
            return ledger
        if not accepted:
            ledger.update(status="no_publishable_content", checked_at=utcnow())
            atomic_json(ledger_path, ledger)
            raise RuntimeError("本轮没有日期与事实都核实的内容，未创建空周报")
        previous_work = Path(ledger["worktree"]) if ledger.get("worktree") else None
        if previous_work and previous_work.exists() and git(previous_work, "rev-parse", "HEAD") != ledger.get("base"):
            raise RuntimeError("上次检出含未登记提交，保留现场，需要检查后恢复")
        root = Path(settings["checkout"])
        if git(root, "status", "--porcelain", "--untracked-files=all"):
            raise RuntimeError("博客检出有未提交改动，保留用户现场，停止自动发布")
        origin = git(root, "remote", "get-url", "origin")
        if origin not in ("git@github.com:SaltAdamW/blog.git", "https://github.com/SaltAdamW/blog.git"):
            raise ValueError("博客 origin 不是已授权远端")
        original_head = git(root, "rev-parse", "HEAD")
        git(root, "fetch", "origin", settings["branch"])
        base = git(root, "rev-parse", "origin/" + settings["branch"])
        if original_head != base:
            raise RuntimeError("博客本地与远端不一致，停止发布以免带入无关提交")
        # deliver、retry 和手动 publish 共用同一门禁；不允许命令行路径绕过追问。
        try:
            bundle = prepare_questioning(config, bundle_path)
        except ReviewBlocked as exc:
            ledger.update(status="needs_revision", error=str(exc), checked_at=utcnow())
            atomic_json(ledger_path, ledger)
            raise
        accepted, deferred = validate_bundle(bundle, state, allow_supplement)
        ledger["questioning_review"] = bundle["questioning_review"]
        ledger["deferred"] = deferred
        previous_work = Path(ledger["worktree"]) if ledger.get("worktree") else None
        if previous_work and previous_work.exists() and git(previous_work, "rev-parse", "HEAD") != ledger.get("base"):
            raise RuntimeError("上次检出含未登记提交，保留现场，需要检查后恢复")
        attempt = ledger.get("attempt", 0) + 1
        work = publication_dir / f"worktree-{attempt}"
        if work.exists():
            raise RuntimeError("本次发布检出目录已经存在，停止以免覆盖")
        git(root, "worktree", "add", "--detach", str(work), base)
        if previous_work and previous_work.exists():
            ledger.setdefault("previous_worktrees", []).append(str(previous_work))
        ledger.update(status="preparing", base=base, worktree=str(work), attempt=attempt)
        atomic_json(ledger_path, ledger)
        try:
            merged = merge_issue(work, bundle, accepted)
            if not merged:
                ledger.update(status="already_published", checked_at=utcnow())
                atomic_json(ledger_path, ledger)
                git(root, "worktree", "remove", str(work))
                return ledger
            python = settings.get("python", sys.executable)
            checks = []
            for args in ([python, "tools/fetch-heading-font.py"], [python, "build.py"],
                         [python, "-m", "unittest", "discover", "-s", "tests", "-v"]):
                out, err = command(args, cwd=work, timeout=240)
                checks.append({"command": args, "stdout": out, "stderr": err})
            atomic_json(publication_dir / "checks.json", checks)
            allowed = {merged["source"], merged["source_manifest"], "posts.json", "index.html", "archive/index.html", "about/index.html", "feed.xml",
                       "assets/fonts/heading-serif.woff2", "assets/fonts/OFL.txt"}
            allowed.update(p.relative_to(work).as_posix() for p in (work / "posts").glob("*/index.html"))
            changes = git(work, "status", "--porcelain", "--untracked-files=all").splitlines()
            names = [line[3:] for line in changes]
            if not names or set(names) - allowed:
                raise RuntimeError("发布包含非预期文件：" + repr(set(names) - allowed))
            git(work, "diff", "--check")
            if git(root, "rev-parse", "HEAD") != original_head or git(root, "status", "--porcelain", "--untracked-files=all"):
                raise RuntimeError("验证期间用户检出发生变化，停止提交")
            git(work, "add", "--", *names)
            git(work, "diff", "--cached", "--check")
            atomic_text(publication_dir / "review.diff", git(work, "diff", "--cached"))
            git(work, "commit", "-m", f"更新 {bundle['end']} 研究周报及原文来源")
            commit = git(work, "rev-parse", "HEAD")
            files = {name: digest(safe_path(work, name).read_bytes()) for name in allowed if safe_path(work, name).is_file()}
            for name in ("assets/style.css", "assets/site.js", "assets/analytics.js"):
                if safe_path(work, name).is_file():
                    files[name] = digest(safe_path(work, name).read_bytes())
            # 所有周报原稿与来源也纳入回读，避免只验证 HTML。
            for pattern in ("agent-research-weekly-*.md", "agent-research-weekly-*.sources.json"):
                for file in work.glob(pattern):
                    files[file.name] = digest(file.read_bytes())
            ledger.update(status="committed", commit=commit, files=files, added=merged["added"], finding_ids=merged["finding_ids"])
            atomic_json(ledger_path, ledger)
            if git(root, "ls-remote", "origin", "refs/heads/" + settings["branch"]).split()[0] != base:
                raise RuntimeError("远端在发布期间前进，停止推送，禁止强推")
            if git(root, "rev-parse", "HEAD") != original_head or git(root, "status", "--porcelain", "--untracked-files=all"):
                raise RuntimeError("推送前用户检出发生变化，停止发布")
            if git(work, "rev-parse", "HEAD") != commit or git(work, "status", "--porcelain", "--untracked-files=all"):
                raise RuntimeError("推送前发布检出发生变化，停止发布")
            git(work, "push", "origin", "HEAD:refs/heads/" + settings["branch"])
            ledger.update(status="pushed", pushed_at=utcnow())
            atomic_json(ledger_path, ledger)
            # 快进干净的用户检出，不 reset，不丢弃任何改动。
            if git(root, "rev-parse", "HEAD") == original_head and not git(root, "status", "--porcelain", "--untracked-files=all"):
                git(root, "merge", "--ff-only", commit)
            ledger.update(online_check(settings, commit, files))
            ledger["verified_at"] = utcnow()
            _finish_publication_history(config, bundle, ledger, ledger_path)
            return ledger
        except Exception as exc:
            ledger["error"] = str(exc)
            atomic_json(ledger_path, ledger)
            raise
