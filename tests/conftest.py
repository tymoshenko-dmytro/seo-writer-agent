"""Set-up for the offline tests: no key is read, no connection leaves this machine, nothing is
written inside the repository.

Before anything imports the app: the services' keys are removed from the environment, the data
folder, the job list and the session secret point at a temp dir, and every connection off this
machine is refused (tests/netguard). No test spends money or reaches a live service.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

KEY_ENVS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PARALLEL_API_KEY", "AHREFS_API_KEY", "SCRAPFLY_KEY",
            "WINSTON_API_KEY", "WP_BASE_URL", "WP_USERNAME", "WP_APP_PASSWORD", "WP_AUTHOR_ID", "UI_PASSWORD")
for _k in KEY_ENVS:
    os.environ.pop(_k, None)
_TMP = Path(tempfile.mkdtemp(prefix="seo-writer-tests-"))
os.environ["JOBS_FILE"] = str(_TMP / "jobs.json")
os.environ["CONTENT_OUTPUT_DIR"] = str(_TMP / "output")
os.environ["UI_SECRET"] = "test-only-" + os.urandom(8).hex()
os.environ.setdefault("MPLBACKEND", "Agg")

_spec = importlib.util.spec_from_file_location("_netguard", Path(__file__).parent / "netguard" / "sitecustomize.py")
_spec.loader.exec_module(importlib.util.module_from_spec(_spec))


@pytest.fixture
def output_dir(tmp_path, monkeypatch) -> Path:
    """An empty data folder: a new install."""
    from core import api_keys

    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setenv("CONTENT_OUTPUT_DIR", str(out))
    # what an earlier test applied from a saved key file is forgotten
    monkeypatch.setattr(api_keys, "_ORIGINAL", {})
    monkeypatch.setattr(api_keys, "_APPLIED", set())
    monkeypatch.setattr(api_keys, "_STATE", {"dir": None})
    for k in KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    return out


@pytest.fixture
def studio(output_dir, monkeypatch):
    """The Studio on a new install, through Flask's test client (it answers this machine only
    when no password is set, as a test client is)."""
    import ui.app as appmod

    monkeypatch.setitem(appmod.app.config, "OUTPUT_ROOT", output_dir)
    appmod.app.config.update(TESTING=True)
    client = appmod.app.test_client()
    client.environ_base.update({"HTTP_ORIGIN": "http://localhost"})   # posts come from the Studio's pages
    return client
