"""选题范围和公开正文门禁；关键词用于兜底，正文语义判断仍由审稿完成。"""

import datetime as dt
import json
import re


EXCLUDED = re.compile(
    r"医学|医疗|病理|基因|生物医学|临床|患者|癌症|蛋白质|"
    r"\b(?:medical|medicine|clinical|pathology|biomedical|genomic[s]?|genome|healthcare|cancer)\b", re.I)

EXPERIENCE_ORIGIN = "experience-search"


def is_experience(candidate):
    if "track" in candidate:
        return candidate["track"] == "experience"
    origins = candidate.get("origins", [])
    if isinstance(origins, str):
        origins = json.loads(origins)
    return candidate.get("origin") == EXPERIENCE_ORIGIN or EXPERIENCE_ORIGIN in origins


def eligible_date(date, start, end, *, experience=False):
    if not date:
        return True  # 未知日期只能进入核读，不能据此发布。
    value = dt.date.fromisoformat(date)
    return value <= dt.date.fromisoformat(end) and (experience or value >= dt.date.fromisoformat(start))


def check_experience(finding):
    if finding.get("category") != "practice":
        raise ValueError("历史经验必须是核读后的 practice，不能用旧公告或假设方案替代")
    if any(not isinstance(finding.get(key), str) or not finding[key].strip()
           for key in ("problem", "mechanism", "conclusion", "implication")):
        raise ValueError("经验缺少具体任务、尝试反馈、观察结果或有条件的启示")
    limits = finding.get("limitations")
    if (not isinstance(limits, list) or not limits
            or any(not isinstance(value, str) or not value.strip() for value in limits)
            or not finding.get("evidence")):
        raise ValueError("经验缺少原文证据或适用边界")


def excluded(text):
    return bool(EXCLUDED.search(text))


def window(now, start=None, end=None):
    end_date = dt.date.fromisoformat(end) if end else now.date() - dt.timedelta(days=1)
    start_date = dt.date.fromisoformat(start) if start else end_date - dt.timedelta(days=6)
    if start_date > end_date or end_date >= now.date():
        raise ValueError("只能生成已经结束的日期窗口；开始日期不能晚于结束日期")
    return start_date.isoformat(), end_date.isoformat()


def check_prose(title, paragraphs):
    if excluded("\n".join([title, *paragraphs])):
        raise ValueError("正文含排除领域内容")
    if len(paragraphs) < 3 or any(not p.strip() for p in paragraphs):
        raise ValueError("正文需要至少三个非空自然段")
    for value in [title, *paragraphs]:
        if any(c in value for c in ("<", ">", "\n", "\r", "\x00", "\u2014", "\u2013")):
            raise ValueError("正文含 HTML、换行或不支持的标点")
        if re.search(r"https?://|\]\(|!\[|^\s*(?:#|>|[-*] |\d+\. )", value):
            raise ValueError("正文只接受自然段，链接由程序从已核对来源生成")
