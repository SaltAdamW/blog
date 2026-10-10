"""发现并排序适合技术长文的原创博客站点。"""

import re
from urllib.parse import urlsplit


LOW_SIGNAL_HOSTS = {
    "csdn.net", "51cto.com", "juejin.cn", "zhihu.com", "jianshu.com",
    "toutiao.com", "sohu.com", "qq.com", "163.com", "baijiahao.baidu.com",
}

QUALITY_TERMS = (
    "benchmark", "实测", "复现", "源码", "source", "implementation", "profil",
    "field report", "production", "失败", "限制", "limitations", "tradeoff",
)


def hostname(url):
    host = urlsplit(url).hostname or ""
    return host.lower().removeprefix("www.")


def root_url(url):
    host = hostname(url)
    return f"https://{host}/" if host else ""


def domain_from_item(item):
    return hostname(item.get("url", ""))


def score_site(records, low_signal_hosts=None):
    """Return a bounded score with explainable, search-result-only signals.

    This ranks discovery leads; it never turns a score into evidence.
    """
    records = list(records)
    if not records:
        return {"score": 0, "signals": [], "unknowns": ["没有站点材料"]}
    host = domain_from_item(records[0])
    text = " ".join(str(r.get(k, "")) for r in records for k in ("title", "focus", "snippet", "text")).lower()
    signals = []
    if host and host not in LOW_SIGNAL_HOSTS:
        signals.append("非聚合平台域名")
    if len({r.get("url") for r in records if r.get("url")}) >= 2:
        signals.append("同站点有多篇候选")
    if any(term in text for term in QUALITY_TERMS):
        signals.append("摘要出现实现、实测或边界信号")
    if any(re.search(pattern, text) for pattern in (r"\b\d{3,5}\s*(?:字|words?)", r"\b(?:ms|gb/s|tok/s|tflops)\b", r"\bbenchmark\b")):
        signals.append("摘要含测量规模或性能指标")
    score = min(100, len(signals) * 20)
    low_signal_hosts = set(low_signal_hosts or LOW_SIGNAL_HOSTS)
    if any(host == low or host.endswith("." + low) for low in low_signal_hosts):
        score = min(score, 20)
        signals.append("聚合平台降权")
    return {"score": score, "signals": signals,
            "unknowns": ["作者身份、长期更新和原文完整性仍需抓取原站核对"]}


def rank_sites(records, limit=8, low_signal_hosts=None):
    grouped = {}
    for item in records:
        host = domain_from_item(item)
        if host:
            grouped.setdefault(host, []).append(item)
    ranked = []
    for host, items in grouped.items():
        quality = score_site(items, low_signal_hosts)
        ranked.append({"host": host, "url": root_url(items[0]["url"]),
                       "score": quality["score"], "signals": quality["signals"],
                       "unknowns": quality["unknowns"],
                       "candidate_count": len(items),
                       "examples": [{k: x.get(k) for k in ("title", "url", "published_at")}
                                    for x in items[:3]]})
    return sorted(ranked, key=lambda x: (-x["score"], -x["candidate_count"], x["host"]))[:limit]


def site_queries(topic, sites, as_of, limit=4):
    """Build focused queries only after open discovery has found candidate sites."""
    queries = []
    for site in sites[:limit]:
        queries.append((f"site:{site['host']} {topic} implementation benchmark limitations as of {as_of}", site["host"]))
    return queries
