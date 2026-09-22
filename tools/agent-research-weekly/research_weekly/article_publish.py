"""已确认长文的显式发布；不会从 resume 或定时器触发。"""

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import atomic_json, atomic_text, command, digest, run_lock, utcnow
from .publish import git, online_check, safe_path


def require_approved(article, sha256):
    state = article.state
    if state["phase"] not in ("ready_to_publish", "published") or not state["draft_approval"]:
        raise ValueError("成稿尚未由用户确认，不能导出发布包或发布")
    if state["draft_approval"]["sha256"] != sha256 or state["draft"]["sha256"] != sha256:
        raise ValueError("发布确认与成稿版本不一致")
    article.validate_draft()


def export(article, sha256):
    with run_lock(article.root):
        article.load()
        require_approved(article, sha256)
        payload = article.delivery()
        directory = article.root / "delivery" / sha256
        files = {"article.md": payload["body"], "sources.json": json.dumps(payload["manifest"], ensure_ascii=False, indent=2) + "\n"}
        for name, body in files.items():
            path = directory / name
            if path.exists() and path.read_text() != body:
                raise ValueError("发布包已被修改，保留文件，不覆盖")
            atomic_text(path, body)
        return {"directory": str(directory), "files": {name: digest(body) for name, body in files.items()}, "published": False}


def add_post(work, article, payload):
    identifier = article.state["id"]
    posts = json.loads((work / "posts.json").read_text())
    source = f"longform-{identifier}.md"
    manifest = f"longform-{identifier}.sources.json"
    if any(post["slug"] == identifier for post in posts) or (work / source).exists() or (work / manifest).exists():
        raise ValueError("博客已有同名文章或文件，不覆盖既有内容")
    atomic_text(work / source, payload["body"])
    atomic_json(work / manifest, payload["manifest"])
    posts.append({"slug": identifier, "title": payload["title"],
                  "date": dt.datetime.now(ZoneInfo(article.config["timezone"])).date().isoformat(),
                  "category": "技术长文", "tags": [], "description": payload["description"],
                  "deck": payload["description"], "word_count": len(payload["body"]),
                  "source": source, "source_manifest": manifest})
    atomic_json(work / "posts.json", posts)
    return {source, manifest, "posts.json", "index.html", "archive/index.html", "about/index.html", "feed.xml",
            "assets/fonts/heading-serif.woff2", "assets/fonts/OFL.txt", f"posts/{identifier}/index.html"}


def publish(article, sha256, confirm=False):
    if not confirm:
        raise ValueError("发布需要显式 --confirm，不会随 resume 自动执行")
    settings = article.config["publication"]
    if (not settings.get("enabled") or settings.get("repository") != "SaltAdamW/blog" or settings.get("branch") != "main"
            or settings.get("site_url") != "https://saltadamw.github.io/blog/"):
        raise ValueError("发布目标不是已授权博客")
    with run_lock(article.config["state_dir"]), run_lock(article.root):
        article.load()
        require_approved(article, sha256)
        payload = article.delivery()
        directory = article.root / "publication" / sha256
        ledger_path = directory / "status.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"status": "pending", "draft_hash": sha256}
        root = Path(settings["checkout"])
        if ledger["draft_hash"] != sha256:
            raise ValueError("发布记录绑定的稿件已变化")
        if ledger["status"] == "verified":
            article.state["phase"] = "published"
            article.save("publication-verified")
            return ledger
        try:
            if git(root, "remote", "get-url", "origin") not in ("git@github.com:SaltAdamW/blog.git", "https://github.com/SaltAdamW/blog.git"):
                raise ValueError("博客 origin 不是已授权仓库")
            if not ledger.get("commit"):
                if git(root, "status", "--porcelain", "--untracked-files=all"):
                    raise RuntimeError("博客有未提交改动，保留现场，停止发布")
                base = git(root, "rev-parse", "HEAD")
                git(root, "fetch", "origin", "main")
                if git(root, "rev-parse", "origin/main") != base:
                    raise RuntimeError("博客本地与远端不一致")
                if ledger.get("worktree") and Path(ledger["worktree"]).exists():
                    if git(Path(ledger["worktree"]), "rev-parse", "HEAD") != ledger["base"]:
                        raise RuntimeError("旧发布检出含未登记提交，需要人工核对，不能重建后推送")
                attempt = ledger.get("attempt", 0) + 1
                work = directory / f"worktree-{attempt}"
                if work.exists():
                    raise RuntimeError("发布检出已存在，不覆盖")
                git(root, "worktree", "add", "--detach", str(work), base)
                ledger.update(status="preparing", base=base, worktree=str(work), attempt=attempt)
                atomic_json(ledger_path, ledger)
                allowed = add_post(work, article, payload)
                python = settings.get("python", "python3")
                checks = []
                for args in ([python, "tools/fetch-heading-font.py"], [python, "build.py"],
                             [python, "-m", "unittest", "discover", "-s", "tests", "-v"],
                             ["node", "--test", "tests/test_analytics.cjs"]):
                    out, err = command(args, cwd=work, timeout=240)
                    checks.append({"command": args, "stdout": out, "stderr": err})
                atomic_json(directory / "checks.json", checks)
                # 字体子集变化会更新其他文章的资源版本，允许重建这些 HTML，但不允许改旧原稿。
                allowed.update(p.relative_to(work).as_posix() for p in (work / "posts").glob("*/index.html"))
                names = [line[3:] for line in git(work, "status", "--porcelain", "--untracked-files=all").splitlines()]
                if not names or set(names) - allowed:
                    raise RuntimeError("出现超出长文发布范围的文件")
                git(work, "diff", "--check")
                if git(root, "rev-parse", "HEAD") != base or git(root, "status", "--porcelain", "--untracked-files=all"):
                    raise RuntimeError("构建期间用户检出发生变化，停止提交")
                git(work, "add", "--", *names)
                atomic_text(directory / "review.diff", git(work, "diff", "--cached"))
                git(work, "commit", "-m", "发布技术长文：" + article.state["id"])
                ledger.update(status="committed", commit=git(work, "rev-parse", "HEAD"),
                              files={name: digest(safe_path(work, name).read_bytes()) for name in allowed if safe_path(work, name).is_file()})
                atomic_json(ledger_path, ledger)
            remote = git(root, "ls-remote", "origin", "refs/heads/main").split()[0]
            if remote not in (ledger["base"], ledger["commit"]):
                raise RuntimeError("远端已经前进，不强推，也不把其他版本算作本次部署")
            if remote == ledger["base"]:
                work = Path(ledger["worktree"])
                if (git(root, "rev-parse", "HEAD") != ledger["base"] or git(root, "status", "--porcelain", "--untracked-files=all")
                        or git(work, "rev-parse", "HEAD") != ledger["commit"] or git(work, "status", "--porcelain", "--untracked-files=all")):
                    raise RuntimeError("推送前检出发生变化")
                git(work, "push", "origin", "HEAD:refs/heads/main")
            ledger.update(status="pushed")
            atomic_json(ledger_path, ledger)
            if git(root, "rev-parse", "HEAD") == ledger["base"] and not git(root, "status", "--porcelain", "--untracked-files=all"):
                git(root, "fetch", "origin", "main")
                git(root, "merge", "--ff-only", ledger["commit"])
            ledger.update(online_check(settings, ledger["commit"], ledger["files"]), status="verified", verified_at=utcnow())
            ledger.pop("error", None)
            atomic_json(ledger_path, ledger)
            article.state["phase"] = "published"
            article.save("publication-verified")
            work = Path(ledger["worktree"])
            if work.exists() and not git(work, "status", "--porcelain", "--untracked-files=all"):
                git(root, "worktree", "remove", str(work))
            return ledger
        except Exception as exc:
            ledger["error"] = str(exc)
            atomic_json(ledger_path, ledger)
            raise
