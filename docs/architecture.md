# How it works

## A run

```
topic
  -> keyword research (Ahrefs)        the primary keyword, its clusters, the ranking pages measured
  -> brief                            the outline, each section's keywords and format, the questions
  -> deep research (Parallel)         facts with their sources, and what could not be found
  -> writing (Anthropic)              the article in the brand's voice, every outside fact cited
  -> pictures                         charts and infographics drawn here, screenshots (Scrapfly or the
                                      local browser), a cover (OpenAI image + the headline and logo)
  -> finalize                         article.md, the keyword report, the uniqueness check (Winston AI)
  -> the first version kept
  -> one review                       a panel of three reads, the picture check, the code's checks;
                                      the safe changes made, the rest listed for a person
```

The graph is `core/orchestrator.py` (LangGraph); each step is one agent in `core/agents/`, with
its prompt in `core/prompts/<name>.md`. A brief that does not parse stops the run before the
paid research. Each run gets a new folder (`topic`, `topic-2`, …), so a new run never writes over
an article that is already on WordPress.

## The project in the prompts

The prompts are general. Everything about one product reaches them through five slots filled at
runtime from the project's texts (`core/project_texts.py`): `{{project_name}}`, `{{audience}}`,
`{{brand_voice}}`, `{{product_facts}}` and `{{editorial_rules}}`. The writer, the review and the
in-editor AI edit read all of them; the edit check gets the product facts and the editorial
rules, and the fixer the editorial rules (and the product facts when a fix is about the product). A text left empty says so in the prompt ("None given") instead of leaving a
heading over nothing. The product facts are the only authority for claims about the product: the
review accepts a number found there without a source, and refuses a feature that is not there.

## The review

The review happens once per click, never in a loop:

- **The panel** (`pipeline.qa.panel`): three reads, cross-vendor on purpose (two Claude models and
  one OpenAI model), because different models miss different things. Each read answers in a fixed
  JSON format: every finding with its severity, its kind (text, fact, link, keyword, picture,
  structure, meta), the one action that fixes it and the exact words or picture it targets.
- **The picture check**: a vision model looks at every picture with its caption.
- **The code's checks**: keyword coverage, links, structure, measured without a model.
- **One finding per problem**: the finding matcher decides which raw findings say the same thing.
- **The safe changes** (`core/qa_model.SAFE`) are made by the fixer, each bounded to its target,
  each a version of its own, each checked afterwards by the edit check. A changed fact, a picture's
  removal or an outline change is a proposal for the person.

Three facts stay apart and are never folded into one flag: what the review left open, the
person's acceptance (*Accept this version*), and WordPress (what was last sent, what the post is
now). Nothing is sent unless someone sends it.

## Versions

Every state of an article's text and pictures is a version (`core/versions.py`, kept in the
article's `_versions/` folder, content-addressed): the run's article, each machine change, each
edit, each restore. A page carries the version it showed; an action on an article that changed
since is refused with the reason, so a stale page never acts.

## The code

```
core/                 the engine
  orchestrator.py     the graph of a run
  agents/             one file per agent; prompts in core/prompts/<name>.md
  producers/          charts (matplotlib), screenshots (Playwright / Scrapfly), infographics, cover
  tools/              Ahrefs, Parallel, Scrapfly, Winston, OpenAI images, WordPress, the link index
  store.py            the article folder: atomic writes, the per-article lock, the frontmatter
  qa_model.py         the review's records: findings, change groups, acceptance, the post
  qa_review.py        one review: the panel, the picture check, the code's checks
  qa_changes.py       the changes: the safe ones, proposals, revert, Not important
  publication.py      Accept and the sends to WordPress
  versions.py         every state of an article, restorable
  agent_door.py       the facts the CLI's --json answers carry (the same the pages show)
  setup.py            what the install still needs before it can write
  connections.py      the free test of each service
  project_settings.py, project_texts.py, settings.py, api_keys.py   what the Studio saves
  usage.py, pricing.py  the cost ledger and the price table
  transfer.py         export and import of an article between installs
  sample.py           the sample article
ui/                   the Studio (Flask): app.py builds it, views/ holds the pages, static/js the scripts
project/              the project's shipped defaults: profile.yaml and its texts (empty)
samples/              the sample article (made by bin/make_sample.py)
cli.py                every action of the interface as a command
tests/                offline tests; tests/standin.py runs a whole article on stand-in services
```

## One process

The Studio is one process (gunicorn, one worker, threads). The job queue lives in memory and the
article locks are per process, so run one replica. All state is files on the data volume.
