# Configuration

Four layers, each over the one before it. Most installs only ever use the last two: the setup
and the Configure pages.

| Layer | Where | What |
|---|---|---|
| The engine's defaults | `config.yaml` | models, the pipeline's knobs, the SEO thresholds, the paths |
| The project's shipped defaults | `project/profile.yaml` and its texts | empty in a new install |
| The environment | `.env` or the server's variables | the password, the keys, the folders |
| What the Studio saves | the data volume | the setup, Configure, `cli.py setup/project/settings/prompts` |

A release never undoes the last layer: it lives on the data volume, not in the image.

## The environment

`.env.example` lists every variable with what it does. The ones that matter:

- `UI_PASSWORD`: the team's password. Docker Compose refuses to start without it. Without any
  password, the Studio answers only requests from its own machine.
- `UI_COOKIE_SECURE=1` when the Studio is served over HTTPS.
- The seven services' keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `PARALLEL_API_KEY`,
  `AHREFS_API_KEY`, `SCRAPFLY_KEY`, `WINSTON_API_KEY`, and `WP_BASE_URL`, `WP_USERNAME`,
  `WP_APP_PASSWORD`). A key saved in the setup or in Configure > Connections wins over the
  environment's.
- `CONTENT_OUTPUT_DIR`: the data folder (default `project/output`; the volume in Docker).
- `CONTENT_PROJECT_DIR`: another folder of shipped project defaults (default `project/`).

## The project

The setup and Configure > Project write it:

- **the name, the logo, the audience**;
- **the website**: our domains (internal links, sources the fixer may use, what the uniqueness
  check leaves out), the blog's address, the sitemaps the internal link index is built from (the
  site's defaults to `https://<domain>/sitemap.xml`, the blog's to
  `<blog>/sitemap_index.xml`);
- **the texts**: the brand voice, the product facts and the editorial rules (below);
- **the colours** of the infographics, and **the competitors**: hosts on which only a document
  (docs, help, a PDF, a report) may be linked; their product and pricing pages are cited in prose.

### The three texts

- **Brand voice**: how the brand writes: tone, words to use and avoid.
- **Product facts**: everything the agents may say about the product: plans, prices, features,
  limits, links to its pages. Its numbers count as attested, so keep it current. Empty, the
  articles say nothing specific about the product.
- **Editorial rules**: your own rules for what the articles may say and how: claims to avoid,
  how to name customers ("teams at …", never "used by …"), comparisons you allow or forbid, how
  much technical detail readers get. The writer follows them and the review raises a breach as a
  Major.

Each can be left empty and written later. From the command line:
`cli.py project --text editorial_rules --file rules.md`.

### Your own research sources (optional)

Data your project reads by itself that the web research cannot find (a leaderboard drawn by
JavaScript, an internal price list) can join the research for the articles it is meant for: a
Python file in the project folder, `research_sources/<name>.py`, with `gate()` (is this article
in its scope; it keeps its decision in the article's folder), `corpus_block()` (what it adds to
the research every agent reads; a URL in it written `[source: https://…]` may be cited like a
researched source) and, optionally, `leaves_out()` (pages the article must not use)
and `KEEP` (files a rerun copies). The contract is at the top of `core/research_sources.py`. It
is code from your own project folder, run as the Studio: nothing in the interface adds one.
Without the folder nothing changes.

## The settings

Configure > Research and Configure > Publishing (or `cli.py settings get|set|reset`): the search
market, how many competitor pages are measured, the keyword and LSI targets, the meta tag
lengths, what *Accept this version* does (nothing, a draft, a live post), the default category.

## The prompts

Every agent's prompt is a file in `core/prompts/`, editable under Configure > Prompts (or
`cli.py prompts set <name> --file f.md`). An edit is saved on the volume; its output format (the
part the code parses, between the `output-contract` markers) always runs as shipped. *Restore
original* removes the edit.

## Models and prices

`config.yaml` sets the models: `models.worker` (research, brief, the picture check),
`models.writer` (the article), `models.editor` (the fixer), `models.visuals` (the picture
producer), and the review's panel in `pipeline.qa.panel` (an `openai:` prefix sends a read to
OpenAI). A project can override them in `project/profile.yaml` under `models:`.

The price table (`core/pricing.py`, dated) turns the cost ledger into dollars; Configure > Prices
changes it. Ahrefs units and Winston credits have no price until you enter what your plan costs.

## WordPress

The Studio publishes through the WordPress REST API with an application password. A user with the
Editor or Administrator role can create drafts, publish and update posts; the connection test says
which role it found.

Two things reach the post only when the site accepts them:

- **Yoast SEO's meta title, description and focus keyphrase.** The Studio writes them as the post
  meta `_yoast_wpseo_title`, `_yoast_wpseo_metadesc` and `_yoast_wpseo_focuskw`. WordPress accepts
  post meta through the REST API only when it is registered for it, so add this to the site (a
  small plugin or the theme's `functions.php`):

  ```php
  add_action('init', function () {
      foreach (['_yoast_wpseo_title', '_yoast_wpseo_metadesc', '_yoast_wpseo_focuskw'] as $key) {
          register_post_meta('post', $key, [
              'show_in_rest'  => true,
              'single'        => true,
              'type'          => 'string',
              'auth_callback' => fn() => current_user_can('edit_posts'),
          ]);
      }
  });
  ```

  Without it the post is published without them, and Yoast falls back to its defaults.
- **The FAQ.** The article's questions and answers go to an Advanced Custom Fields field named
  `faq` (a repeater with the sub-fields `question` and `answer`, its field group with *Show in REST
  API* on), where your theme can render them and their FAQ markup. Without that field, the FAQ is
  added to the end of the post as a section, and the publish result says so.

## The browser

`browser.backend` in `config.yaml`: `scrapfly` opens third-party pages through Scrapfly in the
country of the search market, so a screenshot shows what a reader there sees; `local` uses the
Chromium installed with Playwright. Charts and infographics are always drawn locally.
