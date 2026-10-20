"""Base agent.

Design goals:
  - Transparent prompts: each agent loads `core/prompts/<name>.md` (plain, editable).
  - Product context is INJECTED into the prompt at runtime ({{brand_voice}},
    {{product_facts}}, {{editorial_rules}}, {{audience}}, {{project_name}}) — so the same agent
    code works for any product.
  - Lazy Claude client: agents can run in stub mode without an API key.
"""
from __future__ import annotations

import os
import re

from .. import usage
from ..config import ROOT, Project
from ..store import output_root


_TRAILING_COMMA = None  # compiled lazily in _json_repairs


def _json_repairs(candidate: str):
    """Cheap, safe repairs for the LLM-JSON failure shapes we have actually measured.

    3 of 7 editor reads failed to parse in the implementer's field test — at N=3 reads per
    round that invalidates ~40% of rounds, doubling the loop's cost. The failures are not
    exotic: trailing commas before a closing brace/bracket, and smart quotes used AS the JSON
    string delimiters. Each repair is tried alone and in combination; anything that still does
    not parse is a real failure and goes through the existing retry.
    """
    import json
    import re

    global _TRAILING_COMMA
    if _TRAILING_COMMA is None:
        _TRAILING_COMMA = re.compile(r",\s*([}\]])")

    variants = [candidate]
    variants.append(_TRAILING_COMMA.sub(r"\1", candidate))
    # smart quotes as delimiters (only when the text contains no plain double quotes at all,
    # so we never corrupt legitimate curly quotes INSIDE properly-delimited strings)
    if '"' not in candidate and ("“" in candidate or "”" in candidate):
        dumb = candidate.replace("“", '"').replace("”", '"')
        variants.append(dumb)
        variants.append(_TRAILING_COMMA.sub(r"\1", dumb))
    for v in variants:
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            continue
    return None


def _last_json_object(text: str):
    """The LAST balanced {...} in the text, parsed — or None.

    Exists because the model sometimes emits reasoning prose BEFORE the JSON (measured: a
    literal <think>…</think> block discussing the checks, braces and all), and the greedy
    first-{-to-last-} fallback then captures think-garbage. The real verdict is at the end;
    walk back from the final '}' balancing braces, ignoring braces inside strings.
    """
    end = text.rfind("}")
    attempts = 0
    while end >= 0 and attempts < 5:
        depth, i = 0, end
        while i >= 0:
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    got = _json_repairs(text[i:end + 1])
                    if got is not None:
                        return got
                    break
            i -= 1
        attempts += 1
        end = text.rfind("}", 0, max(end - 1, 0))
    return None


def strict_bool(value) -> bool | None:
    """A model's yes/no: True, False, or None when it is neither. `bool("false")` is True, so a
    verdict the model wrote as a string used to read as its opposite (RF-22)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    t = str(value).strip().lower() if value is not None else ""
    return True if t in ("true", "yes") else False if t in ("false", "no") else None


def parse_json(text: str):
    """Best-effort JSON extraction from a Claude response (handles ```json fences)."""
    import re

    text = (text or "").strip()
    # reasoning blocks are prose, not payload — strip before any structural matching
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^<think>.*", "", text, flags=re.DOTALL).strip() or text
    got = _json_repairs(text)
    if got is not None:
        return got
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if m:
        got = _json_repairs(m.group(1).strip())
        if got is not None:
            return got
    m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if m:
        got = _json_repairs(m.group(1))
        if got is not None:
            return got
    return _last_json_object(text)


# --- summary numbers without a source (W4) ------------------------------------------------------
# A finding's summary is Parallel's prose over its facts, and it can carry a number none of those
# facts has (measured: 5 of 59 live summaries). The summary stays - it says what the facts mean
# together - but such a number is marked, so no agent mistakes it for a citable figure.
_DATE_SPANS = re.compile(
    r"\b\d{4}-\d{1,2}(?:-\d{1,2})?\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:,?\s+\d{4})?\b"
    r"|\bq[1-4]\s+\d{4}\b", re.I)
_NUMBER = re.compile(
    r"(?<![\w.])(?P<cur>[$€£])?\s?(?P<num>\d[\d,]*(?:\.\d+)?)(?P<unit>\s?(?:%|percent|x\b|ms\b|k\b|m\b|bn\b"
    r"|million|billion|thousand|hours?|minutes?|seconds?|languages?|voices?|words?|characters?))?"
    r"(?P<suffix>-[A-Za-z]+)?", re.I)       # "13-language": the mark goes after the whole word


def _num_key(num: str) -> str:
    t = num.replace(",", "")
    return t.rstrip("0").rstrip(".") if "." in t else t


def _is_countable(m) -> bool:
    num = m.group("num").replace(",", "")
    if re.fullmatch(r"(19|20)\d\d", num) and not (m.group("cur") or m.group("unit")):
        return False                                   # a year
    digits = num.replace(".", "").lstrip("0")
    return bool(m.group("cur") or m.group("unit")) or len(digits) >= 2


def mark_unsourced_numbers(summary: str, fact_texts: list[str]) -> str:
    """Append " (no source)" to countable numbers in `summary` that appear in none of `fact_texts`."""
    known = {_num_key(m.group("num")) for m in _NUMBER.finditer(" ".join(fact_texts))}
    masked = _DATE_SPANS.sub(lambda d: "\0" * len(d.group(0)), summary)   # dates never count
    out, last = [], 0
    for m in _NUMBER.finditer(masked):
        if not _is_countable(m) or _num_key(m.group("num")) in known:
            continue
        out.append(summary[last:m.end()] + " (no source)")
        last = m.end()
    return "".join(out) + summary[last:]


class BaseAgent:
    name: str = "base"          # -> core/prompts/<name>.md
    model_role: str = "worker"  # 'worker' | 'editor'

    def __init__(self, project: Project, config: dict):
        from ..config import runtime_config

        self.project = project
        self.config = runtime_config(config, project)   # the profile's settings, not only config.yaml
        self._client = None

    # --- Claude ---
    # The SDK defaults to a 10-minute request timeout with 2 retries, so ONE hung request can
    # stall a run for half an hour at 0% CPU with nothing in the log. Observed: a generation sat
    # sleeping for 24 minutes inside keyword research, no child processes, no output — every
    # other bounded call in the pipeline (Ahrefs 30s, page fetch 15s, Playwright 25s) had already
    # been given a limit; the model client was the one that had not.
    REQUEST_TIMEOUT_S = float(os.getenv("ANTHROPIC_TIMEOUT_S", "600"))
    # 2 retries was not enough for a run that spends 40+ minutes making dozens of calls: one
    # connection error at minute 35 threw away the writer, 14 visuals and four QA rounds.
    # Retries are cheap (a failed request bills nothing); a lost run is not.
    MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "5"))

    @property
    def client(self):
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"),
                                     timeout=self.REQUEST_TIMEOUT_S,
                                     max_retries=self.MAX_RETRIES)
        return self._client

    @property
    def model(self) -> str:
        return self.project.models.get(
            self.model_role, self.project.models.get("worker", "claude-sonnet-5-5")
        )

    # --- prompt ---
    def load_prompt(self) -> str:
        # the team's edit from the Studio (Settings > Prompts) wins over the shipped file; it
        # lives on the article volume, so a release does not undo it (core/prompt_overrides.py)
        p = ROOT / self.config["paths"]["prompts"] / f"{self.name}.md"
        shipped = p.read_text(encoding="utf-8") if p.exists() else ""
        over = output_root(self.config) / "_prompts" / f"{self.name}.md"
        if over.is_file():
            from ..prompt_overrides import with_shipped_contract

            # the output format the code parses always runs as shipped
            return with_shipped_contract(over.read_text(encoding="utf-8"), shipped)
        return shipped

    def edited_prompt_hint(self, name: str | None = None) -> str:
        """For a failure note: names the prompt when it runs as edited in the Studio, so a broken
        edit is not mistaken for an outage."""
        from ..prompt_overrides import edited_hint

        return edited_hint(output_root(self.config), name or self.name)

    def _inject(self, tmpl: str) -> str:
        # {{current_year}} / {{today}}: a model writes the year it remembers, so "2026 guide"
        # articles were coming out dated 2025. The SEO rule is that any year in the copy is
        # THIS year, which means the agents have to be told what year it is.
        from datetime import date

        today = date.today()
        brand = self.project.brand
        p = self.project

        def given(text, empty: str) -> str:
            # a text the project left empty says so, instead of a heading over nothing
            return text if (text or "").strip() else empty

        repl = {
            "{{own_domain}}": brand.domain if brand else "our site",
            "{{min_product_links}}": str(brand.min_product_links if brand else 2),
            "{{words_per_link}}": str(brand.words_per_link if brand else 350),
            "{{brand_voice}}": given(p.brand_voice, "(None given: write in plain, clear, confident English.)"),
            "{{product_facts}}": given(p.product_facts, "(None given: state no feature, plan, price or number "
                                                        "of our own product.)"),
            "{{editorial_rules}}": given(getattr(p, "editorial_rules", ""), "(None given.)"),
            "{{audience}}": given(p.audience, "readers researching this topic"),
            "{{project_name}}": given(p.name, "our company"),
            "{{current_year}}": str(today.year),
            "{{today}}": today.isoformat(),
        }
        for k, v in repl.items():
            tmpl = tmpl.replace(k, v)
        return tmpl

    def system_prompt(self) -> str:
        # the output-contract marker comments are for saving and restoring in the Studio, not
        # for the model (review of a80b044)
        from ..prompt_overrides import strip_markers

        return self._inject(strip_markers(self.load_prompt()))

    # The set of facts an article may cite. ONE definition, because two definitions is how this
    # went wrong: the Copywriter took the first 5 facts of each finding with no overall cap,
    # while the Editor took the first 3 and then truncated the whole list to 40 lines. So the
    # writer could legitimately cite fact 4 of a finding, or anything past line 40, and the
    # Editor — not seeing it — raised a CRITICAL for an unsourced claim. Measured on `audio ai
    # news`: 11 findings, 37 facts, and both Criticals in the run were false. 'Audioboost',
    # 'podnews 45.7%' and the Forbes briefing were all in the corpus. A blocking Critical then
    # forces revisions that damage a correct article.
    #
    # If this ever has to truncate, it SAYS so, because an Editor that reads absence as
    # fabrication must never be handed a silently shortened corpus.
    MAX_CITABLE_FACTS = 200

    @staticmethod
    def citable_facts(research: dict | None) -> tuple[list[dict], bool]:
        """Every fact any agent may treat as sourced, and whether the list had to be cut."""
        r = research or {}
        out: list[dict] = []
        for s in (r.get("key_statistics") or []):
            out.append({"kind": "stat", "claim": s.get("stat"), "value": s.get("value"),
                        "source_url": s.get("source_url"), "question": None, "confidence": None,
                        "summary": None})
        for f in (r.get("findings") or []):
            for fact in (f.get("facts") or []):
                out.append({"kind": "fact", "claim": fact.get("claim"),
                            "value": fact.get("value"), "source_url": fact.get("source_url"),
                            "question": f.get("question"), "confidence": f.get("confidence"),
                            "summary": f.get("summary")})
        cut = len(out) > BaseAgent.MAX_CITABLE_FACTS
        return out[:BaseAgent.MAX_CITABLE_FACTS], cut

    def render_corpus(self, research: dict | None, article_dir=None) -> str:
        """THE view of the research every agent gets, byte for byte; with the article's folder,
        the project's own research sources add theirs (core/research_sources.py).

        Five components used to see the corpus five different ways: the writer grouped by
        question with summaries; the editor a flat list worded apart; the fixer's span re-check a
        flat list without the summaries (so a figure from a summary looked unsourced to it); the
        FAQ answerer nothing; the chart builder the first 60 facts without sources. Two views of
        one fact is this project's most expensive bug shape, so there is one function and one
        text: what a component judges against is what the writer was shown.
        """
        r = research or {}
        facts, cut = self.citable_facts(r)
        L: list[str] = []
        # A competitor's selling page can be a source but never a link (validators.selling_surface).
        # Only code knew which sources those were, so the editor kept asking for the link the
        # fixer is not allowed to add, and the finding went stuck as a Critical (trial 2,
        # a translation agency's rates page). The mark tells every reader of this corpus.
        from ..validators import selling_surface

        links = getattr(self.project, "links", None) or {}
        hosts, docs = links.get("competitor_hosts") or (), links.get("competitor_doc_paths") or ()
        in_prose: list[str] = []

        def src(url) -> str:
            if selling_surface(url, hosts, docs):
                in_prose.append(url)
                return f"[source: {url} (a competitor's selling page: cite it in prose)]"
            return f"[source: {url}]"

        stats = [f for f in facts if f["kind"] == "stat"]
        if stats:
            L.append("KEY STATISTICS")
            for f in stats:
                val = f": {f['value']}" if f.get("value") else ""
                L.append(f"- {f.get('claim')}{val}  {src(f.get('source_url'))}")
        groups: dict[str, list[dict]] = {}
        for f in facts:
            if f["kind"] == "fact":
                groups.setdefault(f.get("question") or "", []).append(f)
        if groups:
            L.append("\nFINDINGS — grouped by the question we asked. A question is not a finding: a "
                     "name that appears only inside a question was never sourced.")
            for q, items in groups.items():
                head = items[0]
                L.append(f'Q asked: "{q}"' if q else "Q asked: (not recorded)")
                L.append(f"  found (confidence: {head.get('confidence') or 'not stated'}):")
                if head.get("summary"):
                    facts_text = [f"{f.get('claim') or ''} {f.get('value') or ''}" for f in items]
                    L.append(f"  summary: {mark_unsourced_numbers(head['summary'], facts_text)}")
                for f in items:
                    val = f" = {f['value']}" if f.get("value") else ""
                    L.append(f"  - {f.get('claim') or ''}{val}  {src(f.get('source_url'))}")
        if in_prose:
            L.append("\nA source marked \"cite it in prose\" is a competitor's selling page: the sentence "
                     "names the page (\"<Competitor>'s rates page lists …\"), and that attribution is "
                     "its whole citation. It is never hyperlinked.")
        if cut:
            L.append("\n(This list is TRUNCATED. A claim not found here may still be sourced — its "
                     "absence alone is not evidence against it.)")
        unanswered = [str(f_.get("question")) for f_ in (r.get("findings") or [])
                      if f_.get("question") and not (f_.get("facts") or [])]
        gaps = [str(g) for g in (r.get("gaps") or [])]
        if gaps or unanswered:
            L.append("\nGAPS — asked or named, but no reliable source was found; never state these "
                     "as fact")
            L += [f"- {g}" for g in gaps]
            L += [f'- asked, not answered: "{q}"' for q in unanswered]
        text = ("\n".join(L).strip()
                or "(no researched facts — introduce no statistics or third-party claims)")
        from ..research_sources import corpus_blocks

        extra = corpus_blocks(article_dir, self.project)
        return text + ("\n\n" + extra if extra else "")

    @staticmethod
    def length_sample(bench: dict | None) -> dict:
        """The length figures, from the PRIMARY SERP's prototypes only.

        `content_benchmark` is computed twice by the keyword stage: once over every prototype
        (format — headings, images, tables) and once over the pages from our own SERP (length).
        Borrowed pages answer a neighbouring query, and length follows coverage, so they must not
        set our target or our ceiling. Falls back to the combined figures for runs recorded
        before the split existed.
        """
        b = bench or {}
        if b.get("length_sample") is not None:
            return {"sample": b.get("length_sample"), "words_min": b.get("length_words_min"),
                    "words_median": b.get("length_words_median"),
                    "words_max": b.get("length_words_max"),
                    "words_target": b.get("length_words_target")}
        return {"sample": b.get("sample"), "words_min": b.get("words_min"),
                "words_median": b.get("words_median"), "words_max": b.get("words_max"),
                "words_target": b.get("words_target")}

    # Sampling temperature stays at the API DEFAULT (1.0) on purpose, and that is a decision,
    # not an accident: the QA loop's union-of-N-reads mechanism works BECAUSE reads sample
    # differently — the measured read-to-read variance is the outlier-suppression that replaced
    # the single-read verdict. Anyone tempted to pin temperature to 0 "for determinism" would
    # make the N reads near-identical and silently nullify the whole mechanism. (The 1.0 SDK
    # removed the `temperature` kwarg entirely, so the default is also the only option here.)

    @staticmethod
    def _text_from(resp) -> str:
        """The text of a response. Claude 5 models return adaptive-thinking blocks FIRST in
        `content`, so `content[0].text` crashes on them — join the text blocks, skip thinking."""
        parts = [b.text for b in (resp.content or [])
                 if getattr(b, "type", "") == "text" and getattr(b, "text", None)]
        return "\n".join(parts)

    def _record_anthropic_usage(self, resp, model: str, agent_key: str | None = None) -> None:
        """The call's real usage into the article's cost ledger (core/usage.py): the model that
        answered, and cache writes split by TTL (a 1-hour write costs 2x input, a 5-minute one
        1.25x; the QA panel caches its system prompt for an hour)."""
        try:
            u = resp.usage
            cc = getattr(u, "cache_creation", None)
            w1h = int(getattr(cc, "ephemeral_1h_input_tokens", 0) or 0) if cc is not None else 0
            w5m = int(getattr(cc, "ephemeral_5m_input_tokens", 0) or 0) if cc is not None else 0
            if not (w1h or w5m):
                w5m = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
            usage.record("anthropic", getattr(resp, "model", None) or model,
                         {"input_tokens": int(getattr(u, "input_tokens", 0) or 0),
                          "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
                          "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
                          "cache_write_5m_tokens": w5m, "cache_write_1h_tokens": w1h},
                         agent=agent_key or self.name)
        except Exception:  # noqa: BLE001 — accounting must never fail a call
            pass

    # A response that stops AT the cap is truncated mid-sentence — and mid-JSON for every call
    # that asks for JSON. The caller's parse then fails and the work is silently lost: one such
    # call cost an article all six of its charts, and two QA reads died the same way in the same
    # run. Adaptive thinking makes the cap easy to reach, since thinking bills as output too.
    # So notice the cap and buy the answer one larger budget — it only fires when the first
    # attempt was already paid for and wasted.
    TRUNCATION_RETRY_CAP = 32000

    def _create_untruncated(self, make, budget: int, model: str, agent_key: str | None = None):
        resp = make(budget)
        self._record_anthropic_usage(resp, model, agent_key=agent_key)
        if getattr(resp, "stop_reason", None) == "max_tokens" and budget < self.TRUNCATION_RETRY_CAP:
            resp = make(min(budget * 2, self.TRUNCATION_RETRY_CAP))
            self._record_anthropic_usage(resp, model, agent_key=agent_key)
        return resp

    def call_claude(self, user: str, system: str | None = None, max_tokens: int | None = None) -> str:
        # Claude 5 adaptive thinking bills as OUTPUT on EVERY call, not just panel reads — an
        # uncapped brief burned its whole 8000 budget and truncated its JSON (the editor then
        # chased a "missing meta description" that was really a truncation). One knob for all
        # plain calls: models.effort (default medium ≈ the previous generation's behavior).
        effort = self.config.get("models", {}).get("effort", "medium")

        def _make(mt):
            kwargs = dict(
                model=self.model,
                max_tokens=mt,
                system=system or self.system_prompt(),
                messages=[{"role": "user", "content": user}],
            )
            try:
                return self.client.messages.create(**kwargs, output_config={"effort": effort})
            except TypeError:
                return self.client.messages.create(**kwargs,
                                                   extra_body={"output_config": {"effort": effort}})
            except Exception as e:  # noqa: BLE001 — a model without the knob gets a plain call
                if "effort" in str(e).lower() or "output_config" in str(e).lower():
                    return self.client.messages.create(**kwargs)
                raise

        budget = max_tokens or self.config.get("models", {}).get("max_tokens", 8192)
        return self._text_from(self._create_untruncated(_make, budget, self.model))

    def call_claude_cached(self, model: str, user_base: str, user_tail: str = "",
                           system: str | None = None, max_tokens: int | None = None,
                           agent_key: str | None = None, cache_base: bool = True) -> str:
        """A Messages call with prompt caching on the stable prefix.

        The QA panel's reads share the system prompt (checklist + product facts, stable all
        session) and the user base (article + facts + measured blocks, stable within a round);
        only the router-hint tail differs between read 1 and the rest.

        `cache_base` decides whether the VOLATILE block (article + measured blocks) is worth a
        breakpoint. It is not, whenever nothing will read it back: with a cross-vendor panel of
        three distinct models, each model sees each article version exactly once, so the base
        can never be hit and the breakpoint only buys the 1.25x write premium. Callers that do
        reuse a base within one model pass True. The stable system block is always cached.
        """
        # Claude 5's adaptive thinking defaults to effort=high and its thinking tokens bill as
        # OUTPUT — a verdict read burned its whole 16k budget thinking and never reached the
        # JSON. The documented knob is `output_config.effort` (a top-level `effort` field is
        # silently ignored — measured). Keep the value CONSTANT across reads and rounds:
        # changing effort between requests invalidates prompt caching (docs, Best practices #5).
        # max_tokens is NOT part of the cache key, so the truncation retry keeps the cache warm.
        effort = ((self.config.get("pipeline") or {}).get("qa") or {}).get("read_effort", "medium")

        def _make(mt):
            base_blk = {"type": "text", "text": user_base}
            if cache_base:
                base_blk["cache_control"] = {"type": "ephemeral"}
            kwargs = dict(
                model=model,
                max_tokens=mt,
                # 1-hour TTL on the STABLE block. Measured: on a run with real visual work
                # between rounds, more than five minutes passed between one model's calls, so
                # every 5-minute entry expired unread — the panel wrote 278k tokens of cache and
                # read 0, paying the 1.25x write premium for nothing. An hour covers a whole
                # article. A 1h entry is readable by an ordinary 5m-default read (measured).
                system=[{"type": "text", "text": system or self.system_prompt(),
                         "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                messages=[{"role": "user", "content": [base_blk]
                           + ([{"type": "text", "text": user_tail}] if user_tail else [])}],
            )
            try:
                return self.client.messages.create(**kwargs, output_config={"effort": effort})
            except TypeError:
                return self.client.messages.create(**kwargs,
                                                   extra_body={"output_config": {"effort": effort}})
            except Exception as e:  # noqa: BLE001 — e.g. a 400 from a model without effort support
                if "effort" in str(e).lower() or "output_config" in str(e).lower():
                    return self.client.messages.create(**kwargs)
                raise

        budget = max_tokens or self.config.get("models", {}).get("max_tokens", 8192)
        return self._text_from(self._create_untruncated(_make, budget, model, agent_key=agent_key))

    def call_openai(self, model: str, user: str, system: str | None = None,
                    max_tokens: int | None = None, agent_key: str = "editor_openai") -> str:
        """One chat.completions call to OpenAI — the cross-vendor voice in the QA panel.

        Kept deliberately thin: same system+user text as a Claude read, JSON parsed by the
        same tolerant parser downstream. OpenAI caches long shared prefixes automatically
        (cached input billed at their discounted rate); usage is recorded under its own
        agent key so the cost report can price vendors separately. A failure here costs one
        dropped read — the round stays valid at MIN_VALID_READS.
        """
        import requests

        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "system", "content": system or self.system_prompt()},
                               {"role": "user", "content": user}],
                  "max_completion_tokens": max_tokens
                  or self.config.get("models", {}).get("max_tokens", 8192)},
            timeout=self.REQUEST_TIMEOUT_S,
        )
        r.raise_for_status()
        data = r.json()
        try:
            u = data.get("usage") or {}
            cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
            usage.record("openai", data.get("model") or model,
                         {"input_tokens": int(u.get("prompt_tokens") or 0) - cached,
                          "output_tokens": int(u.get("completion_tokens") or 0),
                          "cache_read_tokens": cached}, agent=agent_key)
        except Exception:  # noqa: BLE001 — accounting must never fail a call
            pass
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    def call_claude_json(self, user: str, system: str | None = None, max_tokens: int | None = None):
        """Call Claude and parse a JSON object/array from the response. Returns (parsed_or_None, raw_text)."""
        raw = self.call_claude(user, system=system, max_tokens=max_tokens)
        return parse_json(raw), raw

    # --- meta tags ---
    META_SPEC = {"title_min": 50, "title_max": 70,
                 "description_min": 130, "description_max": 150}

    # Asked to count characters, the model started SHIPPING the count inside the value:
    # "Real Time Meeting Translation: Best Tools in 2026 (62 ch)". The annotation then counts
    # toward the length and fails the very check it was proving, so it is stripped in code.
    _META_ANNOTATION = re.compile(
        r"\s*[\(\[]\s*\d{1,3}\s*(?:ch|chr|chars?|characters?)\s*\.?\s*[\)\]]\s*$", re.I)

    @classmethod
    def _clean_meta_value(cls, text) -> str:
        t = str(text or "").strip()
        # quotes first: a trailing quote would hide the annotation from the end anchor
        if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1].strip()
        t = cls._META_ANNOTATION.sub("", t).strip()
        # the no-em-dash rule is absolute and the repair prompt alone does not hold (an em
        # dash shipped inside meta_description twice) — deterministic ban, like the body's
        t = re.sub(r"\s*—\s*", ", ", t)
        return re.sub(r"\s+", " ", t)

    @staticmethod
    def _char_delta(text: str, lo: int, hi: int) -> str:
        n = len(text)
        if n < lo:
            return f"currently {n} characters — ADD between {lo - n} and {hi - n} characters"
        if n > hi:
            return f"currently {n} characters — REMOVE between {n - hi} and {n - lo} characters"
        return f"currently {n} characters — correct, leave the length alone"

    def ensure_meta(self, meta: dict, primary: str, h1: str, spec: dict | None = None,
                    attempts: int = 3) -> dict:
        """Validate meta tags in code and repair with the EXACT character delta.

        Models cannot count characters: asked for "50-70" they return 49 and assert it is 60,
        and repeating the range does not help — the prompt has to say "add 7 characters".
        Same discipline as the QA fixer: verify in code, keep the best version, never trust
        the rewrite. Returns {meta_title, meta_description, problems, rounds}.
        """
        from ..validators import validate_meta

        spec = {**self.META_SPEC, **(spec or {})}
        cur = {"meta_title": self._clean_meta_value(meta.get("meta_title")),
               "meta_description": self._clean_meta_value(meta.get("meta_description"))}

        def check(m):
            return validate_meta(m, primary, h1, spec["title_min"], spec["title_max"],
                                 spec["description_min"], spec["description_max"])

        best, problems, rounds = cur, check(cur), 0
        while problems and rounds < attempts:
            rounds += 1
            cand = self._repair_meta(best, primary, h1, spec, problems)
            if not cand:
                break
            left = check(cand)
            if len(left) < len(problems):
                best, problems = cand, left
            if not problems:
                break
        return {**best, "problems": problems, "rounds": rounds}

    def _repair_meta(self, meta, primary, h1, spec, problems) -> dict | None:
        from datetime import date

        prompt = (
            "Rewrite these meta tags so they meet the spec EXACTLY.\n\n"
            f"PRIMARY KEYWORD (must appear in both, verbatim): {primary}\n"
            f"H1 (the meta_title must NOT be identical to it): {h1 or '(none)'}\n\n"
            f"meta_title: {meta['meta_title']}\n"
            f"  target {spec['title_min']}-{spec['title_max']} characters; "
            f"{self._char_delta(meta['meta_title'], spec['title_min'], spec['title_max'])}\n"
            f"meta_description: {meta['meta_description']}\n"
            f"  target {spec['description_min']}-{spec['description_max']} characters; "
            f"{self._char_delta(meta['meta_description'], spec['description_min'], spec['description_max'])}\n\n"
            "PROBLEMS TO FIX:\n" + "\n".join(f"- {p}" for p in problems) + "\n\n"
            "Add or cut real words that carry meaning — never pad with filler, and never repeat "
            f"the keyword to make up length. Any year you mention is {date.today().year}.\n"
            "Count the characters of each field before you answer, including spaces — counting "
            "letter by letter is fine and it is what gets this right, but keep the count in "
            "your working: the JSON values must contain ONLY the tag text, never a "
            '"(62 ch)" annotation.\n'
            "No em dashes in either field.\n"
            "FINISH your reply with the JSON object and nothing after it: "
            '{"meta_title":"...","meta_description":"..."}'
        )
        # Room to count out loud. At 400 tokens the model spent its whole budget spelling the
        # title character by character and never reached the JSON, so every repair returned None.
        data, _ = self.call_claude_json(prompt, system="You are an SEO editor.", max_tokens=1500)
        if isinstance(data, dict) and data.get("meta_title"):
            return {"meta_title": self._clean_meta_value(data["meta_title"]),
                    "meta_description": self._clean_meta_value(data.get("meta_description"))}
        return None

    # --- pipeline entry: mutate state, return it ---
    def run(self, state):
        raise NotImplementedError
