import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

from .codex import Codex, obj
from .core import ROOT, Store, atomic_json, atomic_text, command, load_config, run_lock
from .fetch import FetchError, fetch_evidence
from .pipeline import due, run
from .publish import publish
from .questioning import prepare as prepare_questioning, review_seconds


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def systemd_quote(value):
    if any(c in value for c in "\n\r\0"):
        raise ValueError("systemd 参数不允许换行或 NUL")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def schedule_files(config):
    python = str(Path(sys.executable).resolve())
    entry = [python, "-m", "research_weekly", "--config", config["_path"]]
    path = os.pathsep.join((str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"))
    units = {}
    for name, action, calendar in (
        ("agent-research-weekly", "deliver", config["schedule"]),
        ("agent-research-weekly-retry", "retry", f"*-*-* 21:30:00 {config['timezone']}"),
    ):
        if any(c in calendar for c in "\n\r\0%"):
            raise ValueError("非法定时表达式")
        units[name + ".service"] = "\n".join([
            "[Unit]", "Description=Evidence-first Agent research weekly", "After=network-online.target", "",
            "[Service]", "Type=oneshot", "UMask=0077", f"WorkingDirectory={str(ROOT).replace('%', '%%')}",
            f"Environment={systemd_quote('PATH=' + path)}", "Environment=PYTHONUNBUFFERED=1",
            "ExecStart=" + " ".join(systemd_quote(v) for v in [*entry, action]),
            f"TimeoutStartSec={config['limits']['run_seconds'] + review_seconds(config) + config.get('publication', {}).get('deploy_timeout_seconds', 900) + 1800}",
            "KillMode=control-group", ""])
        units[name + ".timer"] = "\n".join([
            "[Unit]", "Description=Agent research weekly schedule", "", "[Timer]",
            f"OnCalendar={calendar}", "Persistent=true", "RandomizedDelaySec=60", "",
            "[Install]", "WantedBy=timers.target", ""])
    return units


def parser():
    p = argparse.ArgumentParser(prog="agent-weekly", description="以 Codex 为编辑的 Agent 研究周报")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="建立状态库并加入三份质量参照材料")
    add = sub.add_parser("add", help="加入候选；Codex/DSH/人工采集都用同一入口")
    add.add_argument("url")
    add.add_argument("--title", default="")
    add.add_argument("--origin", default="manual")
    add.add_argument("--published-at")
    add.add_argument("--focus", default="")
    add.add_argument("--alternative", action="append", default=[])
    imp = sub.add_parser("import", help="导入 JSON 数组或 JSONL 候选")
    imp.add_argument("file")
    status = sub.add_parser("status", help="显示最近运行、发布和定时器状态")
    status.add_argument("--full", action="store_true", help="同时列出完整候选队列")
    fetch = sub.add_parser("fetch", help="仅下载和解析，不调用模型")
    fetch.add_argument("id")
    execute = sub.add_parser("run", help="发现、补证、审稿并生成本周报告")
    execute.add_argument("--no-discover", action="store_true")
    execute.add_argument("--force", action="store_true", help="同周追加一次运行，已报道发现仍去重")
    execute.add_argument("--limit", type=int)
    execute.add_argument("--only", action="append", help="只处理指定候选 ID，忽略重试冷却")
    execute.add_argument("--start", help="窗口开始日期，YYYY-MM-DD")
    execute.add_argument("--end", help="窗口结束日期，必须早于今天")
    delivery = sub.add_parser("deliver", help="生成已结束窗口的周报，推送并等待 Pages 与线上回读")
    delivery.add_argument("--start")
    delivery.add_argument("--end")
    delivery.add_argument("--limit", type=int)
    pub = sub.add_parser("publish", help="发布已经核读的本地稿，失败时复用同一提交恢复")
    pub.add_argument("bundle")
    pub.add_argument("--allow-supplement", action="store_true", help="显式允许定向补充已有一期，不作为完整周报验收")
    review = sub.add_parser("review", help="对已有周报逐轮追问并保留断点，不发布、不修改原稿")
    review.add_argument("bundle")
    sub.add_parser("retry", help="恢复未完成发布并处理到期的待补证候选，无候选则不调用模型")
    rejudge = sub.add_parser("rejudge", help="清除指定候选的审稿缓存，下一轮重新判定")
    rejudge.add_argument("id")
    feedback = sub.add_parser("feedback", help="记录对某项已报道发现的阅读反馈")
    feedback.add_argument("finding_id")
    feedback.add_argument("verdict", choices=["useful", "not_useful", "correction"])
    feedback.add_argument("note")
    doctor = sub.add_parser("doctor", help="检查依赖，不输出凭据")
    doctor.add_argument("--smoke", action="store_true", help="真实调用 Codex 验证认证和结构化输出，会消耗额度")
    sched = sub.add_parser("schedule", help="生成 systemd 用户定时器，默认不启用")
    sched.add_argument("--install", action="store_true", help="安装并启用每周运行和每日待补证重试，会周期性调用模型")
    return p


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.action == "run":
            if args.limit is not None and args.limit <= 0:
                raise ValueError("limit 必须大于零")
            print_json(run(config, discover=not args.no_discover, force=args.force, limit=args.limit, only=args.only,
                           start=args.start, end=args.end))
            return
        if args.action == "publish":
            print_json(publish(config, args.bundle, allow_supplement=args.allow_supplement))
            return
        if args.action == "review":
            from .publish import validate_bundle
            with run_lock(Path(config["state_dir"]) / "publish-lock"):
                bundle = json.loads(Path(args.bundle).read_text())
                validate_bundle(bundle, Path(config["state_dir"]), allow_supplement=True)
                reviewed = prepare_questioning(config, args.bundle)
                validate_bundle(reviewed, Path(config["state_dir"]), allow_supplement=True)
                print_json({"status": "reviewed_not_published", "review": reviewed["questioning_review"]})
            return
        if args.action == "deliver":
            if args.limit is not None and args.limit <= 0:
                raise ValueError("limit 必须大于零")
            # 先恢复已推送却未完成回读的发布，不用新一轮模型调用掩盖发布失败。
            for path in sorted((Path(config["state_dir"]) / "publications").glob("*/status.json")):
                ledger = json.loads(path.read_text())
                if ledger.get("status") not in ("verified", "already_published", "no_publishable_content", "needs_revision"):
                    print_json(publish(config, ledger["bundle"], allow_supplement=ledger.get("allow_supplement", False)))
            outcome = run(config, start=args.start, end=args.end, limit=args.limit, record_reported=False)
            print_json(publish(config, outcome["bundle"]))
            return
        if args.action == "doctor":
            found = {name: shutil.which(name) for name in (config["codex"]["binary"], "pdftotext", "pdftoppm", "tesseract", "systemctl")}
            result = {"python": sys.version.split()[0], "binaries": found, "model": config["codex"].get("model") or "继承本机 Codex 配置", "real_model_verified": False}
            skill = Path.home() / ".codex/skills/deep-tech-writing"
            skill_files = ("SKILL.md", "references/style-reference.md", "references/draft.md", "references/review.md")
            missing = [name for name in skill_files if not (skill / name).is_file()]
            result["optional_dependencies"] = {"article": {
                "required_for_weekly": False,
                "markdown_it_available": importlib.util.find_spec("markdown_it") is not None,
                "skill_path": str(skill), "skill_available": not missing, "missing_skill_files": missing,
            }}
            if args.smoke:
                editor = Codex(config, Path(config["state_dir"]) / "doctor" / dt.datetime.now().strftime("%Y%m%dT%H%M%S"))
                reply = editor.ask("smoke", "不要调用工具，只输出符合 schema 的 JSON，ok 为 true。", obj({"ok": {"type": "boolean"}}))
                result["real_model_verified"] = reply["ok"] is True
                result["usage"] = editor.usage
            print_json(result)
            return
        if args.action == "schedule":
            units = schedule_files(config)
            target = Path.home()/".config/systemd/user" if args.install else Path(config["state_dir"])/"schedule"
            target.mkdir(parents=True, exist_ok=True)
            for name, body in units.items():
                destination = target / name
                if args.install and destination.exists() and destination.read_text() != body:
                    raise ValueError(f"已有不同的定时器文件，不覆盖：{destination}")
            for name, body in units.items():
                atomic_text(target / name, body)
            if args.install:
                command(["systemctl", "--user", "daemon-reload"])
                command(["systemctl", "--user", "enable", "--now", "agent-research-weekly.timer", "agent-research-weekly-retry.timer"])
            print_json({"installed": args.install, "directory": str(target), "weekly": config["schedule"], "retry": "每日21:30，恢复未完成发布并重试到期的待补证候选"})
            return
        store = Store(config["state_dir"])
        try:
            if args.action == "retry":
                for path in sorted((Path(config["state_dir"]) / "publications").glob("*/status.json")):
                    ledger = json.loads(path.read_text())
                    if ledger.get("status") not in ("verified", "already_published", "no_publishable_content", "needs_revision"):
                        print_json(publish(config, ledger["bundle"], allow_supplement=ledger.get("allow_supplement", False)))
                now = dt.datetime.now(dt.timezone.utc)
                ids = [c["id"] for c in store.list() if c["status"] == "needs_evidence" and due(c, config, now)]
                print_json(run(config, discover=False, force=True, only=ids, record_reported=False) if ids else {"status": "no_retry_due"})
                return
            if args.action == "status":
                from collections import Counter
                publications = []
                for path in sorted((Path(config["state_dir"]) / "publications").glob("*/status.json")):
                    ledger = json.loads(path.read_text())
                    publications.append({k: ledger.get(k) for k in ("status", "commit", "pages_url", "verified_at", "error")})
                result = {"candidate_counts": dict(Counter(c["status"] for c in store.list())),
                          "runs": [dict(r) for r in store.db.execute("SELECT id,status,report,started_at FROM runs ORDER BY started_at DESC LIMIT 10")],
                          "publications": publications[-10:]}
                try:
                    result["timers"] = command(["systemctl", "--user", "show", "agent-research-weekly.timer", "agent-research-weekly-retry.timer",
                                                 "--property=Id,LoadState,ActiveState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime"])[0]
                except RuntimeError as exc:
                    result["timers"] = str(exc)
                if args.full:
                    result["candidates"] = [{k: c[k] for k in ("id", "title", "origins", "status", "reason", "retry_at")} for c in store.list()]
                print_json(result)
                return
            with run_lock(config["state_dir"]):
                if args.action == "init":
                    print_json({"state_dir": config["state_dir"], "anchors": [store.add(a) for a in config["anchors"]]})
                elif args.action == "add":
                    print_json({"id": store.add({"url": args.url, "title": args.title, "origin": args.origin,
                                                "published_at": args.published_at, "focus": args.focus, "alternatives": args.alternative})})
                elif args.action == "import":
                    content = Path(args.file).read_text()
                    items = json.loads(content) if content.lstrip().startswith("[") else [json.loads(line) for line in content.splitlines() if line.strip()]
                    print_json({"ids": [store.add(item) for item in items]})
                elif args.action == "fetch":
                    item = store.get(args.id)
                    if not item:
                        raise ValueError("候选 ID 不存在")
                    from .core import utcnow
                    try:
                        evidence = fetch_evidence(item, config, store.root)
                    except FetchError as exc:
                        store.update(args.id, status="needs_evidence", reason=str(exc), failures=item["failures"]+1,
                                     retry_at=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=config["fetch"]["retry_hours"])).isoformat())
                        atomic_json(store.root / "manual-fetch" / f"{args.id}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}.json", {"error": str(exc), "attempts": exc.attempts})
                        raise
                    store.update(args.id, evidence_hash=evidence["sha256"], fetched_at=utcnow(), status="ready", retry_at=None)
                    print_json({"sha256": evidence["sha256"], "blocks": len(evidence["blocks"]), "raw_path": evidence["raw_path"], "attempts": evidence["attempts"]})
                elif args.action == "rejudge":
                    if not store.get(args.id):
                        raise ValueError("候选 ID 不存在")
                    store.update(args.id, reviewed_hash=None, review=None, status="ready", retry_at=None)
                    print_json({"id": args.id, "status": "ready"})
                elif args.action == "feedback":
                    if not store.db.execute("SELECT 1 FROM reported WHERE finding_id=?", (args.finding_id,)).fetchone():
                        raise ValueError("只能反馈已报道的发现 ID")
                    from .core import utcnow
                    with store.db:
                        store.db.execute("INSERT INTO feedback(finding_id,verdict,note,created_at) VALUES(?,?,?,?)", (args.finding_id, args.verdict, args.note, utcnow()))
                    print_json({"recorded": True})
        finally:
            store.close()
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print_json({"error": f"{type(exc).__name__}: {exc}"})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
