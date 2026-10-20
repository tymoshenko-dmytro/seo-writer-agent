"""LangGraph orchestrator: topic -> researched, written, illustrated, reviewed article.

Graph:
    init -> [keyword?] -> brief -> deep -> write -> visual -> finalize
    init --cached brief + research (reuse_from)--> write
    deep --nothing to write from--> abort

`finalize` writes the article, its keyword report and its uniqueness check. Then, still under
the run's lock, the article's first version is kept and ONE review runs on it in the QA model
(core/qa_review: the panel's reads, the picture check, the code checks; the safe changes made in
the working version, each a version of its own; everything else waits for the person). There is
no verdict and no READY: the review's findings, the person's acceptance and WordPress are three
facts kept apart.

Connections are intentionally explicit here (transparent wiring). Each node calls
one agent; agents live in core/agents/ with prompts in core/prompts/.
"""
from __future__ import annotations

import os
from pathlib import Path

from langgraph.graph import StateGraph, END

from .config import Project, load_core_config, load_project, runtime_config
from . import store, usage
from .store import output_root, slugify
from .workflow_state import ArticleState
from .agents import (
    KeywordResearcher,
    BriefGenerator,
    DeepResearcher,
    Copywriter,
    VisualDirector,
)
from .qa_migration import RUN_SCHEMA
from .qa_review import check_qa_settings
from .validators import validate_article


def build_workflow(project: Project, config: dict, status_callback=None):
    config = runtime_config(config, project)     # the routers read the profile's settings too
    kw = KeywordResearcher(project, config)
    brief = BriefGenerator(project, config)
    deep = DeepResearcher(project, config)
    writer = Copywriter(project, config)
    visual = VisualDirector(project, config)

    def cb(msg: str):
        if status_callback:
            status_callback(msg)

    def init(state: ArticleState):
        # generate_article reserved the folder; a direct graph caller (tests) gets one here
        if not state.output_dir:
            art = store.Repository(config=config).reserve(state.slug or slugify(state.topic))
            state.slug, state.output_dir = art.slug, str(art.dir)
        state.status = "running"
        state.note(f"[init] topic='{state.topic}' slug='{state.slug}' use_ahrefs={state.use_ahrefs}")
        cb("Init")
        return state

    def n_keyword(state):
        cb("Keyword research")
        return kw.run(state)

    def n_brief(state):
        cb("Brief")
        return brief.run(state)

    def n_deep(state):
        cb("Deep research")
        return deep.run(state)

    def n_write(state):
        cb("Writing")
        return writer.run(state)

    def n_visual(state):
        cb("Visuals")
        return visual.run(state)

    def finalize(state):
        cb("Finalize")
        out = Path(state.output_dir)
        fm = state.frontmatter or {"title": state.topic, "slug": state.slug}

        # The focus keyphrase Yoast scores against ships in the frontmatter, right after the
        # slug it was derived from: visible to whoever opens the article, editable by hand,
        # and preserved verbatim by the standalone polish flows (they write the frontmatter
        # block back untouched). The publisher reads it from here.
        try:
            primary = str((state.keyword_research or {}).get("primary_keyword") or "").strip()
            if primary and not fm.get("focus_keyword"):
                items = list(fm.items())
                at = next((i for i, (k, _) in enumerate(items) if k == "slug"), len(items) - 1)
                fm = dict(items[:at + 1] + [("focus_keyword", primary)] + items[at + 1:])
                state.frontmatter = fm
        except Exception as e:  # noqa: BLE001 — a keyphrase is never worth losing the article
            state.note(f"[yoast] focus keyword not written to the frontmatter: {e}")

        store.atomic_write_text(out / "article.md",
                                store.compose_article(store.render_frontmatter(fm), state.draft_markdown))

        from .project_texts import active as _edited_texts
        from .prompt_overrides import active as _edited_prompts

        state.qa_state = {**(state.qa_state or {}),
                          "project_edits": _edited_texts(output_root(config)),
                          # which agents ran on a prompt edited in the Studio (the run's report)
                          "prompt_edits": _edited_prompts(output_root(config))}

        # Keyword coverage: a script, not an agent — no model call, so it always runs and never
        # blocks. Computed from state rather than via keyword_report.build(), which reads
        # _run.json and would find it missing or one run stale at this point. A failure here
        # must never cost us the article, hence the broad catch.
        try:
            from .keyword_report import analyze, thresholds_from, write_report

            state.keywords = analyze(state.draft_markdown, state.keyword_research, state.brief,
                                     thresholds_from(project.seo))
            state.keywords["slug"] = state.slug
            write_report(out, state.keywords, state.topic)
            kw = state.keywords
            state.note(f"[keywords] {kw['verdict']} — {kw['failed']} failed, {kw['warned']} to "
                       f"review; primary '{(kw.get('primary') or {}).get('keyword')}' x"
                       f"{(kw.get('primary') or {}).get('total')}")
        except Exception as e:  # noqa: BLE001
            state.note(f"[keywords] report failed: {e}")

        # Uniqueness. A real network call taking tens of seconds, so it runs ONCE here on the
        # finished text — not inside the revision loop, which would multiply the credit cost by
        # the number of rounds. Skipped silently without a key so a run never depends on it.
        try:
            from .plagiarism_report import build as build_uniqueness
            from .plagiarism_report import settings_from as plag_settings

            cfg_plag = plag_settings(project.seo)
            if cfg_plag.get("run_on_finalize", True) and os.getenv("WINSTON_API_KEY"):
                cb("Uniqueness check")
                with usage.stage("uniqueness"):
                    state.uniqueness = build_uniqueness(out, project.seo)
                u = state.uniqueness
                state.note(f"[uniqueness] {u['verdict']} — {u['uniqueness']}% unique, "
                           f"{u['plagiarised_words']}/{u['total_words']} words matched across "
                           f"{u['source_count']} source(s), "
                           f"{len([p for p in u['passages'] if not p['cited']])} passage(s) to "
                           f"rewrite, {u['credits_used']} credits")
            else:
                state.note("[uniqueness] skipped (no WINSTON_API_KEY or disabled in settings)")
        except Exception as e:  # noqa: BLE001 — never lose the article over a third-party call
            state.note(f"[uniqueness] check failed: {e}")

        ok, problems = validate_article(state)
        state.status = "success" if ok else "needs_review"
        state.final_path = str(out / "article.md")
        state.note(f"[finalize] status={state.status} problems={problems} -> {state.final_path}")
        art = store.Article(out.parent, out.name)
        art.save_run({
            **_history(art),
            "topic": state.topic,
            "slug": state.slug,
            "status": state.status,
            # what this run cost; every call, including later edits, is in _usage.jsonl
            "cost": _run_cost(),
            "keyword_research": state.keyword_research,
            "brief": state.brief,
            "research": state.research,
            "visuals": state.visuals,
            "qa_state": state.qa_state,
            "keywords": state.keywords,
            "uniqueness": state.uniqueness,
            "log": state.log,
            # the QA model's record from the start (core/qa_migration.RUN_SCHEMA): the review
            # that follows the run fills it; there is no old checklist to keep as history
            "schema": RUN_SCHEMA,
            "review": {"reviews": [], "findings": [], "changes": []},
        })
        # Nothing is sent to WordPress when a run ends: a person's Accept (with the action
        # Settings > Publishing chooses) or a send does it (plan contract 5).
        return state

    # routing
    def _precondition(state) -> str | None:
        """What makes writing pointless. Returns the reason to abort, or None to continue.

        An article shipped APPROVED 91 on an EMPTY research corpus: deep research timed out, and
        the citation guard is written as `if has_facts and no citations` — so it disables itself
        exactly when the thing it protects is missing. Nothing asked whether there was anything
        to write from, and the run spent the writer, the visuals, the Editor and two fix passes
        producing a piece with no evidence under it.

        Writing is the expensive part, so failing before it is also the cheapest way to fail.
        Deliberately NOT blockers: `use_ahrefs=False` is a supported mode, and an empty SERP is
        a degradation (no measured format) rather than an absence of grounds.
        """
        b = state.brief or {}
        if not b.get("sections"):
            # a truncated/unparsed brief JSON slipped through once and a whole article was
            # written against sections=0 (no outline, no meta, no research questions) — the
            # writer, visuals, and six QA rounds all spent money on a run that was already dead
            return ("the brief has no sections (its JSON likely failed to parse or was "
                    "truncated) — nothing structured to write from")
        r = state.research or {}
        if not (r.get("key_statistics") or r.get("findings")):
            why = r.get("error")
            return ("the deep research corpus is empty"
                    + (f" ({why})" if why else "")
                    + " — there are no cited facts to write from")
        if state.use_ahrefs:
            kr = state.keyword_research or {}
            if not kr.get("primary_keyword"):
                return "keyword research produced no primary keyword"
            if not kr.get("related"):
                return "keyword research produced an empty keyword pool"
        return None

    def after_brief(state):
        # Routing only. A brief with no sections cannot be written from, and the deep research it
        # would start is the most expensive call of the run: stop here (RF-28)
        return "deep" if (state.brief or {}).get("sections") else "abort"

    def after_deep(state):
        # Routing only. A conditional-edge function's mutations are NOT persisted by LangGraph —
        # setting status here left the run reporting "running" with no error while the abort node
        # had already fired. Only nodes may change state.
        return "write" if _precondition(state) is None else "abort"

    def abort(state):
        """Record the failure without leaving anything that looks like a finished article.

        `article.md` is deliberately not written: it is what the dashboard lists and what the
        publish button acts on, so a half-grounded draft on disk is worse than no draft.
        """
        why = _precondition(state) or "a required input was missing"
        _progress(state, stopped=True)              # the node whose result did not pass
        state.status = "aborted"
        state.error = why
        state.note(f"[abort] {why}. Not writing the article — re-run the topic once the "
                   f"input is available.")
        cb(f"Aborted: {why}")
        out = Path(state.output_dir)
        art = store.Article(out.parent, out.name)
        art.save_run({
            **_history(art),
            "topic": state.topic, "slug": state.slug, "status": "aborted",
            "error": state.error, "cost": _run_cost(),
            "keyword_research": state.keyword_research, "brief": state.brief,
            "research": state.research, "log": state.log,
        })
        return state

    def after_init(state):
        # A re-run that already carries a brief and a research corpus skips straight to the
        # writer: Ahrefs and the deep-research task are the expensive, slow, and (when the
        # topic has not changed) unnecessary half of the pipeline. Used to test pipeline
        # changes against identical inputs — the only way to tell a fix from a different roll.
        if (state.brief or {}).get("sections") and (state.research or {}).get("findings"):
            state.note("[init] reusing the cached brief and research corpus — skipping "
                       "keyword research and deep research")
            return "write"
        return "keyword" if state.use_ahrefs else "brief"

    def _run_cost() -> dict:
        rec = usage.current()
        return usage.summarize(rec.events, rec.prices) if rec else {}

    # which part of the article each node's paid calls are for (the cost report's breakdown)
    stages = {"keyword": "keywords", "brief": "brief", "deep": "research", "write": "writing",
              "visual": "visuals"}

    def staged(name, fn):
        if name not in stages and name != "finalize":
            return fn

        def node(state):
            # the attempt's progress (`_progress.json`): the node running now, the nodes done;
            # a node that raises stays the one running, so the page says where the run failed
            _progress(state, stage=name)
            with usage.stage(stages.get(name, "other")):
                out = fn(state)
            _progress(state, stage=None, done=name)
            return out
        return node

    g = StateGraph(ArticleState)
    for name, fn in [
        ("init", init),
        ("keyword", n_keyword),
        ("brief", n_brief),
        ("deep", n_deep),
        ("write", n_write),
        ("visual", n_visual),
        ("finalize", finalize),
        ("abort", abort),
    ]:
        g.add_node(name, staged(name, fn))

    g.set_entry_point("init")
    g.add_conditional_edges("init", after_init,
                            {"keyword": "keyword", "brief": "brief", "write": "write"})
    g.add_edge("keyword", "brief")
    g.add_conditional_edges("brief", after_brief, {"deep": "deep", "abort": "abort"})
    g.add_conditional_edges("deep", after_deep, {"write": "write", "abort": "abort"})
    g.add_edge("write", "visual")
    g.add_edge("visual", "finalize")
    g.add_edge("finalize", END)
    g.add_edge("abort", END)
    return g.compile()


PROGRESS_FILE = "_progress.json"


def _history(art: store.Article) -> dict:
    """The earlier attempts' outcomes and this one's start, kept when a run writes its record."""
    try:
        run = art.run()
    except Exception:  # noqa: BLE001 — a record that does not parse has no history to keep
        return {}
    return {k: run[k] for k in ("attempts", "started_at") if run.get(k)}


def _progress(state, stage: str | None = None, done: str | None = None, start: dict | None = None,
              stopped: bool = False) -> None:
    """Write the attempt's progress next to the article: {"attempt", "stage", "done"}. `start`
    begins an attempt; `stopped` (the graph's abort) takes the last node done back as the one
    the run stopped at, since its result did not pass. Never raises: the page's timeline must
    not fail the run."""
    try:
        p = Path(state.output_dir) / PROGRESS_FILE
        cur = start if start is not None else (store.read_json_lenient(p, {}) or {})
        cur = {**cur, "stage": stage}
        if stopped and cur.get("done"):
            cur["stage"] = cur["done"].pop()
        if done and done not in cur.setdefault("done", []):
            cur["done"].append(done)
        store.write_json(p, cur)
    except Exception:  # noqa: BLE001
        pass


def generate_article(topic: str, status_callback=None, on_folder=None, **overrides) -> dict:
    """Run the full pipeline for one topic. Returns the final state dict snapshot.

    Every run gets a NEW folder (`<slug>`, else `<slug>-2`, …): a second run of the same topic
    used to write into the first one's folder, keeping its WordPress binding and stale reports,
    and a crash midway left a mix of both. The folder is locked for the whole run and carries a
    `running` record from the start, so the Studio can tell a live run from an interrupted one.

    `reuse_from="<slug>"` re-runs an existing topic from the WRITER, on that run's cached
    keyword research, brief and research corpus. Same inputs, new pipeline — the only honest
    way to attribute a difference to a code change rather than to a different sampling roll.
    The re-run writes to its OWN directory (`reuse_out`, default `<slug>--rerun-<timestamp>`,
    which must not exist) and copies what the project's own research sources pinned in the source
    folder, so the inputs really are the same. `reuse_in_place=True` regenerates the source folder on purpose, keeping its
    WordPress binding (`_wp.json`). `on_folder(slug)` is told the folder as soon as it exists (the
    Studio's job links to the article while it is being written).
    """
    from datetime import datetime, timezone

    config = load_core_config()
    project = load_project(core=config)
    check_qa_settings(runtime_config(config, project))   # core/qa_review: before anything is paid
    if overrides.get("country"):
        # this run's market (New article's options), over the Research setting
        project.seo = {**(project.seo or {}), "country": str(overrides["country"]).lower()}
    d = project.defaults
    repo = store.Repository(config=config)
    state = ArticleState(
        topic=topic,
        project=project.slug,
        use_ahrefs=overrides.get("use_ahrefs", d.get("use_ahrefs", True)),
        target_words=overrides.get("target_words", d.get("target_words", 0)),  # 0 = Brief decides from research
        processor=overrides.get("processor", d.get("deep_research_processor", "core")),
    )
    src_dir = None
    if reuse := overrides.get("reuse_from"):
        src_art = repo.get(reuse)
        src_dir = src_art.dir
        cached = src_art.run()
        state.topic = cached.get("topic") or topic
        state.keyword_research = cached.get("keyword_research") or {}
        state.brief = cached.get("brief") or {}
        state.research = cached.get("research") or {}
        state.target_words = (state.brief or {}).get("target_words") or state.target_words
        if not ((state.brief or {}).get("sections") and (state.research or {}).get("findings")):
            raise ValueError(f"{reuse} has no usable cached brief/research to reuse")

    # the folder, created only now that the inputs are known to be usable: a refused reuse must
    # not leave an empty directory behind (the Studio lists every folder as an article)
    if reuse and overrides.get("reuse_in_place"):
        art = src_art
    elif reuse:
        name = overrides.get("reuse_out") or f"{reuse}--rerun-{datetime.now():%Y%m%d-%H%M%S}"
        if not store.valid_slug(name) or name in (".", ".."):
            raise ValueError(f"reuse_out '{name}' must be a plain directory name")
        if (repo.root / name).resolve() == src_dir.resolve():
            raise ValueError(f"reuse_out '{name}' is the source directory - pass "
                             f"reuse_in_place=True to regenerate it in place")
        (repo.root / name).mkdir(parents=True, exist_ok=False)    # never an existing article
        art = store.Article(repo.root, name)
        import shutil

        # the reports of the stages this run reuses instead of running: without them the new
        # folder's trail started at the draft (webinar, ai-interpreter on prod, 2026-09-29)
        for name in ("01_keyword_research.md", "02_brief.md", "03_research.md"):
            if (src_dir / name).exists():
                shutil.copyfile(src_dir / name, art.path(name))
        # and what the project's own research sources kept in it (core/research_sources.py: a
        # pinned snapshot), so the inputs really are the same
        from .research_sources import copy_kept

        copy_kept(src_dir, art.dir, project)
    else:
        art = repo.reserve(slugify(topic))
    state.slug, state.output_dir = art.slug, str(art.dir)
    if on_folder:
        on_folder(art.slug)

    app = build_workflow(project, config, status_callback)
    with art.lock(holder=f"generation of '{state.topic[:60]}'"), \
            usage.recording(art.dir, "generation", label=state.topic, config=config):
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if art.path("article.md").is_file():
            # a run over an existing article (--in-place): the state it overwrites is a version
            from . import versions

            versions.ensure(art)
        # this attempt: running from now, over an earlier one's record (a Run again in place
        # keeps the record's cached inputs, and the earlier attempt's outcome as history)
        try:
            prev = art.run() if art.path("_run.json").exists() else {}
        except store.CorruptFile:
            prev = {}
        attempts = list(prev.get("attempts") or [])
        if prev.get("status"):
            attempts.append({k: prev[k] for k in ("status", "error", "started_at") if prev.get(k)})
        art.save_run({**{k: v for k, v in prev.items() if k != "error"}, "topic": state.topic, "slug": state.slug,
                      "status": "running", "started_at": started, **({"attempts": attempts[-10:]} if attempts else {})})
        reused = [n for n, have in (("keyword", state.keyword_research), ("brief", (state.brief or {}).get("sections")),
                                    ("deep", (state.research or {}).get("findings"))) if have] if src_dir else []
        _progress(state, start={"attempt": started, "done": reused})
        try:
            res = app.invoke(state, config={"recursion_limit": 40})
            if res.get("status") in ("success", "needs_review") and art.path("article.md").is_file():
                first_review(art, config, project, status_callback)
            return res
        except BaseException as e:
            # whatever was paid for so far stays on disk; the record says the run did not finish
            try:
                run = art.run()
            except store.CorruptFile:
                run = {}
            if run.get("status") in (None, "running"):
                art.save_run({**run, "topic": state.topic, "slug": state.slug, "status": "failed",
                              "error": f"{type(e).__name__}: {str(e)[:300]}", "started_at": started})
            raise


def first_review(art: store.Article, config: dict, project: Project, status_callback=None) -> dict | None:
    """After the run wrote the article, under its lock: the first version kept, then one review
    in the QA model with its safe changes (each a version). A review that fails leaves the
    article as written; the run's log says why, and *Review again* runs it."""
    from . import versions
    from .qa_review import review

    versions.record(art, "the pipeline", "Article written", "keywords, brief, research, writing, pictures",
                    kind="pipeline")
    if status_callback:
        status_callback("Review")
    try:
        from .agent_door import review_summary

        rep = review(art.slug, config=config, project=project)
        note = f"[review] {review_summary(art)}"
    except Exception as e:  # noqa: BLE001 — the article stands without its review; Review again runs it
        rep, note = None, f"[review] could not run: {type(e).__name__}: {str(e)[:300]}"
    try:
        run = art.run()
        run["log"] = list(run.get("log") or []) + [note]
        art.save_run(run)
    except store.CorruptFile:
        pass
    return rep
