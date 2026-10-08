"""Uniqueness / plagiarism check — Winston AI client.

Why Winston and not Copyleaks, which looked cheaper on paper. Three reasons, and the first one
is architectural rather than commercial:

1. **It is synchronous.** One `POST /v2/plagiarism` returns the whole result in the HTTP
   response. Copyleaks' plagiarism flow is asynchronous and delivers results to a webhook URL
   you provide, and a self-hosted Studio usually sits where no third party can reach a callback
   on it.
2. **It returns the matched passages, not just a score.** `sources[].plagiarismFound[]` carries
   `startIndex`, `endIndex` and the matched `sequence` itself, which is exactly what makes the
   result actionable: an editor can see WHICH sentences matched and rewrite those, instead of
   staring at a percentage.
3. **`excluded_sources` keeps our own site out.** Our own published pages would otherwise show
   up as plagiarism — a draft that reuses phrasing from the project's own site is not
   plagiarism. Excluding our domains keeps the number meaningful.

A useful side effect for the de-watermarking work: the response's `attackDetected` flags
`zero_width_space` and `homoglyph_attack`, which is precisely the shape invisible text
watermarks take.

Costs **two credits per word processed** (their docs, not an estimate), so a 3 500-word article
is ~7 000 credits. Every response reports `credits_used` and `credits_remaining`, so budget
tracking is free.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API_URL = "https://api.gowinston.ai/v2/plagiarism"

# Their documented per-request bounds. Under the floor the API rejects the request; over the
# ceiling it would truncate, so we split instead of silently losing the tail.
MIN_CHARS = 100
MAX_CHARS = 120_000

CREDITS_PER_WORD = 2   # documented, used to warn about spend before a call


class PlagiarismError(RuntimeError):
    pass


def api_key() -> str:
    key = os.getenv("WINSTON_API_KEY", "").strip()
    if not key:
        raise PlagiarismError(
            "WINSTON_API_KEY is not set. Create a key at gowinston.ai (the free tier grants "
            "trial credits without a card) and put it in .env")
    return key


def estimate_credits(text: str) -> int:
    return len(re.findall(r"\S+", text or "")) * CREDITS_PER_WORD


def _post(payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        API_URL, method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:400]
        except Exception:  # noqa: BLE001
            pass
        # their documented codes, translated into something a human can act on
        hint = {401: "the API key was rejected",
                402: "out of credits",
                403: "this key is not allowed to call the plagiarism endpoint — the API is "
                     "requested per plan, so check it is enabled on the account",
                415: "unsupported media type",
                429: "rate limited — retry later",
                }.get(e.code, f"HTTP {e.code}")
        raise PlagiarismError(f"Winston API: {hint}. {body}") from e
    except urllib.error.URLError as e:
        raise PlagiarismError(f"Winston API unreachable: {e.reason}") from e


def check(text: str, excluded_sources: list[str] | None = None, language: str = "auto",
          country: str = "us", timeout: int = 180) -> dict:
    """One synchronous scan. Returns Winston's raw response."""
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        raise PlagiarismError(f"text is {len(text)} characters; the API needs at least {MIN_CHARS}")
    if len(text) > MAX_CHARS:
        raise PlagiarismError(
            f"text is {len(text)} characters, over the {MAX_CHARS} limit — split it first "
            f"(see split_text)")
    payload = {"text": text, "language": language, "country": country}
    if excluded_sources:
        payload["excluded_sources"] = list(excluded_sources)
    raw = _post(payload, timeout=timeout)
    from .. import usage

    try:
        usage.record("winston", "plagiarism", {"credits": int(raw.get("credits_used") or 0),
                                               "words": len(text.split())}, agent="uniqueness")
    except (AttributeError, TypeError, ValueError):
        pass
    return raw


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split on paragraph boundaries if an article ever exceeds the per-request ceiling.

    Ours run 20-30k characters so this should never fire, but truncating a scan would report a
    falsely high uniqueness for the part that was never checked — the one failure mode here
    that is worse than an error.
    """
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            parts.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts


# ---- normalisation ------------------------------------------------------------------
def normalise(raw: dict, our_domains: list[str] | None = None) -> dict:
    """Winston's response, reduced to what the report needs.

    `result.score`'s direction is not documented, so uniqueness is derived from the word
    counts, which are unambiguous, and the vendor score is carried alongside for reference
    rather than trusted.
    """
    res = raw.get("result") or {}
    total_words = res.get("textWordCounts") or 0
    plag_words = res.get("totalPlagiarismWords") or 0
    uniqueness = round(100 - (plag_words / total_words * 100), 2) if total_words else None

    ours = {d.lower().removeprefix("www.") for d in (our_domains or [])}

    def is_ours(url: str) -> bool:
        host = re.sub(r"^https?://", "", (url or "").lower()).split("/")[0].removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in ours)

    sources = []
    for s in raw.get("sources") or []:
        matches = [{"start": m.get("startIndex"), "end": m.get("endIndex"),
                    "text": (m.get("sequence") or "").strip()}
                   for m in (s.get("plagiarismFound") or [])]
        sources.append({
            "url": s.get("url"), "title": s.get("title"),
            "score": s.get("score"),
            "words": s.get("plagiarismWords"),
            "identical": s.get("identicalWordCounts"),
            "similar": s.get("similarWordCounts"),
            "is_citation": bool(s.get("citation")),
            "is_excluded": bool(s.get("is_excluded")),
            "is_ours": is_ours(s.get("url") or ""),
            "published": s.get("publishedDate"),
            "matches": sorted(matches, key=lambda m: -len(m["text"])),
        })
    sources.sort(key=lambda s: -(s["words"] or 0))

    attack = raw.get("attackDetected") or {}
    return {
        "provider": "winston",
        "uniqueness": uniqueness,
        "vendor_score": res.get("score"),
        "total_words": total_words,
        "plagiarised_words": plag_words,
        "identical_words": res.get("identicalWordCounts"),
        "similar_words": res.get("similarWordCounts"),
        "source_count": res.get("sourceCounts") or len(sources),
        "sources": sources,
        # invisible-character artefacts. Also the detector for the de-watermarking work.
        "hidden_characters": {"zero_width_space": bool(attack.get("zero_width_space")),
                              "homoglyph": bool(attack.get("homoglyph_attack"))},
        "citations": raw.get("citations") or [],
        "credits_used": raw.get("credits_used"),
        "credits_remaining": raw.get("credits_remaining"),
        "scan": raw.get("scanInformation") or {},
    }


def merge(parts: list[dict]) -> dict:
    """Combine several normalised chunk results into one, for an over-long article."""
    if len(parts) == 1:
        return parts[0]
    total = sum(p["total_words"] or 0 for p in parts)
    plag = sum(p["plagiarised_words"] or 0 for p in parts)
    by_url: dict[str, dict] = {}
    for p in parts:
        for s in p["sources"]:
            cur = by_url.setdefault(s["url"], {**s, "words": 0, "matches": []})
            cur["words"] = (cur["words"] or 0) + (s["words"] or 0)
            cur["matches"] += s["matches"]
    return {
        **parts[0],
        "uniqueness": round(100 - (plag / total * 100), 2) if total else None,
        "vendor_score": None,          # not additive across chunks
        "total_words": total,
        "plagiarised_words": plag,
        "identical_words": sum(p.get("identical_words") or 0 for p in parts),
        "similar_words": sum(p.get("similar_words") or 0 for p in parts),
        "sources": sorted(by_url.values(), key=lambda s: -(s["words"] or 0)),
        "source_count": len(by_url),
        "hidden_characters": {
            k: any(p["hidden_characters"].get(k) for p in parts)
            for k in ("zero_width_space", "homoglyph")},
        "credits_used": sum(p.get("credits_used") or 0 for p in parts),
        "credits_remaining": parts[-1].get("credits_remaining"),
        "chunks": len(parts),
    }
