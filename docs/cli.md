# The command line

`cli.py` does everything the interface does, through the same functions and the same checks.
Every command takes `--json` and prints one object; a refusal exits 1 with `"ok": false`,
`"refused"` and `"reason"`. `cli.py <command> -h` shows a command's options.

In Docker: `docker compose exec studio python cli.py …`. Without Docker:
`.venv/bin/python cli.py …`.

## Setting up

| Command | What it does |
|---|---|
| `setup` | the project (`--name --domain --blog --audience --logo`), its texts (`--brand-voice-file --product-facts-file --editorial-rules-file`), the keys (`--anthropic-key --openai-key --parallel-key --ahrefs-key --scrapfly-key --winston-key`), WordPress (`--wp-url --wp-user --wp-password`, then `--author --category`); each tested for free and saved as the setup screen saves it |
| `doctor` | the seven services (free tests), the project, the data folder, the browser; exit 1 when anything is missing or fails (`--no-test`: only what is set) |
| `sample` | add the sample article, to look at the Studio without keys |
| `project` | the project's facts, or change its texts (`--text <key> --value/--file`, `--reset-text`), logo, domains, blog, sitemaps, colours, competitors |
| `settings get / set key=value / reset` | the research and publishing settings |
| `prompts list / show / set / reset` | the agents' prompts |
| `access` | who may open the Studio; `--password-stdin` sets a new password, `--off --yes` turns it off |
| `refresh-links` | rebuild the internal link index from the sitemaps |

## Writing

| Command | What it does |
|---|---|
| `generate "<topic>"` | a new article (paid: the estimate first, `--yes` to run). `--words`, `--processor lite|core|pro|ultra` (Parallel), `--no-ahrefs`; `--reuse-from <slug>` writes again from an article's saved research |
| `list` | every article, with its three facts: the review, the acceptance, WordPress |
| `status <slug>` | one article's facts, and the version an action needs |
| `costs [<slug>]` | what an article (or every article) cost, by part and service |

## Reviewing

| Command | What it does |
|---|---|
| `review <slug> --version <v>` | review again (paid) |
| `findings <slug>` | the latest review: what needs the person, what the machine changed, what is closed |
| `apply <slug> <ids…> --version <v> --review <r>` | make proposals (paid) |
| `fix-again <slug> <fid> --version <v>` | try a safe change that did not land once more (paid) |
| `not-important <slug> <fid> --version <v> [--why …]` | close a finding |
| `revert <slug> <group> --version <v>` | undo the latest change, or a picture as it was made |
| `keep <slug> <group> --version <v>` | keep a change the check says left its problem in place |
| `edit <slug> --version <v> --before … --after …` | your own words for one paragraph |
| `seo <slug> --version <v>` | the meta title and description, the slug, the focus keyword, the FAQ |
| `keywords [<slug>]` | the keyword coverage report |
| `uniqueness <slug>` | the uniqueness check (Winston credits) |
| `visual-check <slug>` | a vision model looks at every picture and says what is wrong (paid, changes nothing) |

## Versions and pictures

| Command | What it does |
|---|---|
| `versions <slug>` | every version, newest first |
| `compare <slug> <n> [--to <m>]` | what changed from version n to version m (or to now) |
| `restore <slug> <n> --version <v>` | write version n back as the newest |
| `cover <slug>` | a new cover (paid) |
| `redraw <slug> <asset> --version <v>` | a chart or infographic drawn again, a screenshot captured again (paid); `--feedback`, `--kind` |
| `upload-picture <slug> <asset> <file> --version <v>` | your own picture in its place (free) |
| `remove-picture <slug> <asset> --version <v>` | take a picture and its caption out |
| `cleanup <slug>` | the picture rules in code (free) |

## Publishing and moving

| Command | What it does |
|---|---|
| `accept <slug> --version <v> --action none|draft|publish|update_live` | mark this version accepted, and do what Configure > Publishing says |
| `publish <slug>` | send to WordPress as a draft; `--live` publishes, `--update-live` changes a live post, `--to-draft` takes it back to a draft |
| `wp-status <slug>` | read the post's status from WordPress |
| `export <slug>` / `import <zip> [--as <slug>]` | an article as a zip, for another install |
| `migrate-qa` | show what the upgrade of older article folders would do (changes nothing) |

## Paid actions

A command that spends money prints its estimate first. In a terminal it asks; without one it
needs `--yes`. The estimate is the average of the last such actions on this install, or a default
until there are some.
