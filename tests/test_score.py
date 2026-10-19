import pytest

from sba_loanscout.score import score_site
from sba_loanscout.signals import SignalHit, match_site


def hit(signal_id, category, weight, url="https://example.com/"):
    return SignalHit(
        signal_id=signal_id,
        category=category,
        weight=weight,
        snippet="…",
        source_url=url,
        page_type="other",
    )


def test_empty_hits_score_zero(rules):
    score = score_site([], rules)
    assert score.total == 0.0
    assert not score.disqualified
    assert all(v == 0.0 for v in score.categories.values())


def test_score_is_bounded_to_100(rules):
    huge = [hit(f"s{i}", "growth", 1000) for i in range(20)]
    score = score_site(huge, rules)
    assert 0 <= score.total <= 100
    assert score.categories["growth"] < 100


def test_saturation_puts_raw_equal_to_k_at_fifty(rules):
    k = rules.saturation["growth"]
    score = score_site([hit("s1", "growth", k)], rules)
    assert score.categories["growth"] == pytest.approx(50.0, abs=0.1)


def test_more_signals_score_higher(rules):
    few = score_site([hit("a", "growth", 10)], rules)
    many = score_site(
        [hit("a", "growth", 10), hit("b", "growth", 15), hit("c", "growth", 12)],
        rules,
    )
    assert many.total > few.total


def test_disqualifier_zeroes_total_but_keeps_row(rules):
    hits = [hit("target_vertical", "fit", 16), hit("is_lender", "disqualify", 100)]
    score = score_site(hits, rules)

    assert score.disqualified
    assert score.total == 0.0
    assert score.disqualify_reason == "is_lender"
    # sub-scores survive so the killing rule stays visible and tunable
    assert score.categories["fit"] > 0


def test_multiple_disqualify_reasons_are_listed(rules):
    hits = [hit("is_lender", "disqualify", 100), hit("nonprofit", "disqualify", 100)]
    score = score_site(hits, rules)
    assert score.disqualify_reason == "is_lender, nonprofit"


def test_disqualify_hits_excluded_from_evidence(rules):
    hits = [hit("is_lender", "disqualify", 100), hit("target_vertical", "fit", 16)]
    score = score_site(hits, rules)
    assert "is_lender" not in score.signal_ids
    assert all(h.category != "disqualify" for h in score.top_hits())


def test_category_weights_are_respected(rules):
    """fit is weighted above growth, so equal raw scores favor fit."""
    fit = score_site([hit("a", "fit", 30)], rules)
    growth = score_site([hit("a", "growth", 25)], rules)
    # both saturate to 50 in their own category
    assert fit.categories["fit"] == pytest.approx(50.0, abs=0.1)
    assert growth.categories["growth"] == pytest.approx(50.0, abs=0.1)
    assert fit.total > growth.total


def test_end_to_end_growing_business_outranks_thin_page(page_factory, rules):
    good = score_site(match_site([page_factory("growing_hvac.html")], rules), rules)
    thin = score_site(match_site([page_factory("thin_spa.html")], rules), rules)

    assert good.total > thin.total
    assert good.total > 40, "a clearly expanding local business should rank well"
    assert not good.disqualified


def test_end_to_end_bank_is_disqualified(page_factory, rules):
    score = score_site(match_site([page_factory("bank_disqualify.html")], rules), rules)
    assert score.disqualified
    assert "is_lender" in score.disqualify_reason


def test_bank_disqualified_by_footer_only_evidence(page_factory, rules):
    """Regression: a bank whose only tell is 'Member FDIC' in the footer.

    Before footers were kept, this site scored as a qualified lead — it has
    hiring, tenure, and multi-location signals and says nothing about lending
    in its body copy.
    """
    page = page_factory("bank_footer_only.html")
    score = score_site(match_site([page], rules), rules)

    assert score.disqualified, "a Member FDIC footer must disqualify"
    assert "is_lender" in score.disqualify_reason
    assert score.total == 0.0
    # the growth signals really were there — that is why this was dangerous
    assert score.categories["growth"] > 0
