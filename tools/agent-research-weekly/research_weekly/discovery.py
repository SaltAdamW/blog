"""按轮保存实际查询、候选和去向，不把模型声称搜索过当成查询记录。"""

import json
import time

from .core import atomic_json, command


def external_search(config, start, end, index, context, run_dir, deadline, *, historical=False):
    """宿主只执行固定的只读搜索入口，模型不能指定命令或 MCP 服务。"""
    if not config.get("discovery", {}).get("external_search", True):
        return []
    topics = config["topics"]
    period = f"as of {end}" if historical else f"{start} to {end}"
    broad = f"{topics[index % len(topics)]} {period}"
    pending = context.get("followups", [])
    followup = pending[0]["query"] if pending else f"{topics[(index + 1) % len(topics)]} {period}"
    scope = f"available by {end}, including foundational older material without a recent-date restriction" if historical else f"published between {start} and {end}"
    searches = [(query, "research" if historical else "current", scope)
                for query in dict.fromkeys((broad, followup))]
    experience_topics = config.get("discovery", {}).get("experience_topics", [])
    if experience_topics and not historical:
        searches.append((f"{experience_topics[index % len(experience_topics)]} before {end}", "experience",
                         f"available by {end}, without a lower date bound. Find firsthand accounts of actual agent tasks, "
                         "attempts, observed failures or successes and tradeoffs; not generic tutorials or launch claims"))
    records = []
    for number, (query, track, search_scope) in enumerate(searches, 1):
        record = {"backend": "exa", "track": track, "query": query, "status": "failed", "text": ""}
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                raise TimeoutError("总时间预算耗尽，未执行外部搜索")
            stdout, stderr = command([
                "mcporter", "call", "exa.web_search_exa", "--args",
                json.dumps({"query": query, "numResults": 8,
                            "objective": f"Find original releases, source repositories and firsthand implementation or experiment writeups {search_scope}. Prefer originals over mirrors, news summaries and aggregators; exclude medical and biomedical applications. Return source dates and concise evidence or limitations, not promotional claims."}),
                "--output", "json", "--no-oauth", "--timeout", "30000"
            ], timeout=min(remaining, 35), output_limit=512000)
            response = json.loads(stdout)
            record["response"] = response
            if response.get("isError"):
                raise ValueError("Exa 返回工具错误")
            parts = [block["text"] for block in response.get("content", []) if block.get("type") == "text"]
            text = "\n".join(parts)
            if not text.strip():
                raise ValueError("Exa 返回空内容，不能视为检索成功")
            record.update(status="ok", text=text[:24000], truncated=len(text) > 24000)
        except Exception as exc:
            record["error"] = str(exc)
        atomic_json(run_dir / f"external-{index + 1}-{number}.json", record)
        records.append({k: v for k, v in record.items() if k != "response"})
    return records


def scan(editor, config, start, end, run_dir, known, ingest, detail):
    settings = config.get("discovery", {})
    minimum = settings.get("min_rounds", 3)
    maximum = settings.get("max_rounds", 5)
    stagnant = 0
    context = {"items": list(known), "rounds": [], "followups": []}
    detail["discovery_rounds"] = []
    for index in range(maximum):
        record = {"source": f"open-search-{index + 1}", "kind": "search", "group": "开放检索",
                  "status": "failed", "collected": 0, "eligible": 0, "added": 0,
                  "queries": [], "items": [], "notes": []}
        detail["source_results"].append(record)
        try:
            result = editor.discover(start, end, index, context)
            record.update({key: result.get(key, []) for key in ("queries", "items", "notes", "followups", "external_searches")})
            before = detail["discovered"]
            ingest(result["items"], record)
            record["new_in_run"] = detail["discovered"] - before
            stagnant = stagnant + 1 if not record["new_in_run"] and result["search_complete"] else 0
            record["status"] = "ok" if result["search_complete"] else "partial"
            decisions = {d["url"]: d["disposition"] for d in detail.get("candidate_decisions", [])}
            for item in result["items"]:
                context["items"].append({**{k: item.get(k) for k in ("title", "url", "focus", "published_at", "track")},
                                         "disposition": decisions.get(item["url"], "queued")})
            context["items"] = list({item["url"]: item for item in context["items"]}.values())
            # 只标记实际执行过的查询；执行过不等于找到了证据，缺口仍由 notes 传递。
            executed = {" ".join(q.lower().split()) for q in record["queries"]}
            pending = [task for task in context["followups"] if " ".join(task["query"].lower().split()) not in executed]
            context["followups"] = list({task["query"]: task for task in pending + record["followups"]}.values())
            if not result["search_complete"]:
                detail["source_errors"].append({"source": record["source"], "error": "; ".join(record["notes"])})
        except Exception as exc:
            stagnant = 0
            record["notes"] = [str(exc)]
            detail["source_errors"].append({"source": record["source"], "error": str(exc)})
        context["rounds"].append({key: record.get(key, []) for key in ("source", "status", "queries", "notes")})
        detail["discovery_followups"] = list(context["followups"])
        detail["discovery_rounds"].append(record)
        atomic_json(run_dir / f"discovery-round-{index + 1}.json", record)
        atomic_json(run_dir / "discovery.json", detail)
        print(f"[search {index + 1}/{maximum}] {record['status']}: {len(record['queries'])} 查询，{record['collected']} 候选", flush=True)
        if index + 1 >= minimum and stagnant >= settings.get("stagnant_rounds", 2) and not context["followups"]:
            detail["discovery_stop"] = "连续补扫无新增候选"
            break
    else:
        detail["discovery_stop"] = "达到轮数预算，未声称穷尽"
    if context["followups"]:
        detail["source_errors"].append({"source": "discovery-followups",
                                        "error": f"仍有 {len(context['followups'])} 条追查未执行，详见 discovery_followups"})
    atomic_json(run_dir / "discovery.json", detail)
