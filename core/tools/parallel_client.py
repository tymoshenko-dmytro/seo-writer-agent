"""parallel.ai client wrapper for the Deep Researcher.

Verified against the official OpenAPI spec (docs.parallel.ai):
  - base: https://api.parallel.ai, auth header: x-api-key
  - Search API: cheap source gathering (url + excerpts)
  - Task API: structured deep research with per-field citations (`basis`)

Uses the official `parallel-web` SDK (client.search / client.task_run.create / .result).
Every call is recorded in the cost ledger (core/usage.py).
"""
from __future__ import annotations

import os

from .. import usage


class ParallelClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("PARALLEL_API_KEY")
        self._client = None
        self.last_run_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def client(self):
        if self._client is None:
            from parallel import Parallel
            self._client = Parallel(api_key=self.api_key)
        return self._client

    def search(
        self,
        objective: str,
        queries: list[str],
        max_results: int = 10,
        after_date: str | None = None,
        include_domains: list[str] | None = None,
    ) -> list[dict]:
        """Synchronous web search -> [{url, title, publish_date, excerpts}].

        `include_domains` restricts results to those sites (a vendor's own pages first).
        """
        advanced: dict = {"max_results": max_results}
        policy: dict = {}
        if after_date:
            policy["after_date"] = after_date
        if include_domains:
            policy["include_domains"] = list(include_domains)
        if policy:
            advanced["source_policy"] = policy
        res = self.client.search(
            objective=objective, search_queries=queries, advanced_settings=advanced
        )
        units = {"requests": 1}
        for u in getattr(res, "usage", None) or []:        # [{name, count}] as the API reports it
            name, count = getattr(u, "name", None), getattr(u, "count", None)
            if name and isinstance(count, (int, float)):
                units[str(name)] = count
        usage.record("parallel", "search", units)
        out = []
        for r in getattr(res, "results", []) or []:
            out.append(
                {
                    "url": getattr(r, "url", None),
                    "title": getattr(r, "title", None),
                    "publish_date": getattr(r, "publish_date", None),
                    "excerpts": list(getattr(r, "excerpts", []) or []),
                }
            )
        return out

    def deep_research(self, objective: str, json_schema: dict, processor: str = "core", timeout: int = 1800) -> dict:
        """ONE Task API run answering the whole objective (all questions at once).

        Verified API: create(input, processor, task_spec={output_schema:{type:'json',json_schema:...}}) -> result(run_id).
        Returns {"content": <structured JSON>,
                 "basis": [{field, confidence, reasoning, citations:[{url, title, excerpts}]}],
                 "run_id": str}.
        `excerpts` (source text the answer rests on) and `reasoning` are kept: without them the
        pipeline only ever saw Parallel's one-line extraction, never what a source actually says.
        The SDK notes that only some processors return excerpts, so both may be empty.
        """
        run = self.client.task_run.create(
            input=objective,
            processor=processor,
            task_spec={"output_schema": {"type": "json", "json_schema": json_schema}},
        )
        # billed per run by processor; recorded at creation, with the run id, so a run that
        # times out on our side is still on the bill and can be looked up
        self.last_run_id = run.run_id
        usage.record("parallel", f"task:{processor}", {"runs": 1}, note=f"run {run.run_id}")
        res = self.client.task_run.result(run.run_id, api_timeout=timeout)
        out = res.output
        basis = []
        for b in (getattr(out, "basis", None) or []):
            basis.append({
                "field": getattr(b, "field", None),
                "confidence": getattr(b, "confidence", None),
                "reasoning": getattr(b, "reasoning", None),
                "citations": [{"url": getattr(cc, "url", None), "title": getattr(cc, "title", None),
                               "excerpts": list(getattr(cc, "excerpts", None) or [])}
                              for cc in (getattr(b, "citations", []) or [])],
            })
        return {"content": getattr(out, "content", None), "basis": basis, "run_id": run.run_id}
