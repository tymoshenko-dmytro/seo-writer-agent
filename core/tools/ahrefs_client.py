"""
Ahrefs MCP Client - JSON-RPC implementation
Connects to Ahrefs remote MCP server via HTTP.
"""

import os
import httpx
import time
import json
from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class KeywordData:
    """Structured keyword research data from Ahrefs."""
    keyword: str
    search_volume: int
    keyword_difficulty: int
    cpc: float
    parent_topic: Optional[str] = None
    clicks: Optional[int] = None
    global_volume: Optional[int] = None
    intent: Optional[str] = None
    serp_features: list = field(default_factory=list)
    related_keywords: list = field(default_factory=list)
    questions: list = field(default_factory=list)


@dataclass 
class RelatedKeyword:
    """Related keyword with metrics."""
    keyword: str
    search_volume: int
    difficulty: int
    cpc: float


def _record_units(tool: str, costs: dict) -> None:
    """One Ahrefs call's API units, as the server reports them (the actual figure after its
    cache; a zero-volume keyword costs nothing)."""
    from .. import usage

    try:
        units = int(costs.get("units-cost-total-actual", costs.get("units-cost-total", 0)) or 0)
        usage.record("ahrefs", tool, {"units": units, "rows": int(costs.get("rows") or 0)},
                     agent="keyword_researcher")
    except (TypeError, ValueError):
        pass


class AhrefsClient:
    """
    Ahrefs MCP Client (JSON-RPC over HTTP).
    Uses remote endpoint: https://api.ahrefs.com/mcp/mcp
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("AHREFS_API_KEY")
        self.base_url = "https://api.ahrefs.com/mcp/mcp"
        
        if not self.api_key:
            raise ValueError("AHREFS_API_KEY not set")
            
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
    def _rpc_call(self, method: str, params: dict = None, retries: int = 3) -> dict:
        """
        Execute JSON-RPC call with retry logic and session support.
        Create a fresh session for each major operation to ensure clean state,
        or retry within session.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000), # Unique ID
            "method": method,
            "params": params or {}
        }
        
        # Use a new client/session for independent operations to avoid state issues
        # But for 'initialize' then 'call tool', we need same session IF server requires it.
        # Based on testing, simple retries work.
        
        last_error = None
        
        for i in range(retries):
            try:
                with httpx.Client(timeout=30) as client:
                    # If we need to initialize first (MCP usually requires init before tools)
                    # But if we treat each call as stateless (which HTTP often is), 
                    # we might need to INIT every time? 
                    # Let's try to just call the method first (assuming stateless or persistent session via headers if needed)
                    # Actually, MCP standard says Initialize must be first.
                    # Since we are using HTTP POST, we likely need to handshake first in the same session.
                    
                    # 1. Initialize
                    init_payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05", 
                            "capabilities": {}, 
                            "clientInfo": {"name": "py-client", "version": "1.0"}
                        }
                    }
                    
                    resp = client.post(self.base_url, headers=self.headers, json=init_payload)
                    
                    if resp.status_code != 200:
                        # Retry initialization
                        time.sleep(1)
                        continue
                        
                    # 2. Call actual method (using same client to keep cookies)
                    resp = client.post(self.base_url, headers=self.headers, json=payload)
                    
                    if resp.status_code == 200:
                        return resp.json()
                    
                    # If 405 or other error, retry
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    time.sleep(1)
                    
            except Exception as e:
                last_error = str(e)
                time.sleep(1)
                
        raise Exception(f"RPC call failed after {retries} retries: {last_error}")

    def call_tool(self, tool_name: str, tool_args: dict) -> Any:
        """Call a specific MCP tool."""
        res = self._rpc_call("tools/call", {
            "name": tool_name,
            "arguments": tool_args
        })
        
        if "error" in res:
            raise Exception(f"MCP Error: {res['error']}")
            
        if "result" in res and "content" in res["result"]:
            # Content is a list of {type: 'text', text: '...'}: the data, and an `apiUsageCosts`
            # block saying what the call cost in API units, which goes to the cost ledger
            payload, found = None, False
            for item in res["result"]["content"]:
                if item.get("type") != "text":
                    continue
                try:
                    val = json.loads(item["text"])
                except json.JSONDecodeError:
                    val = item["text"]
                if isinstance(val, dict) and "apiUsageCosts" in val:
                    _record_units(tool_name, val.get("apiUsageCosts") or {})
                    val = {k: v for k, v in val.items() if k != "apiUsageCosts"}
                    if not val:
                        continue
                if not found:
                    payload, found = val, True
            if found:
                return payload

        return res

    def get_keyword_metrics(
        self, 
        keyword: str,
        country: str = "us"
    ) -> tuple[KeywordData | None, str | None]:
        """Get keyword metrics using Ahrefs MCP."""
        try:
            # Tool: keywords-explorer-overview
            # Params: keywords, country, select
            # Removed 'main_topic' as it's not supported
            data = self.call_tool("keywords-explorer-overview", {
                "keywords": keyword,
                "country": country,
                "select": "keyword,volume,difficulty,cpc,clicks,global_volume,parent_topic,serp_features"
            })
            
            # API returns list of keywords
            if isinstance(data, list) and len(data) > 0:
                kw = data[0]
                return KeywordData(
                    keyword=kw.get("keyword", keyword),
                    search_volume=kw.get("volume") or 0,
                    keyword_difficulty=kw.get("difficulty") or 0,
                    cpc=float(kw.get("cpc") or 0.0),
                    parent_topic=kw.get("parent_topic"),
                    clicks=kw.get("clicks"),
                    global_volume=kw.get("global_volume"),
                    serp_features=kw.get("serp_features") or []
                ), None
            elif isinstance(data, dict) and "keywords" in data: # Handle raw response structure if different
                 keywords = data["keywords"]
                 if keywords:
                     kw = keywords[0]
                     return KeywordData(
                        keyword=kw.get("keyword", keyword),
                        search_volume=kw.get("volume") or 0,
                        keyword_difficulty=kw.get("difficulty") or 0,
                        cpc=float(kw.get("cpc") or 0.0),
                        parent_topic=kw.get("parent_topic")
                    ), None
            
            return None, f"No data found for keyword: {keyword}"
            
        except Exception as e:
            return None, f"Ahrefs MCP error: {str(e)}"

    # ---- keyword ideas ----------------------------------------------------
    #
    # TWO LANDMINES IN THE MCP SERVER, measured 2026-09-04 after a live run produced
    # "Pool: 0 keywords" and the researcher picked a 100-volume variant over the
    # 1 100-volume head term it had been handed:
    #   1. `order_by` is REJECTED by keywords-explorer/matching-terms and /related-terms.
    #      Every call carrying it fails with a bare 500, which this client turned into an
    #      empty list and a swallowed error string. Sort here instead.
    #   2. On matching-terms, asking for `difficulty` breaks once the response passes
    #      ~12 rows (12 OK, 16 fails) while `keyword,volume` scales to 200 rows. A row
    #      Ahrefs has no KD for appears to take the whole response down with it.
    # So ideas are pulled wide and cheap (keyword+volume, ~1 unit/row), sorted here, and
    # only the shortlist is priced through overview in small batches — which is also the
    # cheaper shape, since every volume-class field bills 10 units per row.
    _PRICE_BATCH = 8

    def _price(self, keywords: list[str], country: str) -> dict[str, dict]:
        """difficulty/cpc for a keyword shortlist, in small batches.

        A batch holding a keyword Ahrefs has no record of fails as a whole, so a failed
        batch is split and retried; a keyword that still fails is left unpriced rather
        than costing the caller everything else in the batch.
        """
        out: dict[str, dict] = {}

        def run(batch: list[str]) -> None:
            if not batch:
                return
            try:
                data = self.call_tool("keywords-explorer-overview", {
                    "keywords": ",".join(batch), "country": country,
                    "select": "keyword,volume,difficulty,cpc",
                })
                for row in self._rows(data):
                    if kw := row.get("keyword"):
                        out[str(kw).strip().lower()] = row
            except Exception:  # noqa: BLE001 — one dead keyword must not cost the batch
                if len(batch) > 1:
                    mid = len(batch) // 2
                    run(batch[:mid])
                    run(batch[mid:])

        for i in range(0, len(keywords), self._PRICE_BATCH):
            run(keywords[i:i + self._PRICE_BATCH])
        return out

    def get_related_keywords(
        self,
        keyword: str,
        country: str = "us",
        limit: int = 50
    ) -> tuple[list[RelatedKeyword], str | None]:
        """Keyword ideas for a seed, with volume and KD. See the landmine note above."""
        try:
            data = self.call_tool("keywords-explorer-matching-terms", {
                "keywords": keyword,
                "country": country,
                "limit": max(limit * 4, 100),   # wide net: these rows cost ~1 unit each
                "select": "keyword,volume",
            })

            seed = keyword.strip().lower()
            rows = [r for r in self._rows(data)
                    if r.get("keyword") and (r.get("volume") or 0) > 0
                    and str(r["keyword"]).strip().lower() != seed]
            rows.sort(key=lambda r: r.get("volume") or 0, reverse=True)
            rows = rows[:limit]

            priced = self._price([str(r["keyword"]) for r in rows], country)
            related = [
                RelatedKeyword(
                    keyword=str(r["keyword"]),
                    search_volume=r.get("volume") or 0,
                    difficulty=(priced.get(str(r["keyword"]).strip().lower(), {})
                                .get("difficulty") or 0),
                    cpc=float((priced.get(str(r["keyword"]).strip().lower(), {})
                               .get("cpc") or 0.0)),
                )
                for r in rows
            ]
            return related, None

        except Exception as e:
            return [], f"Ahrefs MCP error: {str(e)}"

    def get_questions(
        self,
        keyword: str,
        country: str = "us",
        limit: int = 20
    ) -> tuple[list[str], str | None]:
        """
        Get questions. 
        Note: Since specific questions tool is missing, use matching terms with question modifiers.
        """
        try:
            # Alternative: use matching terms but filter myself, or look for 'questions' tool later.
            # Only 'keywords-explorer-matching-terms' is available. 
            # I'll rely on SEOResearcherAgent to generate questions based on logic if Ahrefs fails,
            # OR I'll try to fetch terms containing "how,what,why".
            
            # Simple fallback: return empty list and let Agent 1 hallucinate questions based on related keywords?
            # NO, user forbade hallucinations.
            # But questions are less critical than metrics.
            
            # Let's try to pass "how {keyword}, what {keyword}" etc?
            # No, 'keywords' param is seed.
            
            # Let's just return empty for now, or try 'matching terms' and filter for '?' or question words?
            data = self.call_tool("keywords-explorer-matching-terms", {
                "keywords": keyword,
                "country": country,
                "limit": 200,
                "select": "keyword,volume",
            })

            questions = []
            items = self._rows(data)
            items.sort(key=lambda r: r.get("volume") or 0, reverse=True)

            q_words = ["how", "what", "why", "where", "when", "who", "can", "does"]
            for item in items:
                # Filter out zero-volume questions
                volume = item.get("volume") or 0
                if volume == 0:
                    continue

                kw = item.get("keyword", "").lower()
                if any(kw.startswith(w + " ") for w in q_words) or "?" in kw:
                    questions.append(kw)
                    if len(questions) >= limit:
                        break
                        
            return questions, None
            
        except Exception as e:
            return [], f"Ahrefs MCP error: {str(e)}"

    def get_serp_competitors(self, keyword: str, country: str = "us", limit: int = 3) -> tuple[list[str], str | None]:
        """Get top organic competitor URLs from SERP."""
        try:
            # Tool: serp-overview
            data = self.call_tool("serp-overview", {
                "keyword": keyword,
                "country": country,
                "select": "url,type,position",
                "top_positions": 10 
            })
            
            # `type` is a LIST of SERP-feature tags (e.g. ["ai_overview_sitelink","image_th"]),
            # not the string "organic". Collect distinct result URLs, skipping paid/ads.
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("positions") or data.get("serp") or next((v for v in data.values() if isinstance(v, list)), [])
            else:
                items = []

            urls, seen = [], set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if not url or url in seen:
                    continue
                types = item.get("type") or []
                if isinstance(types, str):
                    types = [types]
                if any(("ad" in str(t).lower()) or ("paid" in str(t).lower()) for t in types):
                    continue  # skip paid placements
                seen.add(url)
                urls.append(url)
                if len(urls) >= limit:
                    break

            return urls, None
        except Exception as e:
            return [], f"SERP Error: {str(e)}"

    def get_url_keywords(self, url: str, country: str = "us", limit: int = 10) -> tuple[list[RelatedKeyword], str | None]:
        """Get organic keywords for a specific URL."""
        try:
            # Tool: site-explorer-organic-keywords
            # We use mode="exact" to get keywords for the exact page
            data = self.call_tool("site-explorer-organic-keywords", {
                "target": url,
                "mode": "exact",
                "country": country,
                "limit": limit,
                "select": "keyword,volume,difficulty,cpc",
            })
            
            keywords = []
            items = data if isinstance(data, list) else data.get("keywords", [])
            
            for item in items:
                if item.get("volume", 0) > 0:
                    keywords.append(RelatedKeyword(
                        keyword=item.get("keyword", ""),
                        search_volume=item.get("volume", 0),
                        difficulty=item.get("difficulty", 0),
                        cpc=float(item.get("cpc", 0))
                    ))
            
            return keywords, None
        except Exception as e:
            return [], f"Site Explorer Error: {str(e)}"

    # ---- SERP -------------------------------------------------------------
    #
    # serp-overview returns one row per SERP position PER FEATURE, and ONLY rows whose
    # `type` contains "organic" carry metrics. An ai_overview_sitelink or a People-Also-Ask
    # row for the SAME url comes back with every metric null - which is why the old
    # dedupe-by-url logic reported "DR None" for a whole SERP: it kept whichever row came
    # first, and feature rows sort first. So the two concerns are now split:
    #   serp_probe()   - no billable columns; used to pick WHICH keyword's SERP to study
    #   serp_details() - the metric columns, paid for once, on the winner only
    # (measured: 5 units/row for the probe against 35 with the metric columns.)
    _BILLABLE = "domain_rating,url_rating,backlinks,refdomains,traffic,keywords,top_keyword,top_keyword_volume"

    @staticmethod
    def _kinds(row: dict) -> list[str]:
        t = row.get("type") or []
        return [str(x) for x in (t if isinstance(t, list) else [t])]

    @staticmethod
    def _rows(data) -> list[dict]:
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            items = data.get("positions") or next((v for v in data.values() if isinstance(v, list)), [])
            return [r for r in items if isinstance(r, dict)]
        return []

    def serp_probe(self, keyword: str, country: str = "us", top: int = 20):
        """SERP SHAPE only - positions, feature types, urls, Ahrefs' page_type. No metrics.

        Cheap enough to run on several candidate keywords, so the expensive metric columns
        are bought once, for the SERP we actually keep.
        Returns ({"rows":[...], "features":{kind:n}, "organic":[{position,url,title,page_type}]}, err).
        """
        try:
            data = self.call_tool("serp-overview", {
                "keyword": keyword, "country": country,
                "select": "position,type,url,title,page_type", "top_positions": top,
            })
            rows, features, organic, seen = self._rows(data), {}, [], set()
            for r in rows:
                kinds = self._kinds(r)
                if any(k.startswith("paid") for k in kinds):
                    continue
                for k in kinds:
                    features[k] = features.get(k, 0) + 1
                url = r.get("url")
                if "organic" in kinds and url and url not in seen:
                    seen.add(url)
                    organic.append({"position": r.get("position"), "url": url,
                                    "title": r.get("title"), "page_type": r.get("page_type")})
            return {"rows": rows, "features": features, "organic": organic}, None
        except Exception as e:
            return {"rows": [], "features": {}, "organic": []}, f"SERP probe error: {str(e)}"

    def serp_details(self, keyword: str, country: str = "us", top: int = 20, probe: dict | None = None):
        """Full SERP picture for ONE keyword: organic results WITH competitiveness metrics,
        plus a count of the SERP features surrounding them.

        `backlinks`/`refdomains` here are PAGE-level ("links pointing to a search result"),
        which is the "direct links to the article" figure the SEO team selects prototype
        competitors by - not the domain's totals.
        Pass `probe` to reuse a serp_probe() result the caller already paid for.
        Returns ({organic:[...], features:{...}, paa_count:int, feature_only:[...]}, err).
        """
        perr = None
        if probe is None:
            probe, perr = self.serp_probe(keyword, country=country, top=top)
        features = probe["features"]
        try:
            data = self.call_tool("serp-overview", {
                "keyword": keyword, "country": country, "type": "organic",
                "select": "position,url,title," + self._BILLABLE, "top_positions": top,
            })
        except Exception as e:
            return {"organic": [], "features": features, "paa_count": features.get("question", 0),
                    "feature_only": []}, f"SERP error: {str(e)}"

        ptypes = {o["url"]: o.get("page_type") for o in probe["organic"]}
        organic, seen = [], set()
        for r in self._rows(data):
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            organic.append({
                "position": r.get("position"), "url": url, "title": r.get("title"),
                "dr": r.get("domain_rating"), "ur": r.get("url_rating"),
                "backlinks": r.get("backlinks"), "refdomains": r.get("refdomains"),
                "traffic": r.get("traffic"), "ranks_for": r.get("keywords"),
                "page_type": r.get("page_type") or ptypes.get(url),
                "top_keyword": r.get("top_keyword"),
                "top_keyword_volume": r.get("top_keyword_volume"),
            })
        # Pages Google surfaces ONLY through a feature (an AI-overview pick, a PAA answer)
        # never appear in the organic list, yet they are real competitors for the query.
        feature_only, fseen = [], set()
        for r in probe["rows"]:
            url = r.get("url")
            kinds = self._kinds(r)
            if not url or url in seen or url in fseen or "organic" in kinds:
                continue
            fseen.add(url)
            feature_only.append({"url": url, "title": r.get("title"), "types": kinds})
        return {"organic": organic, "features": features,
                "paa_count": features.get("question", 0), "feature_only": feature_only}, perr

    def related_terms(self, keyword: str, country: str = "us", terms: str = "also_talk_about",
                      view_for: str = "top_10", limit: int = 40):
        """LSI vocabulary via `keywords-explorer-related-terms`.

        `also_talk_about` = terms the top-ranking pages frequently MENTION - the classic LSI
        signal: the vocabulary a page needs in order to read as on-topic, whether or not
        anyone searches for it. `also_rank_for` = terms those same pages also rank for
        (semantic siblings). Returns ([{keyword, volume, difficulty}], err).
        """
        try:
            data = self.call_tool("keywords-explorer-related-terms", {
                "keywords": keyword, "country": country, "terms": terms, "view_for": view_for,
                "select": "keyword,volume,difficulty", "limit": limit,
            })
            items = data if isinstance(data, list) else (
                (data.get("keywords") or next((v for v in data.values() if isinstance(v, list)), []))
                if isinstance(data, dict) else [])
            return [{"keyword": x.get("keyword"), "volume": x.get("volume"),
                     "difficulty": x.get("difficulty")}
                    for x in items if isinstance(x, dict) and x.get("keyword")], None
        except Exception as e:
            return [], f"Related-terms error: {str(e)}"

    def search_suggestions(self, keyword: str, country: str = "us", limit: int = 25):
        """Autocomplete-style suggestions (long-tail + question-like queries). Returns ([{keyword,volume}], err)."""
        try:
            data = self.call_tool("keywords-explorer-search-suggestions", {
                "keywords": keyword, "country": country, "select": "keyword,volume", "limit": limit,
            })
            items = data if isinstance(data, list) else (
                (data.get("keywords") or next((v for v in data.values() if isinstance(v, list)), [])) if isinstance(data, dict) else []
            )
            return [{"keyword": x.get("keyword"), "volume": x.get("volume")} for x in items if isinstance(x, dict) and x.get("keyword")], None
        except Exception as e:
            return [], f"Suggestions Error: {str(e)}"

    def full_keyword_research(
        self,
        keyword: str,
        country: str = "us"
    ) -> dict:
        """
        Run full research workflow with Deep Research:
        1. Direct Keyword Metrics & Related Terms
        2. Parent Topic Research (ALWAYS runs if parent exists)
        3. SERP Competitor Analysis (Top 3 competitors -> their top keywords)
        """
        errors = []
        api_calls = 0
        
        # 1. Metrics
        api_calls += 1
        main_kw, err = self.get_keyword_metrics(keyword, country)
        if err: errors.append(err)
        
        # 2. Related (Direct)
        api_calls += 1
        related, err = self.get_related_keywords(keyword, country)
        if err: errors.append(err)
        
        # 3. Questions (Direct)
        questions, err = self.get_questions(keyword, country)
        
        # === DEEP RESEARCH 1: PARENT TOPIC STRATEGY ===
        # ALWAYS check Parent Topic if it's different/valid
        if main_kw and main_kw.parent_topic and (main_kw.parent_topic.lower() != keyword.lower()):
            parent = main_kw.parent_topic
            
            # Fetch related for Parent Subject
            api_calls += 1
            parent_related, p_err = self.get_related_keywords(parent, country, limit=30)
            if not p_err:
                existing_kws = {r.keyword.lower() for r in related}
                for pr in parent_related:
                    if pr.keyword.lower() not in existing_kws and pr.keyword.lower() != keyword.lower():
                        # Tag as parent topic derived? No currently structure doesn't support tags, just append
                        related.append(pr)
                        existing_kws.add(pr.keyword.lower())
            
            # Fetch questions for Parent Subject
            api_calls += 1
            parent_questions, pq_err = self.get_questions(parent, country, limit=10)
            if not pq_err:
                existing_qs = set(questions)
                for pq in parent_questions:
                    if pq not in existing_qs:
                        questions.append(pq)

        # === DEEP RESEARCH 2: SERP COMPETITOR STRATEGY ===
        # Get top ranking pages and extract their keywords
        api_calls += 1
        competitor_urls, serp_err = self.get_serp_competitors(keyword, country, limit=3)
        
        if competitor_urls:
            for url in competitor_urls:
                api_calls += 1
                comp_kws, comp_err = self.get_url_keywords(url, country, limit=10)
                if not comp_err:
                    existing_kws = {r.keyword.lower() for r in related}
                    for ck in comp_kws:
                        if ck.keyword.lower() not in existing_kws and ck.keyword.lower() != keyword.lower():
                            related.append(ck)
                            existing_kws.add(ck.keyword.lower())

        return {
            "success": main_kw is not None and main_kw.search_volume > 0,
            "main_keyword": main_kw,
            "related_keywords": sorted(related, key=lambda x: x.search_volume, reverse=True), # Sort combined list
            "questions": questions,
            "serp_competitors": competitor_urls or [],
            "errors": errors,
            "api_calls": api_calls
        }
