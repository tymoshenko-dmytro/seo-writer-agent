"""The check of a change (core/qa_changes, a review's change group): for each finding the
machine worked on, is the problem gone from the text as it is now, and did the edit itself bring
in a defect in the sentences it touched; a redrawn picture without a text spec is looked at by
the picture check. Its answer is a hint beside the change, never a gate. It cannot start a
review of its own.
"""
from __future__ import annotations

from .base import BaseAgent


def _line(text, limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit - 1] + "…"


class EditVerifier(BaseAgent):
    name = "edit_verifier"
    model_role = "editor"       # judging a fix against the facts needs the stronger model

    def _look(self, state, replace: str) -> tuple[bool, str] | None:
        """The picture check on a picture made again (a screenshot, a cover, a drawn chart or
        infographic): (it is right, why), or None when it cannot say (the check is off or the
        call failed), and the old path decides. A drawn picture was judged by its spec alone,
        and a chart whose spec promised wide margins came out with its layout instructions
        printed over it and was called fixed (the live run of 2026-10-05)."""
        import re
        from pathlib import Path

        if not (((self.config.get("pipeline") or {}).get("qa") or {}).get("visual_check", True)):
            return None
        m = re.search(r"assets/[\w.-]+\.(?:png|jpe?g|webp|svg)", replace or "")
        if not m:
            return None
        from .visual_checker import VisualChecker, pictures

        # the picture's kind by what made it (R058-05: a kind change keeps the file's name)
        pic = next((p for p in pictures(state.draft_markdown or "", (state.frontmatter or {}).get("cover"),
                                        state.output_dir) if p["path"] == m.group(0)), None)
        if pic is None or not (Path(state.output_dir) / pic["path"]).is_file():
            return None
        res = VisualChecker(self.project, self.config).check_one(state.output_dir, pic)
        if res.get("verdict") == "right":
            return True, f"the picture check looked at the new {pic['path']}: it shows what its caption says"
        if res.get("verdict") == "problems":
            from .visual_checker import _clip

            # cut at a sentence or a word: a cut at 400 characters ended "…and no pr" (2026-10-06)
            return False, "the picture check still sees: " + _clip("; ".join(p["problem"] for p in res["problems"]), 400)
        return None     # a refusal or an answer it could not use: the edit stays unconfirmed (R9AD-01)

    @staticmethod
    def _visual_spec(state, replace: str) -> str | None:
        """The text spec of a regenerated chart or infographic (its JSON sidecar), or None for a
        screenshot or anything without one."""
        import re
        from pathlib import Path

        from .interactive import picture_kind

        m = re.search(r"assets/[\w.-]+\.(?:png|jpe?g|webp|svg)", replace or "")
        if not m or picture_kind(Path(state.output_dir), m.group(0)) == "screenshot":
            # a screenshot's sidecar holds only its URL and caption, which say nothing about a
            # wrong section or a half-rendered page: those wait for a person's eye
            return None
        side = Path(state.output_dir) / Path(m.group(0)).with_suffix(".json")
        try:
            return side.read_text(encoding="utf-8")[:2500] if side.is_file() else None
        except OSError:
            return None

    def _ask(self, state, batch: list[dict]) -> dict:
        from ..qa_model import instruction

        items = []
        for it in batch:
            e = it.get("last_edit") or {}
            if "satisfied" in e:
                did = f"no edit: the fixer says the text already satisfies this ({_line(e['satisfied'], 300)})"
            elif it.get("_spec"):
                did = (f"the picture was redrawn ({_line(e.get('replace'), 120)}); its spec now reads:\n   "
                       f"{_line(it['_spec'], 2000)}")
            else:
                did = f"BEFORE: {_line(e.get('find'), 700)}\n   AFTER: {_line(e.get('replace'), 700)}"
            items.append(f"[{it['id']}] ({it.get('severity')}, {it.get('check') or '-'}) "
                         f"{_line(instruction(it), 700)}\n   {did}")
        faq = (state.frontmatter or {}).get("faq") or []
        faq_text = "\n".join(f"Q: {q.get('question')}\nA: {q.get('answer')}" for q in faq if isinstance(q, dict))
        rules = (self.project.editorial_rules or "").strip()
        user = (f"PRODUCT FACTS\n{self.project.product_facts}\n\n"
                + (f"EDITORIAL RULES\n{rules}\n\n" if rules else "")
                + f"RESEARCH\n{self.render_corpus(state.research)}\n\n"
                f"ARTICLE AS IT IS NOW\n{state.draft_markdown}\n\n"
                + (f"FAQ AS IT IS NOW\n{faq_text}\n\n" if faq_text else "")
                + "CHECKLIST ITEMS AND WHAT THE FIXER DID\n" + "\n\n".join(items))
        out, _ = self.call_claude_json(user, system=self.system_prompt(),
                                       max_tokens=max(8000, 400 * len(batch)))
        rows = (out or {}).get("items") if isinstance(out, dict) else None
        if not isinstance(rows, list):
            raise ValueError("the edit check returned no item list")
        ids = {it["id"] for it in batch}
        return {str(r.get("id")): r for r in rows if isinstance(r, dict) and str(r.get("id")) in ids}
