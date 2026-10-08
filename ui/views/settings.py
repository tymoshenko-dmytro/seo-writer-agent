"""Configure: Project, Connections, Publishing, Research, Prompts, Prices. Everything is stored on the article volume, so a release
keeps it: the research knobs (core/settings.py), the project's facts and logo
(core/project_settings.py), its texts (core/project_texts.py), the keys (core/api_keys.py), the
prompts (core/prompt_overrides.py), the prices (core/pricing.py). The same functions answer
`cli.py settings`, `project`, `prompts` and `setup` (ui/commands.py)."""
from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from core import api_keys, connections, pricing, project_settings, project_texts, prompt_overrides
from core import settings as user_settings
from core.config import load_core_config, load_project
from core.tools.wp_publisher import author_for

from ..context import CONFIG, out, project
from ..privacy import scrub_mixed

bp = Blueprint("settings", __name__)


def _yaml_seo():
    """The shipped defaults, deliberately WITHOUT the saved overrides applied."""
    return load_project(core=load_core_config(), apply_user_settings=False).seo


def _knobs(publishing: bool):
    yaml_seo = _yaml_seo()
    current = user_settings.resolved(yaml_seo, out())
    saved = user_settings.load(out())
    fields = [f for f in user_settings.FIELDS if (f["group"] == "Publishing") == publishing]
    overridden = {f["key"] for f in fields if user_settings.get_path(saved, f["key"]) is not None}
    return yaml_seo, current, saved, fields, overridden


def _save_part(fields, form, yaml_seo) -> list[str]:
    """Save the fields of one page, the other page's saved values kept (core/settings.save_values)."""
    return user_settings.save_values(out(), {k: form.get(k) for k in (f["key"] for f in fields)}, yaml_seo,
                                     keys=[f["key"] for f in fields])


# ---------------------------------------------------------------------------------------------
# Research and Publishing
# ---------------------------------------------------------------------------------------------
@bp.route("/settings", methods=["GET", "POST"])
def settings():
    """Research: how the research reads the search results. The orchestrator reloads the project
    per run, so a save applies to the next article with no restart."""
    yaml_seo, current, saved, fields, overridden = _knobs(publishing=False)
    if request.method == "POST":
        if request.form.get("action") == "reset":
            _save_part(fields, {}, yaml_seo)
            flash("Back to the defaults that ship with the app.")
            return redirect(url_for("settings.settings"))
        errors = _save_part(fields, request.form, yaml_seo)
        if errors:
            for e in errors:
                flash(e)
        else:
            flash("Saved. The next article uses these settings.")
            return redirect(url_for("settings.settings"))
    groups = [(g, [f for f in fields if f["group"] == g]) for g in user_settings.GROUPS if g != "Publishing"]
    return render_template("configure/research.html", groups=groups, current=current, overridden=overridden,
                           has_overrides=bool(overridden))


@bp.route("/settings/publishing", methods=["GET", "POST"])
def settings_publishing():
    """What Accept also does, and where new posts go."""
    yaml_seo, current, saved, fields, overridden = _knobs(publishing=True)
    if request.method == "POST":
        errors = _save_part(fields, request.form, yaml_seo)
        if errors:
            for e in errors:
                flash(e)
        else:
            flash("Saved. The next Accept and the next send follow it.")
            return redirect(url_for("settings.settings_publishing"))
    site = project_settings.wp_site(out()) or {}
    categories = list(dict.fromkeys([*(site.get("categories") or []), *user_settings._profile_categories()]))
    author_id = author_for(project().publish)
    author = next((a["name"] for a in site.get("authors") or [] if a.get("id") == author_id),
                  f"user #{author_id}" if author_id else None)
    return render_template("configure/publishing.html", current=current, overridden=overridden,
                           accept_field=next(f for f in fields if f["key"] == "publishing.on_accept"),
                           categories=categories, site_categories=bool(site.get("categories")), author=author,
                           migrated=user_settings.migrated_notice(out()))


@bp.route("/settings/migrated/dismiss", methods=["POST"])
def dismiss_migrated():
    """Publishing said once what the retired publishing setting was; the person has read it."""
    user_settings.dismiss_notice(out())
    return redirect(url_for("settings.settings_publishing"))


# ---------------------------------------------------------------------------------------------
# Connections: the keys, WordPress, the link index, this install
# ---------------------------------------------------------------------------------------------
@bp.route("/settings/keys", methods=["GET", "POST"])
def settings_keys():
    """Connections. A key saved here replaces the server's, so a team can run the Studio on its
    own accounts; stored on the volume (owner-only) and never shown back in full."""
    if request.method == "POST":
        updates = {k["env"]: request.form.get(k["env"], "") for k in api_keys.KEYS}
        clear = [k["env"] for k in api_keys.KEYS if request.form.get(f"clear_{k['env']}")]
        try:
            changed = api_keys.save(out(), updates, clear)
        except ValueError as e:
            flash(f"Nothing saved: {scrub_mixed(str(e))}")          # names the field and the rule, never the value
            return redirect(url_for("settings.settings_keys"))
        api_keys.apply(out())
        labels = {k["env"]: k["label"] for k in api_keys.KEYS}
        flash("Saved: " + ", ".join(labels[c] for c in changed) + ". The next run uses them."
              if changed else "Nothing changed.")
        anchor = "wordpress" if any(c.startswith("WP_") for c in changed) else "keys"
        return redirect(url_for("settings.settings_keys") + f"#{anchor}")
    from .. import meta as M
    from ..jobs import runner

    rows = api_keys.status(out())
    site = project_settings.wp_site(out())
    return render_template("configure/connections.html", keys=rows, tested=connections.recorded(out()), wp_site=site,
                           wp_set=all(r["source"] != "missing" for r in rows if r["env"] in api_keys.WP_GROUP),
                           author_id=author_for(project().publish),
                           links=M.link_index_status(project(), runner), system=_system())


def _system() -> list[tuple]:
    """This install: memory, the browser for screenshots, the storage for the articles."""
    from .. import meta as M

    mem = M.mem_limit_mb()
    rows = [("Memory", "not limited" if mem is None else f"{mem} MB" + (", enough" if mem >= M.MIN_MEM_MB else ", too little"),
             "green" if mem is None or mem >= M.MIN_MEM_MB else "red",
             "" if mem is None or mem >= M.MIN_MEM_MB else f"Writing and pictures need at least {M.MIN_MEM_MB} MB.")]
    rows.append(("The browser for screenshots", *_browser()))
    writable = _writable(out())
    rows.append(("Storage for the articles", "writable" if writable else "read only", "green" if writable else "red",
                 "Kept across releases when it is a mounted volume." if writable else "Nothing can be saved."))
    return rows


def _browser() -> tuple[str, str, str]:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        from pathlib import Path
        import os

        base = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "Library/Caches/ms-playwright")
        found = base.is_dir() and any(p.name.startswith("chromium") for p in base.iterdir())
        if not found:
            base = Path.home() / ".cache/ms-playwright"
            found = base.is_dir() and any(p.name.startswith("chromium") for p in base.iterdir())
    except Exception:  # noqa: BLE001
        return "not installed", "red", "Screenshots fall back to Scrapfly, or are skipped."
    return ("ready", "green", "") if found else ("not installed", "red", "Screenshots fall back to Scrapfly, or are skipped.")


def _writable(folder) -> bool:
    import os

    try:
        return os.access(folder, os.W_OK)
    except OSError:
        return False


@bp.route("/settings/keys/test/<env>", methods=["POST"])
def test_key(env):
    """One key asked of its service (free: it lists models, reads the account or sends a request
    the service refuses before any work), the answer kept until the key changes."""
    if env not in connections.TESTS:
        abort(404)
    connections.check(out(), env)
    return redirect(url_for("settings.settings_keys") + f"#key-{env}")


@bp.route("/settings/keys/test-all", methods=["POST"])
def test_all():
    """Every service that is set, tested at once (free)."""
    res = connections.fresh(out(), connections.check_all(out()))
    bad = [api_keys.label(n) for n, r in res.items() if r.get("ok") is False]
    moved = [api_keys.label(n) for n, r in res.items() if r.get("ok") is None]
    flash("Nothing to test: no key is set." if not res else
          f"Tested {len(res)}: " + ("all work." if not bad and not moved else "; ".join(filter(None, [
              "not working: " + ", ".join(bad) if bad else "",
              "changed during the test, test again: " + ", ".join(moved) if moved else ""])) + "."))
    return redirect(url_for("settings.settings_keys") + "#keys")


@bp.route("/settings/wordpress/test", methods=["POST"])
def test_wordpress():
    """The WordPress connection tried: the site's name, the user's role, its categories and
    authors (kept for the pickers), or the exact error."""
    site = connections.fresh(out(), {connections.WORDPRESS: connections.check(out(), connections.WORDPRESS)})[connections.WORDPRESS]
    flash({True: "Connected: ", False: "Not connected: ", None: "Not tested: the connection "}[site["ok"]]
          + scrub_mixed(site["detail"]))
    return redirect(url_for("settings.settings_keys") + "#wordpress")


@bp.route("/settings/wordpress/author", methods=["POST"])
def wordpress_author():
    try:
        project_settings.save_wordpress(out(), author_id=request.form.get("author_id", ""))
    except project_settings.Invalid as e:
        flash(f"Not saved: {e}.")
    else:
        flash("Saved: the next post carries this author.")
    return redirect(url_for("settings.settings_keys") + "#wordpress")


# ---------------------------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------------------------
@bp.route("/settings/project")
def settings_project():
    from ..shell import logo

    p = project()
    links = p.links or {}
    texts = [{**f, "changed": project_texts.is_overridden(out(), f["key"]),
              "preview": " ".join(project_texts.current(out(), f["key"]).split())[:160]}
             for f in project_texts.FIELDS]
    return render_template("configure/project.html", name=p.name, logo=logo(p), texts=texts,
                           name_changed=project_texts.is_overridden(out(), "project_name"),
                           # what is applied: the saved values over the profile's (an empty sitemap stays empty)
                           website={"domains": list(p.brand.domains if p.brand else []),
                                    "blog_base_url": links.get("blog_base_url") or "",
                                    "site_sitemap": links.get("site_sitemap") or "",
                                    "blog_sitemap": links.get("blog_sitemap") or ""},
                           palette=dict(p.brand.palette) if p.brand else {},
                           competitors=list(links.get("competitor_hosts") or []))


def _project_back(anchor: str):
    return redirect(url_for("settings.settings_project") + f"#{anchor}")


@bp.route("/settings/project/logo", methods=["POST"])
def project_logo():
    """Upload, replace or remove the logo (kept on the volume, `_brand/logo.*`)."""
    try:
        if request.form.get("action") == "remove":
            if project_settings.logo_path(out()):
                project_settings.remove_logo(out())
            else:
                project_settings.hide_logo(out())
            flash("The logo is removed.")
        else:
            f = request.files.get("logo")
            project_settings.save_logo(out(), f.read() if f else b"", f.filename if f else "")
            flash("The logo is saved: the sidebar, the login card and the next pictures use it.")
    except project_settings.Invalid as e:
        flash(f"Not saved: {e}.")
    return _project_back("identity")


@bp.route("/settings/project/website", methods=["POST"])
def project_website():
    try:
        project_settings.save_website(out(), domains=request.form.get("domains", ""),
                                      blog_base_url=request.form.get("blog_base_url", ""),
                                      site_sitemap=request.form.get("site_sitemap", ""),
                                      blog_sitemap=request.form.get("blog_sitemap", ""))
    except project_settings.Invalid as e:
        flash(f"Not saved: {e}.")
    else:
        flash("Saved. The next link index and the next article use it.")
    return _project_back("website")


@bp.route("/settings/project/palette", methods=["POST"])
def project_palette():
    try:
        project_settings.save_palette(out(), {k: request.form.get(k, "") for k in project_settings.PALETTE_KEYS})
    except project_settings.Invalid as e:
        flash(f"Not saved: {e}.")
    else:
        flash("Saved. The next picture uses these colours.")
    return _project_back("pictures")


@bp.route("/settings/project/competitors", methods=["POST"])
def project_competitors():
    try:
        hosts = project_settings.save_competitors(out(), request.form.get("hosts", ""))
    except project_settings.Invalid as e:
        flash(f"Not saved: {e}.")
    else:
        flash(f"Saved: {len(hosts)} competitor host{'s' if len(hosts) != 1 else ''}.")
    return _project_back("competitors")


@bp.route("/settings/project/<key>", methods=["GET", "POST"])
def settings_project_text(key):
    """One project text (the value of a {{slot}}): edit it, or restore the original."""
    field = next((f for f in project_texts.FIELDS if f["key"] == key), None)
    if field is None:
        abort(404)
    text, status = project_texts.current(out(), key), 200
    if request.method == "POST":
        if request.form.get("action") == "reset":
            project_texts.reset(out(), key)
            flash(f"Restored the original {field['label'].lower()}. The next run uses it.")
            return redirect(url_for("settings.settings_project_text", key=key))
        try:
            project_texts.save(out(), key, request.form.get("text", ""))
        except ValueError as e:
            flash(str(e))
            text, status = request.form.get("text", ""), 400
        else:
            flash("Saved. The next run and the Studio's own actions use it."
                  if project_texts.is_overridden(out(), key) else "This is the original text; nothing to save.")
            return redirect(url_for("settings.settings_project_text", key=key))
    prompts = [label for n, label, _h in prompt_overrides.AGENTS
               if key in prompt_overrides.placeholders(prompt_overrides.shipped(n))]
    return render_template("configure/project_text.html", field=field, text=text, prompts=prompts,
                           changed=project_texts.is_overridden(out(), key)), status


# ---------------------------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------------------------
# where the answer format sits in the text the page shows: the format itself is below, read only
FORMAT_HERE = "<!-- the answer format (shown below the text) goes here -->"


@bp.route("/settings/prompts")
def settings_prompts():
    rows = [{"name": n, "label": label, "help": h, "changed": prompt_overrides.is_overridden(out(), n)}
            for n, label, h in prompt_overrides.AGENTS]
    return render_template("configure/prompts.html", rows=rows)


@bp.route("/settings/prompts/<name>", methods=["GET", "POST"])
def settings_prompt(name):
    """One agent's prompt: the editable text, the slots it keeps, the answer format read only
    under it (it always runs as shipped); Restore original brings back the shipped prompt."""
    meta = {n: (label, h) for n, label, h in prompt_overrides.AGENTS}
    if name not in meta:
        abort(404)
    ship = prompt_overrides.shipped(name)
    block = prompt_overrides.contract(ship) or ""
    text, status = prompt_overrides.current(out(), name), 200
    if request.method == "POST":
        if request.form.get("action") == "reset":
            prompt_overrides.reset(out(), name)
            flash("Restored the original prompt. The next run uses it.")
            return redirect(url_for("settings.settings_prompt", name=name))
        typed = request.form.get("text", "").replace("\r\n", "\n")
        full = typed.replace(FORMAT_HERE, block) if FORMAT_HERE in typed else (
            prompt_overrides.with_shipped_contract(typed, ship) if block and not prompt_overrides.contract(typed) else typed)
        try:
            warnings = prompt_overrides.save(out(), name, full)
        except ValueError as e:
            flash(str(e))                          # shown with the text as typed, so nothing is lost
            text, status = typed, 400
        else:
            flash("Saved. The next run uses this prompt."
                  if prompt_overrides.is_overridden(out(), name) else "This is the original prompt; nothing to save.")
            for w in warnings:
                flash(w)
            return redirect(url_for("settings.settings_prompt", name=name))
    shown = text
    if block and status == 200:
        cur = prompt_overrides.contract(text)
        if cur:
            shown = text.replace(cur, FORMAT_HERE)
    labels = {f["key"]: f["label"] for f in project_texts.FIELDS}
    slots = [{"name": s, "code": project_texts.CODE_SLOTS.get(s), "label": labels.get(s)}
             for s in sorted(prompt_overrides.placeholders(ship))]
    return render_template("configure/prompt.html", name=name, label=meta[name][0], help=meta[name][1],
                           text=shown, changed=prompt_overrides.is_overridden(out(), name),
                           contract=block.strip(), slots=slots), status


# ---------------------------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------------------------
# the prices a team is likely to need to enter: what it pays for a plan nobody publishes a
# per-unit price for, and the ones that change on a vendor's promotion. Units and credits are
# stored per unit and entered per 1,000; model prices are stored and entered per million tokens,
# which is how the vendors quote them.
PRICE_FIELDS = [
    ("ahrefs|unit", "Ahrefs — $ per 1,000 API units", 1000,
     "API units come with the Ahrefs subscription. Leave empty to report units only; to spread "
     "the plan's cost, enter the monthly price divided by the monthly unit limit, times 1,000."),
    ("winston|credit", "Winston AI — $ per 1,000 credits", 1000,
     "A plagiarism check costs 2 credits per word. Enter the price of the credit pack divided "
     "by its credits, times 1,000."),
    ("scrapfly|credit", "Scrapfly — $ per 1,000 credits", 1000,
     "Discovery plan: $30 for 200,000 credits = $0.15 per 1,000."),
    ("openai|gpt-5.6-sol|input", "GPT-5.6-Sol — $ per million input tokens", 1,
     "The model prices in the table are per million tokens already."),
    ("openai|gpt-5.6-sol|output", "GPT-5.6-Sol — $ per million output tokens", 1, ""),
    ("parallel|task|pro", "Parallel deep research (pro) — $ per run", 1, ""),
]


# a price's place in the table: "provider|model|field" (model names carry dots, "gpt-5.6-sol")
def _get(d: dict, path: str):
    for part in path.split("|"):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _set(d: dict, path: str, value) -> None:
    parts = path.split("|")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    if value is None:
        d.pop(parts[-1], None)
    else:
        d[parts[-1]] = value


@bp.route("/settings/prices", methods=["GET", "POST"])
def settings_prices():
    """The prices the cost report uses. A saved price reprices every call recorded without one."""
    saved = pricing.saved_overrides(out())
    if request.method == "POST":
        new = dict(saved)
        errors = []
        for key, label, scale, _help in PRICE_FIELDS:
            raw = (request.form.get(key) or "").strip().replace(",", ".").lstrip("$")
            if not raw:
                _set(new, key, None)
                continue
            try:
                v = float(raw)
            except ValueError:
                errors.append(f"{label}: '{raw}' is not a number.")
                continue
            if v < 0:
                errors.append(f"{label}: a price cannot be negative.")
                continue
            _set(new, key, v / scale)
        if errors:
            for e in errors:
                flash(e)
        else:
            pricing.save_overrides(out(), new)
            flash("Saved. The cost report uses these prices from now on.")
            return redirect(url_for("settings.settings_prices"))
    from core.config import runtime_config

    table = pricing.table(runtime_config(CONFIG, project()), out())
    rows = []
    for key, label, scale, help_ in PRICE_FIELDS:
        v = _get(table, key)
        rows.append({"key": key, "label": label, "help": help_,
                     "value": "" if v is None else f"{v * scale:g}",
                     "changed": _get(saved, key) is not None})
    return render_template("configure/prices.html", rows=rows, table=table)


# ---------------------------------------------------------------------------------------------
# who may open the Studio (core/access.py; the operator, 2026-10-07)
# ---------------------------------------------------------------------------------------------
@bp.route("/settings/access", methods=["GET", "POST"])
def settings_access():
    """The Studio's password: changed, turned off, or set again. The current password first when
    one is in force; a change signs every other browser out."""
    from core import access

    root = out()
    if request.method == "POST":
        what, current = request.form.get("what"), request.form.get("current") or ""
        new, again = request.form.get("new") or "", request.form.get("again") or ""
        try:
            secret = current_app.secret_key
            if what == "off":
                if request.form.get("sure") != "1":
                    raise access.Refused("tick that anyone who reaches the Studio can use it")
                access.turn_off(root, current, secret=secret)
                session.pop("ok", None)
                flash("The password is off: anyone who reaches the Studio can use it.")
            elif what == "password":
                if new != again:
                    raise access.Refused("the two new passwords differ")
                session["ok"], session["access"] = True, access.set_password(root, new, current, secret=secret)
                flash("Saved: the new password is needed from now on; other browsers sign in again.")
            else:
                raise access.Refused("nothing to do")
        except access.Refused as e:
            flash(f"Nothing changed: {e}.")
        return redirect(url_for("settings.settings_access"))
    return render_template("configure/access.html", mode=access.mode(root), source=access.source(root),
                           min_length=access.MIN_LENGTH)


# ---------------------------------------------------------------------------------------------
# the setup: five steps, each skippable, offered until the install can write (core/setup.py)
# ---------------------------------------------------------------------------------------------
SETUP_STEPS = ("The project", "Its texts", "Keys", "WordPress", "Done")
SETUP_TEXTS = ("brand_voice", "product_facts", "editorial_rules")


@bp.route("/setup")
def setup_page():
    import os

    from core import setup

    from ..shell import logo

    step = request.args.get("step", "1")
    step = int(step) if step.isdigit() and 1 <= int(step) <= len(SETUP_STEPS) else 1
    p = project()
    data = project_settings.load(out())
    web = data.get("website") or {}
    fields = {f["key"]: f for f in project_texts.FIELDS}
    return render_template("configure/setup.html", step=step, names=SETUP_STEPS, project=p, logo=logo(p),
                           website={"domains": web.get("domains") or list(p.brand.domains if p.brand else []),
                                    "blog_base_url": web.get("blog_base_url") or (p.links or {}).get("blog_base_url")},
                           audience_line=" ".join(str(p.audience or "").split())[:200],
                           texts=[{**fields[k], "text": project_texts.current(out(), k)} for k in SETUP_TEXTS],
                           keys=api_keys.status(out()), tests=connections.recorded(out()),
                           site=project_settings.wp_site(out()),
                           wp_url=os.getenv("WP_BASE_URL", ""), wp_user=os.getenv("WP_USERNAME", ""),
                           wp_set=bool(os.getenv("WP_APP_PASSWORD")), author_id=author_for(p.publish),
                           category=(p.seo.get("publishing") or {}).get("category") if p.seo else None,
                           state=setup.state(out()))


def _setup_report(rep: dict, step: int):
    for e in rep["errors"]:
        flash(f"Not saved: {scrub_mixed(e)}.")
    return redirect(url_for("settings.setup_page", step=step))


@bp.route("/setup/project", methods=["POST"])
def setup_project():
    import tempfile
    from pathlib import Path

    from core import setup

    f = request.files.get("logo")
    logo_file = None
    if f and f.filename:
        tmp = Path(tempfile.mkdtemp(prefix="setup-logo-")) / Path(f.filename).name
        tmp.write_bytes(f.read())
        logo_file = tmp
    rep = setup.apply(out(), name=request.form.get("name") or None, logo=logo_file,
                      domain=request.form.get("domain") or None, blog=request.form.get("blog") or None,
                      audience=request.form.get("audience") or None, test=False)
    if logo_file:
        logo_file.unlink(missing_ok=True)
    return _setup_report(rep, 1 if rep["errors"] else 2)


@bp.route("/setup/texts", methods=["POST"])
def setup_texts():
    """The brand voice, the product facts and the editorial rules: each may stay empty."""
    from core import setup

    # a field sent empty clears its text; a field not sent leaves it
    rep = setup.apply(out(), texts={k: request.form[k] for k in SETUP_TEXTS if k in request.form}, test=False)
    return _setup_report(rep, 2 if rep["errors"] else 3)


@bp.route("/setup/keys", methods=["POST"])
def setup_keys():
    """The keys typed here saved and tested; the ones set before (the server's, or saved earlier)
    tested too when they have not passed yet. Every test is free."""
    from core import setup

    keys = {k["env"]: request.form.get(k["env"], "") for k in api_keys.KEYS if not k["env"].startswith("WP_")}
    rep = setup.apply(out(), keys=keys)
    if not rep["errors"]:
        connections.check_all(out(), only_untested=True)
    return _setup_report(rep, 3)


@bp.route("/setup/wordpress", methods=["POST"])
def setup_wordpress():
    from core import setup

    rep = setup.apply(out(), wp_url=request.form.get("wp_url") or None, wp_user=request.form.get("wp_user") or None,
                      wp_password=request.form.get("wp_password") or None)
    if not rep["tests"] and not rep["errors"]:     # nothing new typed: test what is set
        connections.check(out(), connections.WORDPRESS)
    return _setup_report(rep, 4)


@bp.route("/setup/wordpress/pick", methods=["POST"])
def setup_wordpress_pick():
    from core import setup

    # an empty pick is a choice (the user above; no category), an absent field is not
    rep = setup.apply(out(), author_id=request.form.get("author_id"), category=request.form.get("category"), test=False)
    return _setup_report(rep, 4 if rep["errors"] else 5)


@bp.route("/setup/done", methods=["POST"])
def setup_done():
    return redirect("/new" if request.form.get("next") == "new" else url_for("articles.dashboard"))
