"""用已核读的公开原文试跑真实追问，不写博客、不登记已发表历史。"""

import argparse
import datetime as dt
import json
from pathlib import Path

from research_weekly.core import ROOT, atomic_json, load_config, run_lock
from research_weekly.publish import validate_bundle
from research_weekly.questioning import prepare


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle")
    parser.add_argument("--finding-id", required=True)
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    args = parser.parse_args()
    config = load_config(args.config)
    original = json.loads(Path(args.bundle).read_text())
    selected = [f for f in original["findings"] if f["id"] == args.finding_id]
    if len(selected) != 1:
        raise ValueError("必须指定一条实际存在的已核读发现")
    identifier = "questioning-smoke-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    sample = {**original, "run_id": identifier, "scope": "targeted", "findings": selected}
    sample.pop("questioning_review", None)
    path = Path(config["state_dir"]) / "runs" / identifier / "bundle.json"
    validate_bundle(sample, Path(config["state_dir"]), allow_supplement=True)
    atomic_json(path, sample)
    print(json.dumps({"status": "started", "bundle": str(path), "test_only": True}), flush=True)
    with run_lock(Path(config["state_dir"]) / "publish-lock"):
        reviewed = prepare(config, path)
        validate_bundle(reviewed, Path(config["state_dir"]), allow_supplement=True)
    print(json.dumps({"status": "reviewed_not_published", "bundle": str(path),
                      "review": reviewed["questioning_review"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
