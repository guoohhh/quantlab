from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.learning import LearningRepository


POSITIVE_TERMS = (
    "增长",
    "超预期",
    "盈利",
    "增持",
    "回购",
    "中标",
    "上调",
    "创新高",
    "净买入",
    "分红",
)
NEGATIVE_TERMS = (
    "下滑",
    "亏损",
    "减持",
    "处罚",
    "立案",
    "诉讼",
    "下调",
    "风险",
    "暴跌",
    "违约",
)


def collect_news_events(settings: Settings, symbol: str, start: date, end: date) -> dict:
    import akshare as ak

    code = "".join(character for character in symbol if character.isdigit())
    previous_infer = pd.options.future.infer_string
    try:
        # AkShare's current replacement regex is incompatible with Arrow strings.
        pd.options.future.infer_string = False
        frame = ak.stock_news_em(symbol=code)
    finally:
        pd.options.future.infer_string = previous_infer
    if frame.empty:
        return {
            "new_records": 0,
            "matched_records": 0,
            "available": 0,
            "source": "eastmoney via akshare",
        }
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    before = repository.event_count(symbol, start.isoformat(), end.isoformat())
    recorded = set()
    available = 0
    for row in frame.to_dict("records"):
        published = pd.Timestamp(row.get("发布时间"))
        if pd.isna(published) or not (start < published.date() <= end):
            continue
        available += 1
        title = str(row.get("新闻标题") or "").strip()
        content = str(row.get("新闻内容") or "").strip()
        event_type, impact = _classify_event(title + " " + content)
        event_id = repository.add_event(
            symbol=symbol,
            event_date=published.date(),
            event_type=event_type,
            title=title,
            source=str(row.get("文章来源") or "eastmoney"),
            sentiment=_lexicon_sentiment(title + " " + content),
            impact_score=impact,
            payload={
                "published_at": published.isoformat(),
                "url": row.get("新闻链接"),
                "content": content[:2000],
            },
        )
        recorded.add(event_id)
    return {
        "new_records": repository.event_count(symbol, start.isoformat(), end.isoformat()) - before,
        "matched_records": len(recorded),
        "available": available,
        "source": "eastmoney via akshare",
        "coverage_warning": "free endpoint returns a limited recent-news window",
    }


def collect_notice_events(settings: Settings, symbol: str, start: date, end: date) -> dict:
    import akshare as ak

    code = "".join(character for character in symbol if character.isdigit())
    previous_infer = pd.options.future.infer_string
    try:
        pd.options.future.infer_string = False
        try:
            frame = ak.stock_individual_notice_report(
                security=code,
                symbol="全部",
                begin_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
        except KeyError:
            frame = pd.DataFrame()
    finally:
        pd.options.future.infer_string = previous_infer
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    before = repository.event_count(symbol, start.isoformat(), end.isoformat())
    matched = set()
    for row in frame.to_dict("records"):
        announced = pd.Timestamp(row.get("公告日期"))
        if pd.isna(announced) or not (start < announced.date() <= end):
            continue
        title = str(row.get("公告标题") or "").strip()
        notice_type = str(row.get("公告类型") or "").strip()
        event_type, sentiment, impact = _classify_notice(title, notice_type)
        matched.add(
            repository.add_event(
                symbol=symbol,
                event_date=announced.date(),
                event_type=event_type,
                title=title,
                source="eastmoney notice via akshare",
                sentiment=sentiment,
                impact_score=impact,
                payload={
                    "notice_type": notice_type,
                    "company_name": row.get("名称"),
                    "url": row.get("网址"),
                },
            )
        )
    return {
        "new_records": repository.event_count(symbol, start.isoformat(), end.isoformat()) - before,
        "matched_records": len(matched),
        "available": len(frame),
        "source": "eastmoney notice via akshare",
        "coverage_warning": (
            "empty responses are treated as no matching notices; source availability is not guaranteed"
        ),
    }


def collect_all_events(settings: Settings, symbol: str, start: date, end: date) -> dict:
    output = {}
    degraded = []
    for name, collector in (
        ("news", collect_news_events),
        ("notices", collect_notice_events),
    ):
        try:
            output[name] = collector(settings, symbol, start, end)
        except Exception as exc:
            output[name] = None
            degraded.append(f"{name} event collection failed: {exc}")
    output["degraded_sources"] = degraded
    return output


def _lexicon_sentiment(text: str) -> float:
    positive = sum(text.count(term) for term in POSITIVE_TERMS)
    negative = sum(text.count(term) for term in NEGATIVE_TERMS)
    total = positive + negative
    return float(np.clip((positive - negative) / total, -1, 1)) if total else 0.0


def _classify_event(text: str) -> tuple[str, float]:
    if any(term in text for term in ("财报", "业绩", "年报", "季报", "预告")):
        return "earnings", 0.9
    if any(term in text for term in ("处罚", "立案", "监管", "问询", "诉讼")):
        return "regulatory", 0.9
    if any(term in text for term in ("回购", "增持", "减持", "分红", "并购")):
        return "corporate_action", 0.7
    return "news", 0.5


def _classify_notice(title: str, notice_type: str) -> tuple[str, float, float]:
    text = f"{title} {notice_type}"
    if any(term in text for term in ("年报", "季报", "业绩", "财务报告", "盈利预测")):
        return "earnings", _lexicon_sentiment(text), 0.95
    if any(term in text for term in ("处罚", "立案", "监管", "问询", "诉讼")):
        sentiment = -0.3 if "回复" in text else -0.8
        return "regulatory", sentiment, 0.95
    if any(term in text for term in ("质押", "冻结", "减持", "风险提示")):
        return "corporate_action", -0.7, 0.85
    if any(term in text for term in ("回购", "增持", "分红")):
        return "corporate_action", 0.7, 0.8
    return "corporate_action", _lexicon_sentiment(text), 0.6
