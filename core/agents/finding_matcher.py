"""Which raw findings of a review are one problem, and which repeat a finding a person closed
as not important, judged by MEANING (core/qa_review: `findings_from` and `carry_closed`).

Identity by shared words alone failed both ways on the same run (webinar translation, 2026-09-27,
108 raw findings hand-labelled into 24 problems): a wall-of-text finding absorbed ten findings
about citations and a screenshot in its section, while one wall of text split into three as its
word count moved from 401 to 390 to 361. Whether two editor notes describe the same defect is a
judgement, so one small model call per review makes it; the same words decide when it fails.

Code-generated findings are left to the code: their identity is deterministic.
"""
from __future__ import annotations

import re

from .base import BaseAgent

_NEW = re.compile(r"^new:\d{1,3}$")


def _line(text, limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit - 1] + "…"


class FindingMatcher(BaseAgent):
    name = "finding_matcher"
    model_role = "matcher"      # models.matcher if set, else the worker model

    def match(self, items: list[dict], findings: list[dict]) -> list[str | None] | None:
        """One label per raw finding (same order): the id of a closed finding it repeats, a
        "new:N" group label, or None where the same words should decide (code findings, anything
        the model left out or answered with an id that does not exist). None as a whole when the
        call failed."""
        from ..qa_model import as_finding

        raws = [as_finding(f) for f in findings or []]
        idx = [n for n, f in enumerate(raws) if f["fix"] and f.get("source") != "code"]
        if not idx:
            return [None] * len(raws)
        entries = [it for it in items or [] if it.get("source") != "code"]
        if not entries and len(idx) < 2:
            return [None] * len(raws)
        known = {it["id"] for it in entries}
        lines = ["FINDINGS A PERSON CLOSED AS NOT IMPORTANT" if entries else
                 "FINDINGS A PERSON CLOSED AS NOT IMPORTANT: none"]
        for it in entries:
            latest = f" || raised again as: {_line(it['latest_text'], 320)}" if it.get("latest_text") else ""
            lines.append(f"[{it['id']}] (check: {it.get('check') or '-'}) {_line(it.get('text'), 420)}{latest}")
        lines.append("\nTHIS REVIEW'S RAW FINDINGS")
        for n in idx:
            lines.append(f"({n}) (check: {raws[n].get('check') or '-'}) {_line(raws[n]['fix'], 600)}")
        # Claude 5's adaptive thinking bills against max_tokens: at 1,500 a round of 28 findings
        # came back EMPTY (all of it spent thinking). The answer itself is ~30 tokens a finding.
        out, _ = self.call_claude_json("\n".join(lines), system=self.system_prompt(),
                                       max_tokens=max(8000, 80 * len(idx)))
        rows = (out or {}).get("matches") if isinstance(out, dict) else None
        if not isinstance(rows, list):
            return None
        labels: list[str | None] = [None] * len(raws)
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                n = int(row.get("i"))
            except (TypeError, ValueError):
                continue
            entry = str(row.get("entry") or "").strip()
            if n in idx and (entry in known or _NEW.match(entry)):
                labels[n] = entry
        return labels
