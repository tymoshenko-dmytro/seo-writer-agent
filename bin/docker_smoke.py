#!/usr/bin/env python3
"""The Docker smoke, in one command: build the image, run it with a password on an empty data
volume, and check what a person meets first.

    python bin/docker_smoke.py [--repo DIR] [--keep]

Checks: /healthz answers; a page asks for the password and the password opens it; a new install
shows the setup; the sample article opens on its tabs; Connections and Costs answer; the server
logged no error; Chromium starts inside the image (the infographics and screenshots
need it); `cli.py doctor` answers inside the container. Prints one line per check and exits 1 when
any fails. No key and no network service is used, and the container is removed afterwards
(--keep leaves it running). CI runs it on every push (.github/workflows/ci.yml).
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

PASSWORD = "smoke-only"
SLUG = "sample-meta-description"


def sh(*cmd: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}… failed: {(res.stderr or res.stdout)[-800:]}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    tag = f"seo-writer-smoke:{int(time.time())}"
    name = tag.replace(":", "-")
    results: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        results.append((label, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""), flush=True)

    out = Path(tempfile.mkdtemp(prefix="docker-smoke-")) / "output"
    out.mkdir()
    out.chmod(0o777)            # the container's user writes to it
    t0 = time.time()
    build = sh("docker", "build", "-q", "-t", tag, str(repo), check=False)
    check("the image builds", build.returncode == 0, f"{int(time.time() - t0)} s" if build.returncode == 0
          else (build.stderr or build.stdout)[-600:])
    if build.returncode != 0:
        return 1
    port = 5000 + int(time.time()) % 900 + 100
    sh("docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:5050", "-e", f"UI_PASSWORD={PASSWORD}",
       "-v", f"{out}:/app/project/output", tag)
    base = f"http://127.0.0.1:{port}"
    try:
        up = False
        for _ in range(60):
            try:
                up = urllib.request.urlopen(base + "/healthz", timeout=2).status == 200
                break
            except Exception:  # noqa: BLE001 — still starting
                time.sleep(1)
        check("/healthz answers", up)
        if not up:
            return 1
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        first = opener.open(base + "/", timeout=10)
        check("a page asks for the password", "/login" in first.geturl(), first.geturl())
        body = urllib.parse.urlencode({"password": PASSWORD, "next": "/"}).encode()
        # the login form posts from the Studio's own page: the same-origin rule covers it (R058-11)
        req = urllib.request.Request(base + "/login", data=body, headers={"Origin": base})
        try:
            logged = opener.open(req, timeout=10)
            check("the password opens the Studio", "/login" not in logged.geturl(), logged.geturl())
        except urllib.error.HTTPError as e:
            check("the password opens the Studio", False, f"HTTP {e.code}")
        home = opener.open(base + "/", timeout=10).read().decode()
        check("a new install shows the setup", "The setup is not finished" in home)
        req = urllib.request.Request(base + "/sample", data=b"", headers={"Origin": base})
        try:
            got = opener.open(req, timeout=30)
            check("the sample article is added", got.geturl().endswith(f"/a/{SLUG}"), got.geturl())
        except urllib.error.HTTPError as e:
            check("the sample article is added", False, f"HTTP {e.code}")
        for path in ("/setup", f"/a/{SLUG}", f"/a/{SLUG}?tab=review", f"/a/{SLUG}?tab=pictures",
                     f"/a/{SLUG}?tab=history", "/settings/keys", "/settings/publishing", "/costs"):
            try:
                code = opener.open(base + path, timeout=20).status
            except urllib.error.HTTPError as e:
                code = e.code
            check(f"GET {path}", code == 200, str(code))
        logs = sh("docker", "logs", name, check=False)
        text = (logs.stdout or "") + (logs.stderr or "")
        check("the server logged no error", "Traceback" not in text and "[ERROR]" not in text,
              next((ln for ln in text.splitlines() if "Traceback" in ln or "[ERROR]" in ln), "")[:200])
        chromium = sh("docker", "exec", name, "python", "-c",
                      "from playwright.sync_api import sync_playwright\n"
                      "with sync_playwright() as p:\n"
                      " b=p.chromium.launch(); pg=b.new_page(); pg.set_content('<h1>ok</h1>');"
                      " print(pg.inner_text('h1')); b.close()", check=False)
        check("Chromium starts in the image", chromium.stdout.strip() == "ok",
              (chromium.stderr or chromium.stdout)[-300:].strip())
        doctor = sh("docker", "exec", name, "python", "cli.py", "doctor", "--json", "--no-test", check=False)
        try:
            ok = json.loads(doctor.stdout)["ok"] is False and doctor.returncode == 1
        except ValueError:
            ok = False
        check("cli.py doctor answers (and names what is missing)", ok, (doctor.stderr or "")[-300:].strip())
    finally:
        if not a.keep:
            sh("docker", "rm", "-f", name, check=False)
            sh("docker", "rmi", tag, check=False)
            shutil.rmtree(out.parent, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
