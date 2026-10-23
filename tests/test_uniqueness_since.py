"""The uniqueness row after an edit (operator, 2026-10-23): "not checked" after every edit alarmed
people. The row says which version was checked, how much of the text is new or rewritten since,
and whether that much could bring it under the warn line; the check runs again from the row."""
from __future__ import annotations

import pytest

SENTENCES = [f"Sentence number {i} explains one more detail about widgets and teams." for i in range(1, 41)]


def _body(sentences):
    return "# Widget guide\n\n## How widgets work\n\n" + " ".join(sentences) + "\n"


def _article(root, sentences):
    from core import store, versions

    art = store.Repository(root).reserve("widget-guide")
    art.write_article("title: Widget guide\nslug: widget-guide\n", _body(sentences))
    with art.lock(holder="test"):
        versions.ensure(art)
    return art


def _edit(art, sentences, what="An edit"):
    from core import versions

    with art.lock(holder="test"), versions.change(art, "the editor", what):
        art.write_article("title: Widget guide\nslug: widget-guide\n", _body(sentences))


def _result(art, md, uniqueness, verdict="ok", **extra):
    from core import store
    from core.plagiarism_report import DEFAULTS, text_sha

    store.write_json(art.path("_plagiarism.json"), {
        "verdict": verdict, "uniqueness": uniqueness, "text_sha": text_sha(md),
        "thresholds": {k: DEFAULTS[k] for k in ("warn_below", "fail_below", "min_passage_words")}, **extra})


def _row(art):
    from core import publication

    return next(c for c in publication.pre_send_checks(art) if c["check"] == "uniqueness")


def test_the_lowest_uniqueness_bound(monkeypatch):
    """Kept sentences carry at most the matches the check found; every new word may be matched."""
    from core import plagiarism_report as pr

    old = _body(SENTENCES)
    new = _body(SENTENCES[:-2] + ["A brand new sentence about widgets and their teams today."] * 2)
    monkeypatch.setattr(pr, "_checked_text", lambda art, result: (1, old))     # the text the check read
    s = pr.since_check(None, {"text_sha": "x", "uniqueness": 96}, new)
    total = sum(len(t.split()) for t in pr._sentences(pr.article_text(new)))
    new_words = 2 * len("A brand new sentence about widgets and their teams today.".split())
    matched = 0.04 * sum(len(t.split()) for t in pr._sentences(pr.article_text(old)))
    assert s["n"] == 1 and s["new"] == round(100 * new_words / total)
    assert s["lowest"] == int(100 * (1 - (matched + new_words) / total))


def test_a_small_edit_after_a_good_check_stays_clear(output_dir):
    """The edit cannot bring the text under the warn line: clear, saying so."""
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 99)
    _edit(art, SENTENCES[:-1] + ["Sentence number 40 now explains the last detail in other words."])
    row = _row(art)
    assert row["state"] == "clear", row
    assert row["detail"] == ("v1 was checked: 99% unique. 2% of the text is new since; even if all of it were "
                             "copied, it stays 96% unique or more")
    assert row["again"] == "Check again" and "Winston credits" in row["again_cost"]


def test_a_large_rewrite_asks_for_another_check(studio, output_dir):
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 96)
    _edit(art, SENTENCES[:20] + [f"A rewritten sentence {i} with other words about widgets." for i in range(20)])
    row = _row(art)
    assert row["state"] == "check_again", row
    assert row["detail"] == "v1 was checked: 96% unique. 44% of the text is new since: if it were copied, the text could fall under 90% unique"
    page = studio.get(f"/a/{art.slug}?tab=publish").get_data(as_text=True)
    assert "check again" in page.split(">Uniqueness<", 1)[1].split("</li>", 1)[0] and "not checked for this one" not in page


def test_a_warning_on_an_earlier_version_stays_found(output_dir):
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 82, verdict="warn")
    _edit(art, SENTENCES[:-1])
    row = _row(art)
    assert row["state"] == "found" and row["detail"] == "v1 was checked: 82% unique (warn). No sentence is new since: check it again"


@pytest.mark.parametrize("case", ["not kept", "no result"])
def test_an_earlier_check_that_cannot_be_read_asks_for_another(output_dir, case):
    art = _article(output_dir, SENTENCES)
    if case == "not kept":
        _result(art, _body(["A text that no version holds."]), 99)
    else:
        _result(art, _body(SENTENCES[:5]), None, verdict="unknown")
    _edit(art, SENTENCES[:-1])
    row = _row(art)
    assert row["state"] == "not_checked" and row["detail"].endswith("check it again"), row


def test_the_check_runs_again_from_its_row(studio, output_dir):
    art = _article(output_dir, SENTENCES)
    page = studio.get(f"/a/{art.slug}?tab=publish").get_data(as_text=True)
    row = page.split(">Uniqueness<", 1)[1].split("</li>", 1)[0]
    assert 'action="/a/widget-guide/uniqueness"' in row and "Check the uniqueness" in row and "Winston credits" in row
    assert page.count('/uniqueness"') == 1                # the row's, not a second link at the bottom
    _result(art, art.text(), 99)
    page = studio.get(f"/a/{art.slug}?tab=publish").get_data(as_text=True)
    assert "Check again" in page.split(">Uniqueness<", 1)[1].split("</li>", 1)[0]


def test_a_check_records_the_version_it_read(output_dir, monkeypatch):
    from core import plagiarism_report as pr

    art = _article(output_dir, SENTENCES)
    monkeypatch.setattr(pr, "analyse", lambda md, cfg=None, dry_run=False: {
        "uniqueness": 99, "verdict": "ok", "sources": [], "passages": []})
    monkeypatch.setattr(pr, "write_report", lambda *a, **kw: None)
    assert pr.build(art.dir)["version_n"] == 1
    _edit(art, SENTENCES[:-1])
    assert pr.build(art.dir)["version_n"] == 2


def test_only_removed_sentences_keep_a_good_result(output_dir):
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 99)
    _edit(art, SENTENCES[:-3])
    row = _row(art)
    assert row["state"] == "clear" and row["detail"] == "v1 was checked: 99% unique. No sentence is new since", row
