"""The review of 7108e86 (seo-writer-agent-reviews/refactoring-review-7108e86.md): its probes for
R710-03 kept as tests."""
from __future__ import annotations

import re
from pathlib import Path
import pytest
import standin
from test_review_c91 import prepare, writer_state

PICKLED_SOURCE = '''\
from __future__ import annotations
import pickle
from dataclasses import dataclass
KEEP = ("_measurement.pickle",)
@dataclass
class Measurement:
    value: int
    def display(self):
        return f"{self.value} UNIT"
def gate(article_dir, *args):
    pin = article_dir / "_measurement.pickle"
    if pin.exists():
        return "pinned"
    pin.write_bytes(pickle.dumps(Measurement(812)))
    return "on"
def corpus_block(article_dir, project):
    pin = article_dir / "_measurement.pickle"
    return pickle.loads(pin.read_bytes()).display() if pin.exists() else ""
'''


def test_modules_with_the_same_filename_do_not_cross_project_boundaries(tmp_path, monkeypatch):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    _, cfg_a, project_a = prepare(a, monkeypatch, PICKLED_SOURCE.replace('UNIT', 'milliseconds'))
    writer_a, state_a = writer_state(a, cfg_a, project_a)
    seen = []
    answer = standin.answer
    monkeypatch.setattr(standin, 'answer', lambda role, text: (seen.append((role, text)), answer(role, text))[1])
    writer_a.run(state_a)
    assert '812 milliseconds' in writer_a._facts_block(state_a)
    original_pin = (Path(state_a.output_dir) / '_measurement.pickle').read_bytes()
    _, cfg_b, project_b = prepare(b, monkeypatch, PICKLED_SOURCE.replace('UNIT', 'dollars'))
    writer_b, state_b = writer_state(b, cfg_b, project_b)
    writer_b.run(state_b)
    assert '812 dollars' in writer_b._facts_block(state_b)
    seen.clear()
    writer_a.run(state_a)
    assert (Path(state_a.output_dir) / '_measurement.pickle').read_bytes() == original_pin
    assert seen and '812 milliseconds' in seen[0][1] and '812 dollars' not in seen[0][1], {
        'project_A_corpus_after_B': re.findall(r'812 [a-z]+', seen[0][1]),
        'pin_unchanged': True}
