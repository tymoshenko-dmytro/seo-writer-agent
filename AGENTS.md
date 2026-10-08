# AGENTS.md: installing and running SEO Writer Agent for your person

This file is for an agent (Claude Code, Codex) that installs the Studio for someone and runs it for
them, who may never open its interface. Everything the interface does is a command here, with the
same checks: you can do what a person can, and nothing a person could not.

## What it is

SEO Writer Agent turns a topic into a researched, illustrated, reviewed blog article and sends it
to WordPress. A run researches keywords (Ahrefs), plans the article, gathers cited facts
(Parallel), writes it (Anthropic), makes its charts, infographics, screenshots (Scrapfly) and
cover (OpenAI), checks its uniqueness (Winston AI), and then reviews it once: the review makes the
safe changes itself and lists the rest for a person. Every change is a version that can be
restored. Sending to WordPress happens only when someone asks for it.

## What to ask your person for

Before you start, ask for these; never invent a key or take one from somewhere else:

- the project: its name, its site's domain, the blog's address, who the articles are for in one
  line, and optionally a logo file;
- three texts, each may be empty: the brand voice (how they write), the product facts (what the
  articles may say about the product: plans, prices, features), and editorial rules (their own
  rules: claims to avoid, how to name customers, comparisons they allow);
- the keys of all seven services: Anthropic, OpenAI, Parallel, an Ahrefs MCP key, Scrapfly,
  Winston AI, and WordPress (the site's https address, a user who can publish, an application
  password from Users > Profile > Application Passwords);
- a password for the Studio's web interface;
- how much they agree to spend on the first trial article.

## Install

With Docker (recommended):

```bash
git clone <the repository's URL> seo-writer-agent && cd seo-writer-agent
cp .env.example .env            # put UI_PASSWORD (and the keys, if you prefer) in .env
docker compose up --build -d    # the Studio on http://127.0.0.1:5050
```

Run commands inside the container: `docker compose exec studio python cli.py …`.

Without Docker (Python 3.14):

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Run commands as `.venv/bin/python cli.py …`. The examples below write `cli.py …` for either.

## Set it up

```bash
cli.py setup --name "Northwind Payroll" --domain northwind.example --blog https://blog.northwind.example \
  --audience "Owners of small businesses who run payroll themselves" --logo ./logo.png \
  --brand-voice-file brand.md --product-facts-file products.md --editorial-rules-file rules.md \
  --anthropic-key "$ANTHROPIC_API_KEY" --openai-key "$OPENAI_API_KEY" --parallel-key "$PARALLEL_API_KEY" \
  --ahrefs-key "$AHREFS_API_KEY" --scrapfly-key "$SCRAPFLY_KEY" --winston-key "$WINSTON_API_KEY" \
  --wp-url https://blog.northwind.example --wp-user editor --wp-password "$WP_APP_PASSWORD" --json
```

`setup` saves what the setup screen saves and tests each key and the WordPress connection with
free calls. Keys already in the environment (`.env`) are used as they are; `setup` without a key
flag leaves them. Then pick the author the posts carry and the default category, from what the
WordPress test read: `cli.py setup --author <user id> --category "<category>" --json`.

`cli.py project`, `cli.py settings get|set`, `cli.py prompts list|show|set|reset` change what the
Configure pages change. `cli.py access` says who may open the Studio; turning its password off
(`--off --yes`) or setting a new one (`--password-stdin`) is your person's call: ask.

## The self-check

Two steps, after the setup.

**1. `cli.py doctor --json`** (free). It tests the seven keys with free calls (listing models,
reading the account, or a request the service refuses before doing any work), the WordPress
connection, the project's name and domain, the data folder and the browser. It exits 1 and says
what is wrong when anything is missing or fails. Fix each one with your person before going on.

**2. A trial article** (paid). Tell your person what it costs first:

```bash
cli.py generate "a short topic of theirs" --json        # prints the estimate and stops
cli.py generate "a short topic of theirs" --yes --json  # runs, after your person agreed
```

Without `--yes` (and without a terminal) a paid command prints its estimate and does nothing.
Give your person the estimate and the topic, and run it with `--yes` only after they agree. A run
takes from several minutes to over an hour (the deep research). Then `cli.py costs <slug> --json`
says what it actually cost, and `cli.py status <slug> --json` where it stands.

To look at a finished article without spending anything: `cli.py sample --json` adds the sample
article (made up, with its review and pictures).

## Run, review and publish

```bash
cli.py generate "<topic>" --yes --json                                 # priced first; see "Paid actions"
cli.py list --json                                                     # every article, three facts each
cli.py status <slug> --json                                            # its version, review, acceptance, WordPress
cli.py findings <slug> --json                                          # what needs your person, what changed
cli.py apply <slug> <id>… --version <v> --review <r> --yes --json     # make proposals
cli.py not-important <slug> <id> --version <v> --json                  # --why "…" optional
cli.py revert <slug> <group> --version <v> --json                      # the latest change, or a picture as made
cli.py keep <slug> <group> --version <v> --json                        # a half-done change, kept as it is
cli.py edit <slug> --version <v> --before "…" --after "…" --json        # one paragraph, in your words
cli.py versions <slug> --json ; cli.py restore <slug> <n> --version <v> --json
cli.py upload-picture <slug> <asset> <file> --version <v> --json       # your own picture there, free
cli.py accept <slug> --version <v> --action none --json                # your person's mark
cli.py publish <slug> --json ; cli.py wp-status <slug> --json          # send, read back
cli.py costs <slug> --json
```

The version (`--version`) and the review (`--review`) an action needs come from `status --json`
and `findings --json`. An action on an article that changed since is refused with the reason
(`"refused": "stale"`), exactly as a page that is out of date is refused; read the status again
and decide on what is there now. Every command takes `--json` and prints one object; a refusal
exits 1 with `"ok": false`, `"refused"` and `"reason"`. `docs/cli.md` lists every command.

## Paid actions

A command that spends money (`generate`, `review`, `apply`, `fix-again`, `cover`, `redraw`,
`visual-check`, `uniqueness`) prints its estimate first. Without a terminal it needs `--yes`:
tell your person what it will cost before you run it with `--yes`. Never retry a paid command in
a loop.

## Rules

- Nothing is sent to WordPress unless someone runs `publish` or an `accept` whose action sends.
  A live post is changed only by `publish --update-live`.
- Do not edit files in the articles' folder by hand; use the commands, so every change is a
  version and the review and the acceptance stay true.
- Restoring a version is never an acceptance or a send.
- Keys stay where your person put them: never print them, commit them or send them elsewhere.

## Where its data lives

Everything the install writes is in one folder: the Docker volume (mounted at
`/app/project/output`), or `project/output/` (`$CONTENT_OUTPUT_DIR`) without Docker. One folder
per article, plus the install's settings, keys (`_secrets.json`, owner-only), prompts and project
texts. Back up that folder; the image carries none of it.

## Update

```bash
git pull && docker compose up --build -d
```

The data folder carries over.
