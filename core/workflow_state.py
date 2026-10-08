"""Pipeline state: one topic -> one article. Product-agnostic.

This is the LangGraph state object. Nodes mutate it and return it; LangGraph
returns a dict snapshot from `.invoke()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ArticleState:
    # --- input ---
    topic: str
    slug: str = ""
    project: str = ""                 # the project profile slug (set by generate_article)

    # --- run options (resolved from project defaults, overridable per run) ---
    use_ahrefs: bool = True
    target_words: int = 0             # 0 = auto; the Brief agent sets it from keyword + SERP research
    processor: str = "core"           # parallel.ai processor for deep research

    # --- status ---
    status: str = "pending"
    current_agent: str = ""
    error: str | None = None

    # --- artifacts (held inline; written to disk at finalize) ---
    keyword_research: dict = field(default_factory=dict)
    brief: dict = field(default_factory=dict)
    research: dict = field(default_factory=dict)       # facts + citations from parallel.ai
    draft_markdown: str = ""
    frontmatter: dict = field(default_factory=dict)    # title, description, slug, tags
    visuals: list = field(default_factory=list)         # visual manifest
    structure: dict = field(default_factory=dict)       # measured structure (validators.analyze_structure)
    keywords: dict = field(default_factory=dict)        # keyword coverage report (core/keyword_report.py)
    uniqueness: dict = field(default_factory=dict)      # Winston uniqueness check (core/plagiarism_report.py)
    qa_rounds: int = 0                                   # the review's reads so far (1 review = N reads)
    qa_state: dict = field(default_factory=dict)         # what the run records of its prompts and texts edited in the Studio
    picture_check: dict = field(default_factory=dict)    # the review's look at the pictures (agents/editor._picture_findings)

    # --- output ---
    output_dir: str = ""
    final_path: str = ""

    # --- transparency log (shown in CLI/UI) ---
    log: list = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.log.append(msg)
