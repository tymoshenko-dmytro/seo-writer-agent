"""The review of fd4e36e (seo-writer-agent-reviews/refactoring-review-fd4e36e.md): its probe for
RFD4-01 kept as a test."""
from __future__ import annotations

import sys

import pytest
import standin

from test_review_710_loader import PICKLED_SOURCE
from test_review_c91 import prepare, writer_state


@pytest.mark.parametrize("neighbour", ["valid", "broken"])
def test_removing_a_neighbour_preserves_cached_module_registration(tmp_path, monkeypatch, neighbour):
    from core import research_sources

    root, cfg, project = prepare(tmp_path, monkeypatch, PICKLED_SOURCE.replace("UNIT", "milliseconds"))
    requests = []
    answer = standin.answer
    monkeypatch.setattr(standin, "answer", lambda role, text: (requests.append((role, text)), answer(role, text))[1])
    writer, first = writer_state(tmp_path, cfg, project, "first")
    writer.run(first)
    original = research_sources.sources(project)[0]
    original_pin = (tmp_path / "first" / "_measurement.pickle").read_bytes()
    ledger = root / "research_sources" / "ledger.py"
    original_bytes, original_stamp = ledger.read_bytes(), ledger.stat().st_mtime_ns
    assert "812 milliseconds" in writer._facts_block(first)
    path = root / "research_sources" / "other.py"
    path.write_text('def gate(*a): return "on"\ndef corpus_block(*a): return ""\n'
                    if neighbour == "valid" else 'raise RuntimeError("synthetic source installation error")\n')
    _, second = writer_state(tmp_path, cfg, project, "second")
    writer.run(second)
    assert "812 milliseconds" in writer._facts_block(second)
    # Only remove the neighbour. The ledger's bytes, mtime, root and pinned files never change.
    path.unlink()
    _, third = writer_state(tmp_path, cfg, project, "third")
    requests.clear()
    writer.run(third)
    assert (tmp_path / "first" / "_measurement.pickle").read_bytes() == original_pin
    assert ledger.read_bytes() == original_bytes and ledger.stat().st_mtime_ns == original_stamp
    writer_requests = [text for role, text in requests if role == "copywriter"]
    assert writer_requests and all("812 milliseconds" in text for text in writer_requests), {
        "cached_module_is_original": research_sources.sources(project)[0] is original,
        "registration_is_original": sys.modules.get(original.__name__) is original,
        "source_in_writer_and_faq": ["812 milliseconds" in text for text in writer_requests],
        "third_pin_exists": (tmp_path / "third" / "_measurement.pickle").exists(),
    }
