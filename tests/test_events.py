from quantlab.workflows.events import _classify_notice, _lexicon_sentiment


def test_notice_classifier_prioritizes_regulatory_and_pledge_risk():
    event_type, sentiment, impact = _classify_notice("收到监管处罚决定书", "处罚公告")
    pledge_type, pledge_sentiment, pledge_impact = _classify_notice(
        "关于股东股份质押的公告", "股份质押、冻结"
    )

    assert event_type == "regulatory"
    assert sentiment < 0
    assert impact == 0.95
    assert pledge_type == "corporate_action"
    assert pledge_sentiment < 0
    assert pledge_impact == 0.85


def test_chinese_lexicon_sentiment_is_bounded():
    assert _lexicon_sentiment("业绩增长超预期并回购") > 0
    assert _lexicon_sentiment("亏损下滑并被立案处罚") < 0
