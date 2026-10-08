#!/usr/bin/env python3
"""Make the sample article in samples/sample-article/ from a stand-in run (tests/standin.py): the
engine's own code end to end, every paid service answered locally, so it costs nothing.

    python bin/make_sample.py

Run it again after a change to what an article folder holds, and commit the result. The run's
working folder is temporary; its path is taken out of the records before they are copied.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "samples" / "sample-article"
LEAVE_OUT = {".lock", "_progress.json"}


def main() -> int:
    sys.path[:0] = [str(REPO), str(REPO / "tests")]
    tmp = Path(tempfile.mkdtemp(prefix="sample-"))
    project = tmp / "project"
    project.mkdir()
    shutil.copy(REPO / "project" / "chart_style.mplstyle", project / "chart_style.mplstyle")
    (project / "profile.yaml").write_text(
        'name: "Sample"\naudience: "people who write pages for the web"\n'
        'brand:\n  domains: ["example.com"]\nlinks:\n  site_sitemap: ""\nchart_style: "chart_style.mplstyle"\n',
        encoding="utf-8")
    for key in ("ANTHROPIC_API_KEY", "PARALLEL_API_KEY", "AHREFS_API_KEY", "SCRAPFLY_KEY", "WINSTON_API_KEY"):
        os.environ.pop(key, None)
    os.environ.update({"CONTENT_PROJECT_DIR": str(project), "CONTENT_OUTPUT_DIR": str(tmp / "out"),
                       "MPLBACKEND": "Agg", "OPENAI_API_KEY": "stand-in"})

    import pytest
    import standin

    with pytest.MonkeyPatch.context() as mp:
        standin.install(mp)
        from core.orchestrator import generate_article

        res = generate_article(standin.TOPIC, status_callback=lambda step: print(f"  → {step}"))
    if res.get("status") != "success":
        print(f"the stand-in run did not finish: {res.get('status')} {res.get('error')}")
        return 1
    made = tmp / "out" / res["slug"]
    if TARGET.exists():
        shutil.rmtree(TARGET)
    for p in sorted(made.rglob("*")):
        rel = p.relative_to(made)
        if not p.is_file() or p.name in LEAVE_OUT:
            continue
        dest = TARGET / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = p.read_bytes()
        if p.suffix in (".json", ".jsonl", ".md"):
            data = data.replace(str(tmp).encode(), b"<data>")
        dest.write_bytes(data)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"{sum(1 for p in TARGET.rglob('*') if p.is_file())} files -> {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
