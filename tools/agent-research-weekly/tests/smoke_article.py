"""显式运行的真实模型测试，只验证研究到框架门禁，不批准或发布测试文章。"""

import argparse
import json
from pathlib import Path
import uuid

from research_weekly.article import Article
from research_weekly.core import ROOT, atomic_json, load_config


def main():
    parser = argparse.ArgumentParser(description="会读取公开原文并消耗模型额度；状态存入隔离目录")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--source", required=True, action="append")
    parser.add_argument("--topic", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    directory = Path(config["state_dir"]) / "article-smoke" / uuid.uuid4().hex[:12]
    config["state_dir"] = str(directory)
    config["publication"]["enabled"] = False
    config["codex"]["max_calls"] = 12
    config["limits"]["run_seconds"] = 1200
    atomic_json(directory / "config.json", config)
    article = Article(config, "smoke-not-an-approved-topic")
    article.create("测试任务，非正式选题：" + args.topic, urls=args.source, search=False, max_sources=min(4, len(args.source)))
    result = article.resume()
    article.load()
    if article.state["sections"] or article.state["outline_approval"] or article.state["draft_approval"]:
        raise AssertionError("测试跨过了人工确认门禁")
    result["test_only"] = True
    result["config"] = str(directory / "config.json")
    atomic_json(directory / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["phase"] != "awaiting_outline_approval":
        raise SystemExit("已保留测试材料，但框架尚未通过审查；不能报告完整 smoke 通过")


if __name__ == "__main__":
    main()
