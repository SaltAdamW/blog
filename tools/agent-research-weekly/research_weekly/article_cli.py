"""长文独立命令行，不进入周报队列和定时任务。"""

import argparse
import json
import os
from pathlib import Path
import time
import uuid

from .article import Article, checked_slug
from .article_editor import ArticleEditor, TOPICS, rules_snapshot
from .article_publish import export, publish
from .codex import validate
from .core import ROOT, atomic_json, load_config
from .publish import safe_path


def parser():
    cli = argparse.ArgumentParser(prog="article-agent", description="可续跑的技术长文：研究、框架确认、分节写作、审稿、显式发布")
    cli.add_argument("--config", default=str(ROOT / "config.json"))
    sub = cli.add_subparsers(dest="action", required=True)
    create = sub.add_parser("start", help="登记主题与材料，不自动写作")
    create.add_argument("id")
    create.add_argument("--topic", required=True)
    create.add_argument("--source", action="append", default=[])
    create.add_argument("--audience")
    create.add_argument("--skill")
    create.add_argument("--rounds", type=int, default=3)
    create.add_argument("--max-sources", type=int, default=8)
    create.add_argument("--repair-passes", type=int, default=2)
    create.add_argument("--no-search", action="store_true")
    for name in ("status", "resume"):
        sub.add_parser(name).add_argument("id")
    sub.add_parser("list", help="列出已有长文任务")
    for name in ("approve-outline", "approve-draft"):
        approve = sub.add_parser(name, help="仅在用户明确确认当前版本后调用")
        approve.add_argument("id")
        approve.add_argument("--hash", required=True)
        approve.add_argument("--note", required=True, help="用户的确认指令，不能使用模型自评代替")
    revise = sub.add_parser("revise", help="不带 section 修订框架；带 section 只返修该节")
    revise.add_argument("id")
    revise.add_argument("--feedback", required=True)
    revise.add_argument("--section")
    source = sub.add_parser("add-source", help="加入或重试原文，撤销旧成稿确认")
    source.add_argument("id")
    source.add_argument("url")
    research = sub.add_parser("research", help="登记定向补查和预算，随后 resume 执行")
    research.add_argument("id")
    research.add_argument("--question", required=True)
    research.add_argument("--rounds", type=int, default=1, help="追加0至5轮，累计最多10轮")
    research.add_argument("--max-sources", type=int, help="本篇原文总预算，只可增加至20篇")
    for name in ("export", "publish"):
        deliver = sub.add_parser(name)
        deliver.add_argument("id")
        deliver.add_argument("--hash", required=True)
        if name == "publish":
            deliver.add_argument("--confirm", action="store_true")
    topics = sub.add_parser("topics", help="从已有周报提出选题供用户选择，不创建文章")
    topics.add_argument("--issue", required=True, help="博客 posts.json 中的周报 slug")
    return cli


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.action == "list":
            result = [Article(config, p.parent.name).status() for p in sorted((Path(config["state_dir"]) / "articles").glob("*/state.json"))]
        elif args.action == "topics":
            blog = Path(config["publication"]["checkout"])
            posts = json.loads((blog / "posts.json").read_text())
            post = next((p for p in posts if p["slug"] == args.issue and p["category"] == "研究周报"), None)
            if not post:
                raise ValueError("未找到指定周报")
            manifest = json.loads(safe_path(blog, post["source_manifest"]).read_text())
            urls = [s["url"] for s in manifest["sources"]]
            directory = Path(config["state_dir"]) / "article-topics" / uuid.uuid4().hex[:12]
            editor = ArticleEditor(config, directory, time.monotonic() + config["limits"]["run_seconds"],
                                   rules_snapshot(Path.home() / ".codex/skills/deep-tech-writing"))
            result = editor.suggest(safe_path(blog, post["source"]).read_text(), urls)
            validate(result, TOPICS)
            if any(not set(p["source_urls"]) <= set(urls) for p in result["proposals"]):
                raise ValueError("选题引用不在周报原文清单内")
            atomic_json(directory / "proposals.json", result)
            result["path"] = str(directory / "proposals.json")
        else:
            article = Article(config, checked_slug(args.id))
            if args.action == "start":
                result = article.create(args.topic, urls=args.source, audience=args.audience, skill=args.skill,
                                        rounds=args.rounds, max_sources=args.max_sources, repair_passes=args.repair_passes, search=not args.no_search)
            elif args.action == "resume":
                result = article.resume()
            elif args.action == "status":
                result = article.status()
            elif args.action.startswith("approve-"):
                result = article.approve(args.action.removeprefix("approve-"), args.hash, args.note)
            elif args.action == "revise":
                result = article.revise(args.feedback, args.section)
            elif args.action == "add-source":
                result = article.add_source(args.url)
            elif args.action == "research":
                result = article.extend_research(args.question, args.rounds, args.max_sources)
            elif args.action == "export":
                result = export(article, args.hash)
            elif args.action == "publish":
                result = publish(article, args.hash, args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        raise SystemExit(1) from None
