# Third-party notices

SEO Writer Agent's own code is under the MIT licence (`LICENSE`). What it ships or installs from
others keeps its own licence, listed here.

## Shipped in this repository

| What | Where | Licence |
|---|---|---|
| Inter 4.001 (subset to WOFF, weights 400 to 700) | `ui/static/fonts/inter/` | SIL Open Font License 1.1, `ui/static/fonts/inter/OFL.txt` |
| JetBrains Mono 2.211 (subset to WOFF, weights 400 to 600) | `ui/static/fonts/jetbrains-mono/` | SIL Open Font License 1.1, `ui/static/fonts/jetbrains-mono/OFL.txt` |
| Lucide icons (from `@iconify-json/lucide` 1.2.130) | `ui/vendor/lucide/icons.json` | ISC, `ui/vendor/lucide/LICENSE` |
| Tiptap 3.31.2 with ProseMirror, marked and linkifyjs, bundled into one file | `ui/static/vendor/tiptap/tiptap.js` | MIT, each package's licence in `ui/static/vendor/tiptap/LICENSES.md` |

How the fonts were subset and the editor bundle built: `ui/vendor/README.md`.

The sample article (`samples/sample-article/`) was written for this project. Its one source is
Google's public documentation on snippets, which it links and summarises.

## Installed by `pip install -r requirements.txt`

Not shipped in this repository; installed from PyPI, and included in the Docker image.

| Package | Version | Licence |
|---|---|---|
| annotated-types | 0.7.0 | MIT |
| anthropic | 1.0.0 | MIT |
| anyio | 4.14.1 | MIT |
| blinker | 1.9.0 | MIT |
| certifi | 2026.6.17 | MPL-2.0 |
| charset-normalizer | 3.4.7 | MIT |
| click | 8.4.2 | BSD-3-Clause |
| contourpy | 1.3.3 | BSD-3-Clause |
| cycler | 0.12.1 | BSD-3-Clause |
| distro | 1.9.0 | Apache-2.0 |
| docstring_parser | 0.18.0 | MIT |
| Flask | 3.1.3 | BSD-3-Clause |
| fonttools | 4.63.0 | MIT |
| greenlet | 3.5.3 | MIT AND PSF-2.0 |
| gunicorn | 23.0.0 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore2 | 2.12.0 | BSD-3-Clause |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx2 | 2.12.0 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.18 | BSD-3-Clause |
| itsdangerous | 2.2.0 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD-3-Clause |
| jiter | 0.16.0 | MIT |
| jsonpatch | 1.33 | BSD-3-Clause |
| jsonpointer | 3.1.1 | BSD-3-Clause |
| kiwisolver | 1.5.0 | BSD-3-Clause |
| langchain-core | 1.4.8 | MIT |
| langchain-protocol | 0.0.18 | MIT |
| langgraph-checkpoint | 4.1.1 | MIT |
| langgraph-prebuilt | 1.1.0 | MIT |
| langgraph-sdk | 0.4.2 | MIT |
| langgraph | 1.2.6 | MIT |
| langsmith | 0.9.3 | MIT |
| markdown-it-py | 4.2.0 | MIT |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| matplotlib | 3.11.0 | Matplotlib License (PSF-based, BSD-compatible) |
| mdurl | 0.1.2 | MIT |
| numpy | 2.5.0 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| orjson | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| ormsgpack | 1.12.2 | Apache-2.0 OR MIT |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| parallel-web | 1.1.0 | MIT |
| pillow | 12.2.0 | MIT-CMU (HPND) |
| playwright | 1.61.0 | Apache-2.0 |
| pydantic | 2.13.4 | MIT |
| pydantic_core | 2.46.4 | MIT |
| pyee | 13.0.1 | MIT |
| pyparsing | 3.3.2 | MIT |
| python-dateutil | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| PyYAML | 6.0.3 | MIT |
| requests-toolbelt | 1.0.0 | Apache-2.0 |
| requests | 2.34.2 | Apache-2.0 |
| six | 1.17.0 | MIT |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| tenacity | 9.1.4 | Apache-2.0 |
| truststore | 0.10.4 | MIT |
| typing-inspection | 0.4.2 | MIT |
| typing_extensions | 4.15.0 | PSF-2.0 |
| urllib3 | 2.7.0 | MIT |
| uuid_utils | 0.16.2 | BSD-3-Clause |
| websockets | 15.0.1 | BSD-3-Clause |
| Werkzeug | 3.1.8 | BSD-3-Clause |
| xxhash | 3.8.0 | BSD-2-Clause |
| zstandard | 0.25.0 | BSD-3-Clause |

`pytest` (MIT) is installed for the tests only (`requirements-dev.txt`).

## In the Docker image

The image is built on the official `python:3.14-slim-bookworm` image (Debian packages under their
own licences) and adds Chromium, downloaded by Playwright with its system dependencies (Chromium
is under a BSD-style licence with the licences of its components).
