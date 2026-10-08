"""Every route of the interface and the command that does the same thing, or why there is none.
The interface and the CLI call the same core functions with the same checks, so an agent can do
what a person can, and nothing a person could not."""
from __future__ import annotations

# endpoint -> the command(s), as an agent would type them
COMMANDS = {
    "articles.dashboard": "cli.py list",
    "articles.article": "cli.py status <slug> · cli.py findings <slug> · cli.py versions <slug>",
    "articles.edit": "cli.py edit <slug> --version <v> --before … --after …",
    "articles.export_article": "cli.py export <slug>",
    "articles.import_article": "cli.py import <zip>",
    "costs.costs": "cli.py costs",
    "costs.costs_csv": "cli.py costs --json",
    "articles.run_again": "cli.py generate <topic> --reuse-from <slug> --in-place",
    "review.review_again": "cli.py review <slug> --version <v>",
    "review.apply_proposals": "cli.py apply <slug> <ids…> --version <v> --review <r>",
    "review.try_again": "cli.py fix-again <slug> <fid> --version <v>",
    "review.keep_as_is": "cli.py keep <slug> <group> --version <v>",
    "review.not_important": "cli.py not-important <slug> <fid> --version <v> [--why …]",
    "review.revert": "cli.py revert <slug> <group> --version <v>",
    "review.paragraph": "cli.py edit <slug> --version <v> --before … --after …",
    "review.version": "cli.py versions <slug> · cli.py compare <slug> <n>",
    "review.restore": "cli.py restore <slug> <n> --version <v>",
    "review.remove_picture": "cli.py remove-picture <slug> <asset> --version <v>",
    "review.upload_picture": "cli.py upload-picture <slug> <asset> <file> --version <v>",
    "publishing.accept": "cli.py accept <slug> --version <v> --action <a>",
    "publishing.check_post": "cli.py wp-status <slug>",
    "publishing.publish": "cli.py publish <slug>",
    "articles.keywords": "cli.py keywords <slug>",
    "publishing.uniqueness": "cli.py uniqueness <slug>",
    "system.new": "cli.py generate <topic>",
    "system.run": "cli.py generate <topic>",
    "system.refresh_links": "cli.py refresh-links",
    "visuals.cleanup_pictures": "cli.py cleanup <slug>",
    "visuals.make_cover": "cli.py cover <slug>",
    "review.redraw_picture": "cli.py redraw <slug> <asset> [--feedback …] [--kind auto|screenshot|infographic|chart|image]",
    "articles.save_seo": "cli.py seo <slug> --meta-title … --meta-description … --faq …",
    "settings.settings": "cli.py settings get · cli.py settings set key=value",
    "settings.settings_publishing": "cli.py settings get publishing.on_accept · cli.py settings set publishing.on_accept=draft",
    "settings.settings_keys": "cli.py setup --anthropic-key … · cli.py doctor",
    "settings.test_key": "cli.py doctor",
    "settings.test_wordpress": "cli.py doctor",
    "settings.wordpress_author": "cli.py setup --author <id>",
    "settings.settings_project": "cli.py project",
    "settings.project_logo": "cli.py project --logo <file>",
    "settings.project_website": "cli.py project --domains … --blog …",
    "settings.project_palette": "cli.py project --palette accent=#2A6FDB",
    "settings.project_competitors": "cli.py project --competitors …",
    "settings.settings_project_text": "cli.py project --text <key> --value …",
    "settings.settings_prompts": "cli.py prompts list",
    "settings.settings_prompt": "cli.py prompts show <name> · cli.py prompts set <name> --file <f>",
    "settings.setup_page": "cli.py setup",
    "settings.setup_project": "cli.py setup --name … --domain … --blog …",
    "settings.setup_keys": "cli.py setup --anthropic-key …",
    "settings.setup_wordpress": "cli.py setup --wp-url … --wp-user … --wp-password …",
    "settings.setup_wordpress_pick": "cli.py setup --author <id> --category …",
    "settings.setup_done": "cli.py setup",
    "settings.setup_texts": "cli.py setup --brand-voice-file … --product-facts-file … --editorial-rules-file …",
    "settings.test_all": "cli.py doctor",
    "system.sample": "cli.py sample",
}

# endpoint (or a prefix ending in "*") -> why it has no command
NO_COMMAND = {
    "static": "the interface's own stylesheets, scripts and fonts",
    "articles.assets": "the article's pictures, served from its folder; an agent reads the folder",
    "articles.kept_picture": ("a picture as it was before a change made it again, served from the change's "
                              "kept copy; an agent reads `kept` in `findings --json` and the folder"),
    "articles.preview": "renders the editor's text for its preview; the CLI has no preview",
    "auth.login": "a person's session; the CLI runs on the install itself",
    "auth.logout": "a person's session; the CLI runs on the install itself",
    "system.healthz": "the server's liveness probe; `cli.py doctor` (step 8) checks the install",
    "system.brand_logo": "the project's logo for the sidebar and the login card",
    "system.api_job": "a job's progress for the page; a command waits for its own work",
    "system.jobs_api": "the jobs line's progress for the page; a command waits for its own work",
    "system.design": "the design reference: every component with sample data",
    "articles.delete_article": "moving an article to the trash: `cli.py delete` comes with the list (step 7)",
    "visuals.ai_edit": "the editor's Edit with AI: an agent edits the text itself (`cli.py edit`)",
    "visuals.visual_edit": "the old editor's picture redraw, as JSON for its page: `cli.py redraw` does the same",
    "visuals.visual_insert": "the editor's new picture: comes with the block editor (step 6)",
    "settings.dismiss_migrated": "a one-time note on the Publishing page that the person has read",
    "settings.settings_prices": "the price table the cost report uses; `cli.py costs` reads it, prices are set on the page",
    "settings.settings_access": "cli.py access [--off --yes | --password-stdin]",
    "review.version_picture": "a picture as a version kept it, for the version's page; `cli.py compare` names them",
}


def command_for(endpoint: str) -> str | None:
    return COMMANDS.get(endpoint)


def reason_for(endpoint: str) -> str | None:
    if endpoint in NO_COMMAND:
        return NO_COMMAND[endpoint]
    for key, why in NO_COMMAND.items():
        if key.endswith("*") and endpoint.startswith(key[:-1]):
            return why
    return None
