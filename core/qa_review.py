"""One review in the QA model.

The panel's reads, the picture check and the code checks look at the current version once
(agents/editor.Editor.collect). Their findings become the review's findings: the same problem
raised by several reads is one finding (the finding matcher judges which; the picture
check's are one per picture and action; the code's are its own), and its
kind, action and target come from the structured answers. When the reads that raised one finding
named different changes, the finding is a proposal: the reads do not agree on what to do.

Then the machine makes the changes it may make by itself (core/qa_changes.apply_safe), each
checked as a hint. A new review is a new record: its findings and proposals are its own, and
nothing an earlier review raised takes effect under it. A finding a person marked not important
under an earlier review stays not important when the new review raises the same problem again:
the finding matcher (or, without it, the same words) says it is the same problem, its kind,
action, target and phrase are the same, and what its target holds has not changed since the
decision (the picture's bytes, the section's text). Anything less stays open (review R829-04).
"""
from __future__ import annotations

import json

from . import qa_model as qm
from . import store, usage
from .qa_changes import Work, _save, _session, apply_safe, check_version, records, subject_of

_BASIS_RANK = {None: 0, "other": 1, "not_in_research": 2, "contradicted": 3}
_SEV_RANK = {"nitpick": 0, "minor": 1, "unknown": 2, "major": 3, "critical": 4}


def _answer(raw: dict) -> dict | None:
    if raw.get("source") == "vision":
        return qm.picture_answer(raw.get("asset"), raw.get("action"))
    a = raw.get("structured") if "structured" in raw else qm.structured_answer(raw)
    return a if a and a.get("action") else (a or None)


def findings_from(union: list, review_id: str, labels: list | None = None,
                  known: set | None = None, shown: set | None = None) -> list[dict]:
    """The review's findings from its raw findings. `labels` is the finding matcher's judgement
    per raw finding: "new:N" per new problem, or the id of a closed finding in `known` it repeats
    (kept as `same_as`); without labels, the same words are one finding."""
    groups: dict = {}
    for n, raw in enumerate(union or []):
        if isinstance(raw, str):
            raw = {"fix": raw}
        text = " ".join(str(raw.get("fix") or raw.get("text") or "").split())
        if not text:
            continue
        src = raw.get("source") or "editor"
        lab = labels[n] if labels and n < len(labels) else None
        lab = str(lab) if lab else None
        a = _answer(raw) or {}
        pic = (a.get("target") or {}).get("asset") if isinstance(a.get("target"), dict) else None
        if shown is not None and pic and pic not in shown and a.get("action") in ("redraw", "recapture"):
            # a picture the article lacks, named by a file it does not have: "redraw
            # assets/diagram_1.png" for a call-flow diagram the brief asked for (2026-10-06)
            a, pic = {**a, "kind": "picture", "action": "add_picture", "target": None}, None
        if a.get("action") == "add_picture":
            # one picture to add per section, whoever asked for it and wherever they put it: a read
            # quoted the sentence it goes after, another named an invented file (2026-10-06)
            key = ("add_picture", " ".join(str(raw.get("section") or "").split()) or text)
        elif pic and a.get("action") in _PICTURE_ACTIONS:
            # one finding per picture, whoever raised it and whatever they asked for: a read and the
            # picture check both asked to remove chart_2, each in its own words, as two cards; and
            # when one read asked to remove a picture and another to redraw it, the card lost the
            # picture and offered only a paid rewrite (2026-10-06). The matcher's word that it
            # repeats a closed finding is kept with it (review R0B9-02)
            key = ("picture", pic)
        elif src == "vision":
            key = ("picture", raw.get("asset"), raw.get("action"))
        elif src == "code":
            key = ("code", raw.get("check"), text)
        elif a.get("action") == "insert_keyword" and a.get("phrase") and a.get("target"):
            # one phrase for one section is one finding, whatever the words or the matcher say: two
            # reads asked for "translation in real time" in the same section, one change put it in,
            # and the other waited for the person with nothing left to do (2026-10-05)
            key = ("phrase", " ".join(str(a["phrase"]).lower().split()), json.dumps(a["target"], sort_keys=True))
        else:
            key = ("meaning", lab) if lab and (lab.startswith("new:") or lab in (known or ())) else ("words", text)
        groups.setdefault(key, []).append({**raw, "fix": text, "_label": lab, "_answer": a or None})
    out = []
    for i, (key, items) in enumerate(groups.items(), 1):
        first = items[0]
        texts = list(dict.fromkeys(x["fix"] for x in items))
        answers = [x["_answer"] if "_answer" in x else _answer(x) for x in items]
        # the same change in other letters is the same change: a phrase is compared as its words
        acted = {}
        for a in answers:
            if a and a.get("action"):
                same = {**a, "phrase": " ".join(str(a["phrase"]).lower().split())} if a.get("phrase") else a
                if a.get("action") in _PICTURE_ACTIONS:
                    same = {**same, "kind": "picture"}        # a read may call a picture's removal a fact
                acted.setdefault(json.dumps(same, sort_keys=True), a)
        answer = next(iter(acted.values())) if len(acted) == 1 else (
            next((a for a in answers if a), None) if not acted else None)
        if key[0] == "add_picture":
            # the same picture asked for: the place a read quoted, if one did
            answer = next((a for a in answers if a and a.get("target")), None) or next(a for a in answers if a)
            acted = {"add_picture": answer}
        sev = max((x.get("severity") for x in items), key=lambda s: _SEV_RANK.get(qm.norm_severity(s), 2))
        basis = max((x.get("basis") for x in items), key=lambda b: _BASIS_RANK.get(b, 0))
        answer = answer or {}
        f = qm.finding(id=f"{review_id}-{i}", text=texts[0], review=review_id,
                       source=_source(first),
                       severity=sev, kind=answer.get("kind"), action=answer.get("action"),
                       target=answer.get("target"), phrase=answer.get("phrase"),
                       section=first.get("section"), basis=basis, check=first.get("check"))
        if len(texts) > 1:
            f["also"] = texts[1:]
        voices = [x.get("voice") for x in items if x.get("voice")]
        if voices:
            f["voices"] = list(dict.fromkeys(voices))
        if len(acted) > 1 and key[0] == "picture" and len(key) == 2:
            # the reads agree on the picture, not on what to do with it: the card offers each
            # change (a removal is free), and an action on the picture answers the finding
            f["kind"], f["target"] = "picture", {"asset": key[1]}
            f["choices"] = [{"action": act, "text": next(x["fix"] for x, a in zip(items, answers)
                                                          if a and a.get("action") == act)}
                            for act in dict.fromkeys(a["action"] for a in acted.values())]
            f["needs_choice"] = "the reads named different changes for this picture: your choice"
        elif len(acted) > 1:
            f["needs_choice"] = "the reads named different changes for it: your choice"
        # it repeats a closed finding only when the matcher says so of every problem it holds: a
        # new problem grouped with a repeat (one picture, two defects) keeps the whole open (RE97-01)
        labels_ = {x.get("_label") for x in items}
        if len(labels_) == 1 and next(iter(labels_)) in (known or ()):
            f["same_as"] = next(iter(labels_))
        # the same words decide only for one wording the matcher did not call new
        f["_by_words"] = len(texts) == 1 and not any(str(lb or "").startswith("new:") for lb in labels_)
        out.append(f)
    return out


_PICTURE_ACTIONS = ("remove_picture", "redraw", "recapture")


def _source(raw: dict) -> str:
    return {"vision": "picture_check", "code": "code"}.get(raw.get("source"), "reader")


_SHAPE = ("kind", "action", "target", "phrase")


def carry_closed(art, rv: dict, new: list[dict]) -> None:
    """A finding a person closed as not important stays closed when a later review raises the
    same problem: the same problem by the matcher (`same_as`) or by its words, the same kind,
    action, target and phrase, and its target holding what it held at the decision."""
    closed = [f for f in rv.get("findings") or [] if f.get("status") == "not_important"]
    for f in new:
        same_as, by_words = f.pop("same_as", None), f.pop("_by_words", False)
        cands = ([c for c in closed if c["id"] == same_as] if same_as else
                 [c for c in closed if c.get("text") == f.get("text")] if by_words else [])
        old = next((c for c in reversed(cands)
                    if all(c.get(k) == f.get(k) for k in _SHAPE)
                    and (c.get("closed") or {}).get("subject") == subject_of(art, f)), None)
        if old is not None:
            f["status"] = "not_important"
            f["closed"] = {**(old.get("closed") or {}), "carried_from": old["id"]}


def _identity(project, config, union: list, closed: list[dict]) -> list | None:
    """The finding matcher's labels: which raw findings are one problem, and which repeat a
    finding a person closed. None when it is off or fails (the same words decide then)."""
    if not ((config.get("pipeline") or {}).get("qa") or {}).get("match_findings", True):
        return None
    from .agents.finding_matcher import FindingMatcher

    try:
        return FindingMatcher(project, config).match(
            [{"id": c["id"], "text": c.get("text"), "check": c.get("check"), "source": c.get("source")}
             for c in closed], union)
    except Exception:  # noqa: BLE001 — identity must never fail a review: the same words decide
        return None


def check_qa_settings(config: dict) -> None:
    """Refuse, before anything is paid for, QA settings no review can pass: the review needs
    `Editor.MIN_VALID_READS` parsed reads, so fewer reads than that ran a paid review that could
    only come back invalid (review RR-08). A run checks it before its first paid step and
    Review again before its reads (RC-05). Raises ValueError."""
    from .agents.editor import Editor

    qa = ((config.get("pipeline") or {}).get("qa") or {})
    reads = int(qa.get("reads", Editor.READS))
    if reads < Editor.MIN_VALID_READS:
        raise ValueError(f"pipeline.qa.reads is {reads}, and a review needs at least "
                         f"{Editor.MIN_VALID_READS} reads to be valid; set qa.reads to "
                         f"{Editor.MIN_VALID_READS} or more in the profile")


def review(slug: str, config=None, project=None, at: str | None = None,
           version: str | None = None) -> dict:
    """Review the current version once, record its findings, and make the safe changes.
    `version`, from Review again, is the version the page showed: checked under the lock before
    anything is paid for (the pipeline's first review has no page and passes none).
    Returns {"review", "version", "findings", "groups", "landed", "not_landed"}."""
    from .agents.editor import Editor

    with _session(slug, "review", "", config, project) as (art, cfg, proj):
        run, rv = records(art)
        if version is not None:
            check_version(art, version)
        check_qa_settings(cfg)
        w = Work(art, run, rv, cfg, proj)
        version = art.version()
        # the version it reads, by its number now: content alone cannot say which one (a Restore
        # writes the same bytes again as a newer version)
        from . import versions as V

        V.ensure(art)
        idx = V.index(art)
        read_n = idx[-1].get("n") if idx and idx[-1].get("token") == version else None
        w.state.qa_rounds = 0
        with usage.stage("review"):
            got = Editor(proj, cfg).collect(w.state)
            closed = [f for f in rv["findings"] if f.get("status") == "not_important"]
            labels = _identity(proj, cfg, got["union"], closed)
        rid = qm.next_review_id(rv["reviews"])
        rec = qm.review_record(id=rid, version=version, at=at, version_n=read_n, valid=bool(got["valid"]),
                               valid_reads=len(got["reads"]), reads_requested=got["n_reads"],
                               models=[r.get("_model") for r in got["reads"]],
                               picture_check=getattr(w.state, "picture_check", None) or None)
        new = findings_from(got["union"], rid, labels, known={c["id"] for c in closed},
                            shown={ref for _, ref in store.shown_image_refs(w.body)})
        carry_closed(art, rv, new)
        rv["reviews"].append(rec)
        rv["findings"].extend(new)
        _save(w)                     # the review stands even if a change below fails
        rep = apply_safe(w, rec)
        _save(w)
        return {"review": rid, "findings": [f["id"] for f in new], **rep}
