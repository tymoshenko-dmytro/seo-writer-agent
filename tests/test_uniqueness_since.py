"""The uniqueness row after an edit (operator, 2026-10-23): "not checked" after every edit alarmed
people. The row says which version was checked, what it scored and how much of the text changed
since; under 5% new and under 5% gone after a good result it stays clear, else it asks to check
again (a rule of thumb the operator chose over a proof); the check runs again from the row."""
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


def test_the_share_of_the_text_that_changed(monkeypatch):
    """New: the letters of sentences the checked text lacks (a second copy too), of the text now;
    gone: the letters of checked sentences the text no longer has, of the checked text."""
    from core import plagiarism_report as pr

    added = "A brand new sentence about widgets and their teams today."
    old, new = _body(SENTENCES), _body(SENTENCES[:-2] + [added] * 2)
    monkeypatch.setattr(pr, "_checked_text", lambda art, result: (1, old))     # the text the check read
    s = pr.since_check(None, {"text_sha": "x", "uniqueness": 96}, new)
    size = lambda t: len(t.replace(" ", ""))                                   # noqa: E731
    assert s["n"] == 1 and s["new_words"] == 2 * len(added.split()) and s["gone_words"] == sum(len(t.split()) for t in SENTENCES[-2:])
    assert s["new"] == pytest.approx(100 * 2 * size(added) / sum(map(size, pr._units(new))))
    assert s["gone"] == pytest.approx(100 * sum(map(size, SENTENCES[-2:])) / sum(map(size, pr._units(old))))


def test_a_small_edit_after_a_good_check_stays_clear(output_dir):
    """Under 5% changed after a good result: clear, with the numbers."""
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 99)
    _edit(art, SENTENCES[:-1] + ["Sentence number 40 now explains the last detail in other words."])
    row = _row(art)
    assert row["state"] == "clear", row
    assert row["detail"] == "v1 was checked: 99% unique. 2% of the text is new and 2% of the checked text is gone since"
    assert row["again"] == "Check again" and "Winston credits" in row["again_cost"]


def test_a_large_rewrite_asks_for_another_check(studio, output_dir):
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 96)
    _edit(art, SENTENCES[:20] + [f"A rewritten sentence {i} with other words about widgets." for i in range(20)])
    row = _row(art)
    assert row["state"] == "check_again", row
    assert row["detail"].startswith("v1 was checked: 96% unique. ") and "of the text is new and" in row["detail"]
    page = studio.get(f"/a/{art.slug}?tab=publish").get_data(as_text=True)
    assert "check again" in page.split(">Uniqueness<", 1)[1].split("</li>", 1)[0] and "not checked for this one" not in page


def test_a_warning_on_an_earlier_version_stays_found(output_dir):
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 82, verdict="warn")
    _edit(art, SENTENCES[:-1])
    row = _row(art)
    assert row["state"] == "found" and row["detail"] == "v1 was checked: 82% unique (warn). No sentence is new since; 2% of the checked text is gone"


@pytest.mark.parametrize("case", ["not kept", "no result"])
def test_an_earlier_check_that_cannot_be_read_asks_for_another(output_dir, case):
    art = _article(output_dir, SENTENCES)
    if case == "not kept":
        _result(art, _body(["A text that no version holds."]), 99)
    else:
        _result(art, _body(SENTENCES[:5]), None, verdict="unknown")
    _edit(art, SENTENCES[:-1])
    row = _row(art)
    assert row["state"] == "check_again" and row["again"] == "Check again", row


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


@pytest.mark.parametrize("removed,state", [(1, "clear"), (3, "check_again")])
def test_removed_sentences_count_too(output_dir, removed, state):
    """Removing unique text raises the share of what matched elsewhere: 5% gone asks again."""
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 99)
    _edit(art, SENTENCES[:-removed])
    row = _row(art)
    assert row["state"] == state and row["detail"].startswith("v1 was checked: 99% unique. No sentence is new since; "), row


def test_nothing_changed_in_the_prose(output_dir):
    """A change outside the prose (the frontmatter): the text the check read is still there."""
    art = _article(output_dir, SENTENCES)
    _result(art, art.text(), 99)
    _edit(art, list(reversed(SENTENCES)))
    row = _row(art)
    assert row["state"] == "clear" and row["detail"] == "v1 was checked: 99% unique. No sentence is new since", row


def test_an_older_result_without_its_texts_hash_asks_for_another_check(output_dir):
    from core import store

    art = _article(output_dir, SENTENCES)
    store.write_json(art.path("_plagiarism.json"), {"verdict": "ok", "uniqueness": 100})
    row = _row(art)
    assert row["state"] == "check_again" and "not checked for this one" not in row["detail"], row


def test_never_checked_is_not_checked(output_dir):
    art = _article(output_dir, SENTENCES)
    row = _row(art)
    assert row["state"] == "not_checked" and row["again"] == "Check the uniqueness", row


def test_a_script_without_spaces_counts_in_full(monkeypatch):
    """The share is of letters: ten Thai words with no spaces are not one word."""
    from core import plagiarism_report as pr

    old = _body(SENTENCES)
    monkeypatch.setattr(pr, "_checked_text", lambda art, result: (1, old))
    s = pr.since_check(None, {"text_sha": "x", "uniqueness": 99}, _body(SENTENCES + ["สวัสดีครับยินดีต้อนรับสู่บทความของเราวันนี้" * 3]))
    assert s["new"] > 5, s


def test_a_checked_draft_is_compared_with_the_article_it_became(output_dir, monkeypatch):
    """A draft is no version: its result keeps the text it read, and the article is compared
    with that (Codex R11F-03)."""
    from core import plagiarism_report as pr, store

    art = store.Repository(output_dir).reserve("widget-guide")
    store.atomic_write_text(art.path("04_draft.md"), _body(SENTENCES))
    monkeypatch.setattr(pr, "analyse", lambda md, cfg=None, dry_run=False: {
        "uniqueness": 99, "verdict": "ok", "sources": [], "passages": [], "plagiarised_words": 3,
        "thresholds": {"warn_below": 90, "fail_below": 80, "min_passage_words": 8}})
    store.write_json(art.path("_plagiarism.json"), pr.build(art.dir, write=False))
    art.write_article("title: Widget guide\nslug: widget-guide\n", _body(SENTENCES[:-1] + ["One new closing sentence."]))
    row = _row(art)
    assert row["state"] == "clear" and row["detail"].startswith("An earlier text was checked: 99% unique."), row


@pytest.mark.parametrize("old,new,changed", [
    # a quote's lines reflowed, emphasis kept: the same sentences (Codex R8B9-02, R45C-01)
    ("> *Every local widget team should*\n> *review the settings before deployment.*",
     "> *Every local widget team should review the settings before deployment.*", False),
    # a quote's halves swapped between its sentences: both are new
    ("> Every local widget team should\n> review the settings before deployment.\n> All new project owners must\n"
     "> compare the prices before renewal.",
     "> Every local widget team should\n> compare the prices before renewal.\n> All new project owners must\n"
     "> review the settings before deployment.", True),
    # the parts after a colon or a semicolon swapped: the sentence is new (R45C-04)
    ("Teams should check two things: the settings before deployment; the prices before renewal.",
     "Teams should check two things: the prices before renewal; the settings before deployment.", True),
    # a table's rows reordered: the same cells
    ("| Advice |\n|---|\n| Review the settings first. |\n| Compare the prices next. |",
     "| Advice |\n|---|\n| Compare the prices next. |\n| Review the settings first. |", False),
])
def test_what_counts_as_a_changed_sentence(monkeypatch, old, new, changed):
    from core import plagiarism_report as pr

    monkeypatch.setattr(pr, "_checked_text", lambda art, result: (1, "# Guide\n\n" + old))
    s = pr.since_check(None, {"text_sha": "x", "uniqueness": 99}, "# Guide\n\n" + new)
    assert bool(s["new_words"]) is changed and bool(s["gone_words"]) is changed, s
