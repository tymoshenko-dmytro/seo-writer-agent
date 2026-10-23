#!/usr/bin/env python3
"""SEO Writer Agent from the command line: the same engine and the same checks as the web
interface, so a person or their agent (Claude Code, Codex) can run the whole Studio from here.
Every action of the interface is a command; `--json` prints one object an agent can read.

    python cli.py generate "how to choose a project management tool" --yes
    python cli.py list --json
    python cli.py status <slug> --json            # the version an action needs
    python cli.py findings <slug> --json
    python cli.py apply <slug> r1-3 --version <v> --review r1 --yes
"""
from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from core.orchestrator import generate_article  # noqa: E402  (after load_dotenv)
from core.store import output_root  # noqa: E402


def _print_cost():
    """What this command spent, from the cost ledger (core/usage.py)."""
    from core import usage

    s = usage.summarize(usage.PROCESS)
    parts = ", ".join(f"{c['label']} ${c['cost']:.2f}" for c in s["by_category"])
    print(f"Cost   : ${s['total']:.2f}" + (f" ({parts})" if parts else "")
          + ("; not priced: " + ", ".join(f"{u['provider']} {u['units']}" for u in s["unpriced"])
             if s["unpriced"] else ""))


# ---------------------------------------------------------------------------------------------
# the agent's door: every action of
# the interface as a command, the same functions and checks, one JSON object with --json
# ---------------------------------------------------------------------------------------------
class Refusal(SystemExit):
    """An action that changed nothing, with the same reason the page gives."""


def _emit(args, data: dict, lines=()) -> None:
    import json
    import sys

    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        for ln in lines:
            print(ln)


def _refuse(args, kind: str, reason: str, **extra) -> None:
    """Print the refusal (as JSON with --json) and exit 1: nothing was changed."""
    _emit(args, {"ok": False, "refused": kind, "reason": reason, **extra}, [f"Nothing done ({kind}): {reason}"])
    raise Refusal(1)


def _context():
    """The engine as the Studio sees it: the configured output folder, the project, the keys
    saved in the Studio, the price table."""
    from core import api_keys, pricing, store
    from core.config import load_core_config, load_project, runtime_config

    cfg = load_core_config()
    root = store.output_root(cfg)
    api_keys.apply(root)
    proj = load_project(core=cfg)
    return cfg, proj, root, pricing.table(runtime_config(cfg, proj), root)


def _article(args, root):
    from core import store

    try:
        return store.Repository(root).get(args.slug, listed_only=True)
    except KeyError:
        _refuse(args, "unknown", f"no article '{args.slug}' here")


def _paid(args, root, prices, op: str | None, label: str, est: dict | None = None) -> dict:
    """A command that spends money says what it is likely to cost first (`est`, else the estimate
    of `op`). Without a terminal it needs --yes (an agent states the cost to its person, then
    runs it); in a terminal it asks."""
    import sys

    from core.agent_door import estimate

    est = est or estimate(root, op, prices)
    cost = f"about ${est['usd']:.2f}" if est["usd"] is not None else "money"
    if getattr(args, "yes", False):
        return est
    if not sys.stdin.isatty():
        _refuse(args, "needs_yes", f"{label} costs {cost} ({est['basis']}): run it again with --yes", estimate=est)
    answer = input(f"{label}: {cost} ({est['basis']}). Go on? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        _refuse(args, "declined", "not confirmed", estimate=est)
    return est


def _acting(args, fn):
    """Run an action; a stale page, a job holding the article or a refused change become the
    same refusal the page shows."""
    from core import store
    from core.qa_changes import Refused, Stale

    try:
        return fn()
    except Stale as e:
        _refuse(args, "stale", str(e))
    except store.Busy as e:
        _refuse(args, "busy", str(e))
    except store.CorruptFile as e:                 # the record names itself, never its path
        _refuse(args, "unreadable", str(e))
    except Refused as e:
        _refuse(args, "refused", str(e))
    except ValueError as e:                        # settings no action can work with (qa.reads)
        _refuse(args, "refused", str(e))
    except KeyError as e:
        _refuse(args, "unknown", str(e).strip("'\""))


def _agent_command(args) -> bool:
    """The agent's door; True when `args.cmd` was one of its commands."""
    from core import agent_door, publication, versions
    from core import qa_changes as qc
    from core.qa_review import review

    cmd = args.cmd
    if cmd == "list":
        cfg, proj, root, prices = _context()
        from core import store

        rows = [agent_door.facts(a, prices) for a in store.Repository(root).articles()]
        _emit(args, {"ok": True, "articles": rows},
              [f"  {r['slug']:<48} review {((r['review'] or {}).get('needs_you', '—'))!s:>3} need you · "
               f"{r['accepted']['state']:<14} · WP {r['wordpress'].get('post_id') or '—'} {r['wordpress'].get('status') or ''}"
               for r in rows])
        return True
    if cmd in ("status", "findings", "versions", "compare"):
        cfg, proj, root, prices = _context()
        art = _article(args, root)
        if cmd == "status":
            f = agent_door.facts(art, prices)
            _emit(args, {"ok": True, **f}, [f"{k}: {v}" for k, v in f.items()])
        elif cmd == "findings":
            v = agent_door.review_view(art)
            lines = [f"review {(v.get('review') or {}).get('id') or '—'} · score {v.get('score')}"]
            # what needs the person in the Review page's order, a half-done change among them
            lines += [f"  needs you  {i['id']:<8} {i['type']:<9} {i['text']}" for i in v.get("queue") or []]
            lines += [f"  changed    {g['id']:<8} {'reverts now' if g['reverts_now'] else ''}"
                      for g in v.get("changed") or [] if not g["half_done"]]
            _emit(args, {"ok": True, "version": art.version(), **v}, lines)
        elif cmd == "versions":
            vs = versions.listing(art)
            _emit(args, {"ok": True, "version": art.version(), "versions": vs},
                  [f"  v{x['n']:<4} {x['at']}  {x['what']}{'  (now)' if x['current'] else ''}" for x in vs])
        else:
            c = _acting(args, lambda: versions.compare(art, args.n, args.to))
            _emit(args, {"ok": True, **c}, [f"--- {b['before']}\n+++ {b['after']}" for b in c["blocks"]])
        return True
    if cmd in ("seo", "cover", "redraw", "upload-picture"):
        from core import article_edits

        cfg, proj, root, prices = _context()
        art = _article(args, root)
        est = None
        if cmd == "seo":
            import json as _json

            fm = art.read()[0]
            try:
                faq = _json.loads(args.faq) if args.faq is not None else (fm.get("faq") or [])
            except ValueError as e:
                _refuse(args, "refused", f"--faq is not JSON: {e}")
            try:
                out = _acting(args, lambda: article_edits.save_seo(
                    art, args.version, who="the CLI", faq=faq,
                    meta_title=args.meta_title if args.meta_title is not None else fm.get("meta_title") or "",
                    meta_description=(args.meta_description if args.meta_description is not None
                                      else fm.get("meta_description") or fm.get("description") or ""),
                    slug=args.new_slug, focus_keyword=args.focus_keyword))
            except article_edits.BadField as e:
                _refuse(args, "refused", str(e))
        elif cmd == "upload-picture":
            from pathlib import Path as _Path

            src = _Path(args.file)
            if not src.is_file():
                _refuse(args, "refused", f"no file at {args.file}")
            try:
                out = _acting(args, lambda: article_edits.upload_picture(
                    art, args.version, args.asset, src.read_bytes()[:article_edits.UPLOAD_MAX + 1], who="the CLI"))
            except article_edits.BadField as e:
                _refuse(args, "refused", str(e))
        elif cmd == "cover":
            est = _paid(args, root, prices, "cover", f"A new cover for '{art.slug}'")
            out = _acting(args, lambda: article_edits.new_cover(art, args.version, cfg, proj, who="the CLI"))
        else:
            est = _paid(args, root, prices, "visual_edit", f"{args.asset} made again")
            try:
                out = _acting(args, lambda: article_edits.redraw(art, args.version, args.asset, cfg, proj,
                                                                 feedback=args.feedback or "", who="the CLI",
                                                                 **({"kind": args.kind} if args.kind else {})))
            except article_edits.BadField as e:
                _refuse(args, "refused", str(e))
        _emit(args, {"ok": True, "result": out, "estimate": est, "version": art.version()}, [f"done: {out}"])
        return True
    if cmd in ("review", "apply", "fix-again", "not-important", "revert", "keep", "edit", "restore", "accept",
               "remove-picture"):
        cfg, proj, root, prices = _context()
        art = _article(args, root)
        est = None
        if cmd == "review":
            est = _paid(args, root, prices, "review", "Review again")
            out = _acting(args, lambda: review(art.slug, config=cfg, project=proj, version=args.version))
        elif cmd == "apply":
            # each ticked item at its own action's price, as the Review page shows them (RA54-05)
            ticked = [i for i in agent_door.review_view(art).get("needs_you") or [] if i["id"] in args.ids]
            est = _paid(args, root, prices, None, f"Make {len(args.ids)} change(s)",
                        est=agent_door.batch_estimate(root, ticked, prices))
            out = _acting(args, lambda: qc.apply_proposals(art.slug, args.ids, args.version, args.review, config=cfg, project=proj))
        elif cmd == "fix-again":
            est = _paid(args, root, prices, "fix", "Try again")
            out = _acting(args, lambda: qc.fix_again(art.slug, args.fid, args.version, config=cfg, project=proj))
        elif cmd == "not-important":
            out = _acting(args, lambda: qc.not_important(art.slug, args.fid, args.version, "the CLI", args.why, config=cfg, project=proj))
        elif cmd == "revert":
            out = _acting(args, lambda: qc.revert(art.slug, args.group, args.version, config=cfg, project=proj, who="the CLI"))
        elif cmd == "keep":
            out = _acting(args, lambda: qc.keep_as_is(art.slug, args.group, args.version, "the CLI", config=cfg, project=proj))
        elif cmd == "edit":
            out = _acting(args, lambda: qc.edit_block(art.slug, args.version, args.before, args.after, fid=args.finding,
                                                      who="the CLI", config=cfg, project=proj))
        elif cmd == "restore":
            out = _acting(args, lambda: versions.restore(art, args.n, args.version, "the CLI"))
        elif cmd == "remove-picture":
            out = _acting(args, lambda: qc.remove_picture_by_person(art.slug, args.version, args.asset, who="the CLI",
                                                                    config=cfg, project=proj))
        else:
            action = None if args.action in (None, "none") else args.action
            out = _acting(args, lambda: publication.accept(art.slug, args.version, action, "the CLI", config=cfg,
                                                           project=proj, post=args.post))
        _emit(args, {"ok": True, "result": out, "estimate": est, "version": art.version()},
              [f"done: {out}"] + ([f"cost: about ${est['usd']:.2f} ({est['basis']})"] if est and est["usd"] is not None else []))
        return True
    return False


def _configure_command(args) -> bool:
    """Configure from the shell:
    setup, doctor, settings, prompts, project, access. The same modules the Configure pages write."""
    import os
    import sys

    from core import api_keys, connections, project_settings, project_texts, prompt_overrides
    from core import settings as user_settings
    from core import setup
    from core.config import load_core_config, load_project, runtime_config
    from core.store import output_root

    cmd = args.cmd
    if cmd not in ("setup", "doctor", "settings", "prompts", "project", "access", "sample"):
        return False
    cfg = load_core_config()
    root = output_root(cfg)
    if cmd == "sample":
        from core import sample

        try:
            slug = sample.add(root)
        except (FileNotFoundError, OSError, ValueError) as e:
            _refuse(args, "refused", f"the sample could not be added: {e}")
        _emit(args, {"ok": True, "slug": slug}, [f"  the sample article: {slug} (cli.py status {slug})"])
        return True
    if cmd == "access":
        # who may open the Studio (core/access.py): run on the server, so no current password
        from core import access

        if args.off and args.password_stdin:
            _refuse(args, "refused", "either --off or --password-stdin")
        try:
            if args.off:
                if not args.yes:
                    _refuse(args, "refused", "--off opens the Studio to anyone who reaches it: add --yes")
                access.turn_off(root, check_current=False)
            elif args.password_stdin:
                access.set_password(root, sys.stdin.readline().rstrip("\n"), check_current=False)
        except access.Refused as e:
            _refuse(args, "refused", str(e))
        except OSError as e:                # a data folder it cannot write
            _refuse(args, "refused", f"the choice could not be saved in {root}: {e.strerror or e}")
        now = {"mode": access.mode(root), "source": access.source(root)}
        _emit(args, {"ok": True, **now}, [f"  {now['mode']}" + (f" ({now['source']})" if now["source"] else "")])
        return True
    if cmd == "setup":
        keys = {env: getattr(args, flag) for env, flag in SETUP_KEY_FLAGS.items() if getattr(args, flag, None)}
        if args.logo and not Path(args.logo).is_file():
            _refuse(args, "refused", f"no file at {args.logo}")
        texts = {}
        for key in ("brand_voice", "product_facts", "editorial_rules"):
            path = getattr(args, f"{key}_file")
            if path:
                if not Path(path).is_file():
                    _refuse(args, "refused", f"no file at {path}")
                texts[key] = Path(path).read_text(encoding="utf-8")
        rep = setup.apply(root, name=args.name, logo=Path(args.logo) if args.logo else None, domain=args.domain,
                          blog=args.blog, audience=args.audience, texts=texts, keys=keys, wp_url=args.wp_url,
                          wp_user=args.wp_user, wp_password=args.wp_password, author_id=args.author,
                          category=args.category, test=not args.no_test)
        lines = [f"  saved: {', '.join(rep['saved']) or 'nothing'}"]
        lines += [f"  test {k}: {'ok' if v.get('ok') else 'not ok' if v.get('ok') is False else 'not tested'} ({v.get('detail')})"
                  for k, v in rep["tests"].items()]
        lines += [f"  not saved: {e}" for e in rep["errors"]]
        if rep["state"]["reason"]:
            lines.append(f"  {rep['state']['reason']}")
        _emit(args, {"ok": not rep["errors"], **rep}, lines)
        if rep["errors"]:
            raise Refusal(1)
        return True
    if cmd == "doctor":
        api_keys.apply(root)
        if not args.no_test:
            connections.check_all(root)
        checks = []
        writable = os.access(root, os.W_OK) if root.exists() else False
        checks.append({"check": "storage", "ok": writable, "required": True,
                       "detail": "the articles' folder is writable" if writable else "the articles' folder cannot be written"})
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                p.chromium.launch().close()
            checks.append({"check": "browser", "ok": True, "required": True, "detail": "Chromium starts"})
        except Exception as e:  # noqa: BLE001
            checks.append({"check": "browser", "ok": False, "required": True,
                           "detail": f"Chromium does not start ({type(e).__name__}): the infographics, the logo and "
                                     f"the local screenshots need it (playwright install chromium)"})
        # the services last, from the answers about the credentials in force NOW: a key replaced
        # during any check above is untested, and the result says so
        api_keys.apply(root)
        tested = connections.recorded(root)
        st = setup.state(root)
        services = []
        for name in (*connections.REQUIRED, connections.WORDPRESS):
            set_ = (all(os.getenv(e) for e in api_keys.WP_GROUP) if name == connections.WORDPRESS
                    else bool(os.getenv(name)))
            res = tested.get(name) or {}
            if not set_:
                ok, detail = False, "not set"
            elif not res:
                ok, detail = False, ("set, not tested (run doctor without --no-test)" if args.no_test
                                     else "changed during the test, so not tested: run doctor again")
            else:
                ok, detail = res.get("ok") is True, res.get("detail") or ""
            services.append({"check": api_keys.label(name), "ok": ok, "required": True, "detail": detail})
        project = {"check": "project", "ok": st["project"], "required": True,
                   "detail": "the name and the site's domain are set" if st["project"] else
                   "the project's name and the site's domain are needed (cli.py setup --name --domain)"}
        checks = services + [project] + checks
        bad = [c for c in checks if c["required"] and not c["ok"]]
        ok = not bad and st["done"]          # the state read with them agrees, or it is not a pass
        _emit(args, {"ok": ok, "checks": checks, "state": st},
              [f"  {'ok ' if c['ok'] else 'NO '} {c['check']:<12} {c['detail']}" for c in checks]
              + ([] if ok or bad else [f"  NO  {st['reason'] or 'a service changed during the test: run doctor again'}"]))
        if not ok:
            raise Refusal(1)
        return True
    if cmd == "settings":
        base = load_project(core=cfg, apply_user_settings=False).seo
        current = user_settings.resolved(base, root)
        if args.action == "get":
            keys = args.pairs or [f["key"] for f in user_settings.FIELDS]
            unknown = [k for k in keys if k not in current]
            if unknown:
                _refuse(args, "unknown", f"no setting named {', '.join(unknown)}")
            _emit(args, {"ok": True, "settings": {k: current[k] for k in keys}}, [f"  {k} = {current[k]}" for k in keys])
            return True
        if args.action == "reset":
            keys = args.pairs or [f["key"] for f in user_settings.FIELDS]
            errs = user_settings.save_values(root, {}, base, keys=keys)
        else:
            pairs = dict(p.split("=", 1) for p in args.pairs if "=" in p)
            if not pairs or len(pairs) != len(args.pairs):
                _refuse(args, "refused", "settings set takes key=value pairs")
            errs = user_settings.save_values(root, pairs, base)
        if errs:
            _refuse(args, "refused", "; ".join(errs))
        current = user_settings.resolved(base, root)
        _emit(args, {"ok": True, "settings": current}, ["  saved"])
        return True
    if cmd == "prompts":
        names = {n: (label, h) for n, label, h in prompt_overrides.AGENTS}
        if args.action == "list":
            rows = [{"name": n, "label": label, "edited": prompt_overrides.is_overridden(root, n)}
                    for n, (label, _h) in names.items()]
            _emit(args, {"ok": True, "prompts": rows},
                  [f"  {r['name']:<20} {'edited' if r['edited'] else 'original'}  {r['label']}" for r in rows])
            return True
        if args.name not in names:
            _refuse(args, "unknown", f"no prompt named {args.name}")
        if args.action == "show":
            text = prompt_overrides.current(root, args.name)
            _emit(args, {"ok": True, "name": args.name, "text": text, "edited": prompt_overrides.is_overridden(root, args.name)},
                  [text])
        elif args.action == "reset":
            prompt_overrides.reset(root, args.name)
            _emit(args, {"ok": True, "name": args.name, "edited": False}, ["  restored the original"])
        else:
            if not args.file or not Path(args.file).is_file():
                _refuse(args, "refused", "prompts set needs --file with the prompt's text")
            try:
                warns = prompt_overrides.save(root, args.name, Path(args.file).read_text(encoding="utf-8"))
            except ValueError as e:
                _refuse(args, "refused", str(e))
            _emit(args, {"ok": True, "name": args.name, "edited": prompt_overrides.is_overridden(root, args.name),
                         "warnings": warns}, ["  saved"] + [f"  note: {w}" for w in warns])
        return True
    # project
    try:
        if args.text:
            if args.reset_text:
                project_texts.reset(root, args.text)
            elif args.value is not None or args.file:
                project_texts.save(root, args.text, args.value if args.value is not None else Path(args.file).read_text(encoding="utf-8"))
        if args.logo:
            project_settings.save_logo(root, Path(args.logo).read_bytes(), Path(args.logo).name)
        if args.remove_logo:
            project_settings.remove_logo(root) if project_settings.logo_path(root) else project_settings.hide_logo(root)
        if any(v is not None for v in (args.domains, args.blog, args.site_sitemap, args.blog_sitemap)):
            project_settings.save_website(root, domains=args.domains, blog_base_url=args.blog,
                                          site_sitemap=args.site_sitemap, blog_sitemap=args.blog_sitemap)
        if args.palette:
            cur = dict(project_settings.load(root).get("palette") or {})
            cur.update(dict(p.split("=", 1) for p in args.palette if "=" in p))
            project_settings.save_palette(root, cur)
        if args.competitors is not None:
            project_settings.save_competitors(root, args.competitors)
    except (ValueError, KeyError, OSError) as e:
        _refuse(args, "refused", str(e).strip("'\""))
    proj = load_project(core=cfg)
    facts = {"name": proj.name, "domains": list(proj.brand.domains) if proj.brand else [],
             "blog": (proj.links or {}).get("blog_base_url"), "site_sitemap": (proj.links or {}).get("site_sitemap"),
             "blog_sitemap": (proj.links or {}).get("blog_sitemap"),
             "logo": bool(proj.brand and proj.brand.logo_file()), "palette": dict(proj.brand.palette) if proj.brand else {},
             "competitors": list((proj.links or {}).get("competitor_hosts") or []),
             "texts": {f["key"]: {"edited": project_texts.is_overridden(root, f["key"])} for f in project_texts.FIELDS}}
    if args.text and not (args.reset_text or args.value is not None or args.file):
        facts["text"] = project_texts.current(root, args.text)
    _emit(args, {"ok": True, **facts}, [f"  {k}: {v}" for k, v in facts.items() if k != "texts"])
    return True


# the setup's key flags: the environment name each fills
SETUP_KEY_FLAGS = {"ANTHROPIC_API_KEY": "anthropic_key", "OPENAI_API_KEY": "openai_key", "PARALLEL_API_KEY": "parallel_key",
                   "AHREFS_API_KEY": "ahrefs_key", "SCRAPFLY_KEY": "scrapfly_key", "WINSTON_API_KEY": "winston_key"}


def main():
    ap = argparse.ArgumentParser(prog="cli.py", description="SEO Writer Agent: research, write, review and publish "
                                 "blog articles to WordPress, from the command line.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate one article from a topic")
    g.add_argument("topic")
    g.add_argument("--no-ahrefs", action="store_true", help="skip the Ahrefs SEO step")
    g.add_argument("--words", type=int, help="target word count")
    g.add_argument("--processor", help="parallel.ai processor (lite/core/pro/ultra)")
    g.add_argument("--reuse-from", metavar="SLUG",
                   help="re-run an existing article from the WRITER on its cached keyword "
                        "research, brief and research corpus (no Ahrefs, no deep research). "
                        "Writes to a NEW directory unless --in-place is given")
    g.add_argument("--out", metavar="SLUG",
                   help="with --reuse-from: output directory name (default "
                        "<slug>--rerun-<timestamp>); must not exist yet. It also becomes the "
                        "article's slug, i.e. its WordPress slug if the run is published")
    g.add_argument("--in-place", action="store_true",
                   help="with --reuse-from: overwrite the source directory on purpose (keeps "
                        "its WordPress binding) - never use on a frozen testbed")
    g.add_argument("--yes", action="store_true", help="go on without asking (a run costs money: see the estimate)")
    g.add_argument("--json", action="store_true", help="print one JSON object at the end")

    rl = sub.add_parser("refresh-links", help="rebuild the internal-link index from the sitemaps")
    rl.add_argument("--show", type=int, default=0, help="print the first N targets")
    rl.add_argument("--json", action="store_true", help="print one JSON object")


    kw = sub.add_parser("keywords", help="keyword coverage report for an article (or all of them)")
    kw.add_argument("slug", nargs="?", help="folder name under project/output/; omit for all")
    kw.add_argument("--quiet", action="store_true", help="one line per article, no detail")
    kw.add_argument("--json", action="store_true", help="print one JSON object")

    pl = sub.add_parser("uniqueness", help="plagiarism / uniqueness check for an article")
    pl.add_argument("slug", help="folder name under project/output/")
    pl.add_argument("--dry-run", action="store_true",
                    help="show what would be sent and what it would cost; calls no API")
    pl.add_argument("--yes", action="store_true", help="go on without asking (it costs Winston credits)")
    pl.add_argument("--json", action="store_true", help="print one JSON object")

    co = sub.add_parser("costs", help="what an article cost (or every article), by part and service")
    co.add_argument("slug", nargs="?", help="folder name under the output folder; omit for all")
    co.add_argument("--json", action="store_true", help="print one JSON object")

    vc = sub.add_parser("visual-check", help="look at every picture of an article with a vision model and "
                                             "print what is wrong (changes nothing; ~$0.01 a picture)")
    vc.add_argument("slug")
    vc.add_argument("--yes", action="store_true", help="go on without asking (it costs money: see the estimate)")
    vc.add_argument("--json", action="store_true", help="print one JSON object")

    cl = sub.add_parser("cleanup", help="apply the picture rules in code (free): linked captions under "
                                        "our own screenshots, no empty charts, no pictures in a row")
    cl.add_argument("slug")
    cl.add_argument("--json", action="store_true", help="print one JSON object")

    ex = sub.add_parser("export", help="pack an article folder into a zip for another install")
    ex.add_argument("slug")
    ex.add_argument("--out", help="zip path (default <slug>.zip here)")
    ex.add_argument("--json", action="store_true", help="print one JSON object")

    im = sub.add_parser("import", help="unpack an exported article into a new folder")
    im.add_argument("zip", help="the exported zip")
    im.add_argument("--as", dest="as_slug", help="folder name (default: the exported one, -2 if taken)")
    im.add_argument("--json", action="store_true", help="print one JSON object")

    mq = sub.add_parser("migrate-qa", help="show what the upgrade to the QA model would make of each article "
                                           "(changes nothing; no WordPress request, no model call)")
    mq.add_argument("slug", nargs="*", help="folder names under the output folder; omit for all")
    mq.add_argument("--apply", action="store_true", help="migrate them (the Studio does this when it starts)")
    mq.add_argument("--json", action="store_true", help="print one JSON object")

    pub = sub.add_parser("publish", help="send the article to WordPress: a new post or the draft it has; a "
                                         "live post only with --update-live")
    pub.add_argument("slug", help="folder name under the output folder")
    pub.add_argument("--category", default=None, help="WP category name to match (never created)")
    st = pub.add_mutually_exclusive_group()
    st.add_argument("--live", action="store_true", help="publish it on the blog (a new post or the draft)")
    st.add_argument("--update-live", action="store_true", help="update the post that is live on the blog")
    st.add_argument("--to-draft", action="store_true",
                    help="take the live post off the blog: it becomes a draft")

    pub.add_argument("--json", action="store_true", help="print one JSON object")
    ws = sub.add_parser("wp-status", help="read the article's post status from WordPress (changes nothing there)")
    ws.add_argument("slug")
    ws.add_argument("--json", action="store_true", help="print one JSON object")

    # the agent's door: every action of the interface as a command, --json for an agent to read
    def door(name, help_, *, slug=True, version=False, paid=False):
        p = sub.add_parser(name, help=help_)
        if slug:
            p.add_argument("slug", help="the article's folder name")
        if version:
            p.add_argument("--version", required=True, help="the version the decision is about (from status --json)")
        if paid:
            p.add_argument("--yes", action="store_true", help="go on without asking (it costs money: see the estimate)")
        p.add_argument("--json", action="store_true", help="print one JSON object")
        return p

    door("list", "every article with its three facts: the review, the acceptance, WordPress", slug=False)
    door("status", "one article's facts: the version, the review, the acceptance, WordPress, the cost")
    door("findings", "the latest review: what needs you, what the machine changed, what is closed")
    door("review", "review the article again (paid: its estimate first)", version=True, paid=True)
    ap_apply = door("apply", "apply proposals of the latest review", version=True, paid=True)
    ap_apply.add_argument("ids", nargs="+", help="the proposals' ids")
    ap_apply.add_argument("--review", required=True, help="the review they belong to (from findings --json)")
    door("fix-again", "try a safe change that did not land once more", version=True, paid=True).add_argument("fid")
    ni = door("not-important", "close an open finding as not important, with why", version=True)
    ni.add_argument("fid")
    ni.add_argument("--why", default="", help="a note, if you want one")
    door("revert", "undo the latest change while nothing changed after it, or a picture as it was made", version=True).add_argument("group")
    door("keep", "keep as it is a change the check says left its problem in place", version=True).add_argument("group")
    ed = door("edit", "your own words for one paragraph (a review card's Edit it yourself)", version=True)
    ed.add_argument("--before", required=True, help="the paragraph as it reads now")
    ed.add_argument("--after", required=True, help="the paragraph as it should read")
    ed.add_argument("--finding", help="the open finding it answers (closed as changed, by you)")
    door("versions", "every version of the article, newest first")
    cp = door("compare", "what changed from version N to version M (or to now)")
    cp.add_argument("n", type=int)
    cp.add_argument("--to", type=int, default=None)
    door("restore", "write version N back as the newest version", version=True).add_argument("n", type=int)
    door("remove-picture", "take a picture and its caption out of the text (a version)", version=True).add_argument("asset")
    up = door("upload-picture", "your own picture in place of one the article shows, or the cover (free, a version)",
              version=True)
    up.add_argument("asset", help="the picture it replaces, e.g. assets/chart_1.png")
    up.add_argument("file", help="a PNG, JPEG or WebP file")
    so = door("seo", "the meta tags, the slug, the focus keyword, the FAQ (a version); a field not given stays",
              version=True)
    so.add_argument("--meta-title")
    so.add_argument("--meta-description")
    so.add_argument("--slug", dest="new_slug", help="the post's slug (lower-case words joined by hyphens)")
    so.add_argument("--focus-keyword")
    so.add_argument("--faq", help='the whole FAQ as JSON: [{"question": "…", "answer": "…"}]')
    door("cover", "draw a new cover (paid: one image call)", version=True, paid=True)
    rd = door("redraw", "make one picture again: a chart or infographic redrawn, a screenshot re-captured (paid)",
              version=True, paid=True)
    rd.add_argument("asset", help="assets/<file>")
    rd.add_argument("--feedback", help="what to change")
    rd.add_argument("--kind", choices=["auto", "screenshot", "infographic", "chart", "image"],
                    help="make it as this kind instead (image = an illustration), or auto: the producer chooses")
    acc = door("accept", "mark this version accepted, and do what Configure > Publishing says", version=True)
    acc.add_argument("--action", choices=["none", "draft", "publish", "update_live"], default="none",
                     help="what the Accept does, as the page would name it (from status --json)")
    acc.add_argument("--post", help="the post the Accept names, when it sends")

    su = sub.add_parser("setup", help="set the install up without the interface: the project, the keys, WordPress; "
                                      "each is tested (free) and saved as the setup screen saves it")
    su.add_argument("--name")
    su.add_argument("--logo", help="a PNG, JPG, WebP or SVG file")
    su.add_argument("--domain", help="our site's domain, e.g. example.com")
    su.add_argument("--blog", help="the blog's address, e.g. https://blog.example.com")
    su.add_argument("--audience", help="who the articles are for, in one line")
    su.add_argument("--brand-voice-file", help="a text file: how the brand writes")
    su.add_argument("--product-facts-file", help="a text file: everything the agents may say about the products")
    su.add_argument("--editorial-rules-file", help="a text file: the project's own rules for the writer and the review")
    for env, flag in SETUP_KEY_FLAGS.items():
        su.add_argument("--" + flag.replace("_", "-"), dest=flag, help=f"the {env} key")
    su.add_argument("--wp-url")
    su.add_argument("--wp-user")
    su.add_argument("--wp-password", help="a WordPress application password")
    su.add_argument("--author", help="the WordPress user id the posts carry (\"\": the user the password belongs to)")
    su.add_argument("--category", help="the WordPress category for new posts (\"\": none)")
    su.add_argument("--no-test", action="store_true", help="save without asking the services")
    su.add_argument("--json", action="store_true", help="print one JSON object")
    sm = sub.add_parser("sample", help="add the sample article (made up, free) to look at the Studio without keys")
    sm.add_argument("--json", action="store_true", help="print one JSON object")
    dr = sub.add_parser("doctor", help="check the install: the seven services (free tests), the project, the storage, "
                                       "the browser; exit 1 when anything is missing or fails")
    dr.add_argument("--no-test", action="store_true", help="only say what is set; ask no service")
    dr.add_argument("--json", action="store_true", help="print one JSON object")
    se = sub.add_parser("settings", help="the research and publishing settings: get [keys], set key=value…, reset [keys]")
    se.add_argument("action", choices=["get", "set", "reset"])
    se.add_argument("pairs", nargs="*")
    se.add_argument("--json", action="store_true", help="print one JSON object")
    pr = sub.add_parser("prompts", help="the agents' prompts: list, show <name>, set <name> --file F, reset <name>")
    pr.add_argument("action", choices=["list", "show", "set", "reset"])
    pr.add_argument("name", nargs="?")
    pr.add_argument("--file")
    pr.add_argument("--json", action="store_true", help="print one JSON object")
    ac = sub.add_parser("access", help="who may open the Studio: show it, turn the password off (--off --yes), "
                                       "or set a new one read from stdin (--password-stdin)")
    ac.add_argument("--off", action="store_true")
    ac.add_argument("--password-stdin", action="store_true")
    ac.add_argument("--yes", action="store_true", help="confirm --off")
    ac.add_argument("--json", action="store_true", help="print one JSON object")
    pj = sub.add_parser("project", help="the project: show it, or change its texts, logo, website, colours, competitors")
    pj.add_argument("--text", choices=["project_name", "audience", "brand_voice", "product_facts", "editorial_rules"],
                    help="a project text: shown, or set with --value/--file, or --reset-text")
    pj.add_argument("--value")
    pj.add_argument("--file")
    pj.add_argument("--reset-text", action="store_true")
    pj.add_argument("--logo")
    pj.add_argument("--remove-logo", action="store_true")
    pj.add_argument("--domains", help="comma-separated")
    pj.add_argument("--blog")
    pj.add_argument("--site-sitemap")
    pj.add_argument("--blog-sitemap")
    pj.add_argument("--palette", nargs="*", help="name=#RRGGBB …")
    pj.add_argument("--competitors", help="comma-separated hosts")
    pj.add_argument("--json", action="store_true", help="print one JSON object")

    args = ap.parse_args()

    if _agent_command(args) or _configure_command(args):
        return

    if args.cmd == "keywords":
        from core.config import load_core_config, load_project
        from core.keyword_report import build

        cfg = load_core_config()
        project = load_project(core=cfg)
        out = output_root(cfg)
        dirs = ([out / args.slug] if args.slug
                else sorted(d for d in out.iterdir()
                            if d.is_dir() and not d.name.startswith("_")
                            and ((d / "article.md").exists() or (d / "04_draft.md").exists())))
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        if args.json:
            rows = {}
            for d in dirs:
                try:
                    rows[d.name] = build(d, project.seo)
                except FileNotFoundError:
                    rows[d.name] = None             # no text to check yet
            _emit(args, {"ok": True, "articles": rows})
            return
        for d in dirs:
            try:
                data = build(d, project.seo)
            except FileNotFoundError as e:
                print(f"  --   {d.name}: {e}")
                continue
            pk = data["primary"] or {}
            print(f"  {mark[data['verdict']]} {d.name}  primary '{pk.get('keyword')}' "
                  f"x{pk.get('total')} @ {pk.get('density')}%  "
                  f"({data['failed']} failed, {data['warned']} to review, "
                  f"{data['article_words']} words)")
            if not args.quiet:
                for c in data["checks"]:
                    if c["status"] != "ok":
                        print(f"         {'!' if c['status'] == 'fail' else '~'} {c['name']}: {c['detail']}")
                print(f"         -> {d / '06_keywords.md'}")
        return

    if args.cmd == "costs":
        from core import usage
        from core.store import Repository

        _, _, root, prices = _context()     # the Studio's price table, the profile's included (R6FA-05)
        repo = Repository(root)
        arts = [repo.get(args.slug)] if args.slug else repo.articles()
        if args.json:
            rows = {a.slug: usage.article_summary(a.dir, prices) for a in arts}
            _emit(args, {"ok": True, "total_usd": round(sum(r["total"] for r in rows.values()), 2), "articles": rows})
            return
        grand = 0.0
        for art in arts:
            s = usage.article_summary(art.dir, prices)
            grand += s["total"]
            print(f"  ${s['total']:8.2f}{' est.' if s['estimated'] else '     '}  {art.slug}")
            if args.slug:
                for c in s["by_category"]:
                    print(f"      {c['label']:<24} ${c['cost']:7.2f}  {c['share'] * 100:4.0f}%")
                for pv in s["by_provider"]:
                    print(f"      {pv['label']:<24} ${pv['cost']:7.2f}  {pv['calls']} call(s)")
                for u in s["unpriced"]:
                    print(f"      not priced: {u['provider']} {u['units']}")
        if not args.slug:
            print(f"  ${grand:8.2f}       all {len(arts)} article(s)")
        return

    if args.cmd == "visual-check":
        from core import usage
        from core.agents.visual_checker import VisualChecker

        cfg, project, root, prices = _context()
        art = _article(args, root)
        est = _paid(args, root, prices, "visual_check", f"A look at every picture of '{art.slug}'")
        fm, body, _ = art.read()
        with usage.recording(art.dir, "visual_check", config=cfg), usage.stage("review"):
            results = VisualChecker(project, cfg).check(art.dir, body, fm.get("cover"))
        if args.json:
            _emit(args, {"ok": True, "pictures": results, "estimate": est})
            return
        for r in results:
            mark = "error" if r["error"] else {"right": "ok", "problems": "PROBLEM"}.get(r.get("verdict"), "unclear")
            print(f"  {mark:7} {r['path']}" + (f"  ({r['error']})" if r["error"] else ""))
            for pr in r["problems"]:
                print(f"          [{pr['severity']}] {pr['problem']}\n          fix: {VisualChecker.fix_text(r['path'], pr)}")
            for why in r.get("dropped") or []:
                print(f"          (an answer outside the contract was dropped: {why})")
        _print_cost()
        return

    if args.cmd == "cleanup":
        from core import cleanup
        from core.store import Busy, Repository

        try:
            art = Repository().get(args.slug, listed_only=True)
        except KeyError:
            _refuse(args, "unknown", f"no article '{args.slug}' here")
        try:
            res = cleanup.clean_pictures(art)
        except Busy as e:
            if args.json:
                _refuse(args, "busy", str(e))
            raise SystemExit(f"Not cleaned: {e}") from None
        if args.json:
            _emit(args, {"ok": True, **res, "version": art.version()})
            return
        for c in res["changes"]:
            print(f"  changed: {c}")
        for c in res["left"]:
            print(f"  to do  : {c}")
        if not res["changes"] and not res["left"]:
            print("  nothing to clean up")
        return

    if args.cmd == "migrate-qa":
        from core import qa_migration
        from core.settings import load as load_settings
        from core.store import Busy, CorruptFile, Repository

        repo = Repository()
        try:
            arts = [repo.get(s, listed_only=True) for s in args.slug] if args.slug else repo.articles()
        except KeyError as e:
            _refuse(args, "unknown", f"no article {e} here")
        if args.apply and not args.slug:
            # an article skipped (busy, unreadable) is a failure of the command, the others stay
            # migrated (review R5CC-03); the Studio's start goes on either way
            rep = qa_migration.migrate_install(repo.root)
            _emit(args, {"ok": not rep["skipped"], **rep},
                  [f"  migrated {len(rep['migrated'])}: {', '.join(rep['migrated']) or '-'}; already {rep['already']}; "
                   f"skipped {len(rep['skipped'])} {rep['skipped'] or ''}"])
            if rep["skipped"]:
                raise Refusal(1)
            return
        if args.json:
            rows = {}
            for art in arts:
                try:
                    rows[art.slug] = qa_migration.migrate_article(art, apply=args.apply)
                except (CorruptFile, Busy) as e:
                    rows[art.slug] = {"status": "skipped", "reason": str(e)}
            failed = [s for s, r in rows.items() if r.get("status") == "skipped"]
            _emit(args, {"ok": not failed, "applied": bool(args.apply), "articles": rows})
            if failed:
                raise Refusal(1)
            return
        failed = []
        for art in arts:
            try:
                r = qa_migration.migrate_article(art, apply=args.apply)
            except (CorruptFile, Busy) as e:
                print(f"  {art.slug}: not migrated, {e}")
                failed.append(art.slug)
                continue
            if r["status"] != "migrated":
                print(f"  {art.slug}: {r['status']}")
                continue
            wp = (f"post #{r['published']}" if r["published"] else
                  f"unknown ({r['published_unknown']})" if r["published_unknown"] else "not on WordPress")
            print(f"  {art.slug}: old checklist kept as history ({r['legacy']} findings, {r['legacy_open']} "
                  f"open), none carried; accepted: {'yes' if r['accepted'] else 'no'} ({r['old_ready']}); {wp}")
        _, note = qa_migration.migrate_settings(load_settings(repo.root))
        print("  settings: " + (f"publishing.on_ready was '{note['publishing.on_ready']}'; the choice on Accept "
                                f"starts at nothing" if note else "nothing to migrate"))
        if failed:
            raise SystemExit(1)
        return

    if args.cmd == "export":
        from core.store import Repository
        from core.transfer import export_article

        from core.store import Busy

        try:
            art = Repository().get(args.slug, listed_only=True)
        except KeyError:
            _refuse(args, "unknown", f"no article '{args.slug}' here")
        out = Path(args.out or f"{art.slug}.zip")
        try:
            out.write_bytes(export_article(art))
        except Busy as e:
            _refuse(args, "busy", f"Not exported: {e}")
        except OSError as e:
            _refuse(args, "not_written", f"Not exported: {e}")
        _emit(args, {"ok": True, "slug": art.slug, "zip": str(out), "bytes": out.stat().st_size},
              [f"  exported {art.slug} -> {out} ({out.stat().st_size / 1e6:.1f} MB)"])
        return

    if args.cmd == "import":
        from core.store import Repository
        from core.transfer import BadArchive, import_article

        try:
            art = import_article(Repository(), Path(args.zip).read_bytes(), slug=args.as_slug)
        except BadArchive as e:
            _refuse(args, "bad_archive", f"not imported: {e}")
        except OSError as e:
            _refuse(args, "unknown", f"not imported: {e}")
        _emit(args, {"ok": True, "slug": art.slug, "version": art.version()}, [f"  imported -> {art.dir}"])
        return

    if args.cmd == "uniqueness":
        from core import store, usage
        from core.plagiarism_report import build

        cfg, project, root, prices = _context()
        art = _article(args, root)
        d = art.dir
        from core.tools.plagiarism import PlagiarismError

        est = None
        if not args.dry_run:
            words = len(art.read()[1].split())
            est = _paid(args, root, prices, "uniqueness",
                        f"A uniqueness check of about {words:,} words (about {2 * words:,} Winston credits)")
        try:
            with art.lock(holder="uniqueness check (CLI)"), usage.recording(d, "uniqueness", config=cfg), \
                    usage.stage("uniqueness"):
                if not args.dry_run and art.path("article.md").is_file():
                    from core import versions

                    # the text it reads is a version it can name (R9D2-04); a draft is no version,
                    # and the result keeps it instead (R11F-03)
                    versions.ensure(art)
                data = build(d, project.seo, dry_run=args.dry_run)
        except PlagiarismError as e:
            if args.json:
                _refuse(args, "cannot_run", str(e))
            print(f"  cannot run the check: {e}")
            raise SystemExit(1)
        except store.Busy as e:
            _refuse(args, "busy", str(e))
        if args.json:
            _emit(args, {"ok": True, "dry_run": bool(args.dry_run), "estimate": est,
                         **{k: v for k, v in data.items() if k != "report"}})
            return
        if args.dry_run:
            print(f"  would send {data['words']:,} words / {data['chars']:,} characters "
                  f"in {data['chunks']} request(s)")
            print(f"  estimated cost {data['estimated_credits']:,} credits "
                  f"(2 per word, per Winston's docs)")
            print(f"  excluded from matching: {', '.join(data['excluded'])}")
            print("  no API call was made")
            return
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "unknown": "??  "}
        rewrite = [p for p in data["passages"] if not p["cited"]]
        print(f"  {mark[data['verdict']]} {args.slug}: uniqueness {data['uniqueness']}% "
              f"({data['plagiarised_words']}/{data['total_words']} words matched, "
              f"{data['source_count']} sources, {data['credits_used']} credits)")
        for p in rewrite[:10]:
            print(f"         {p['words']}w  {p['text'][:90]}…")
            print(f"                -> {p['sources'][0]['url']}")
        print(f"         -> {d / '07_plagiarism.md'}")
        return

    if args.cmd == "refresh-links":
        from core.config import load_project
        from core.tools.link_index import LinkIndex

        project = load_project()
        idx = LinkIndex.build(project.links)
        path = idx.save(project.root)
        if args.json:
            _emit(args, {"ok": True, "targets": len(idx.targets)})
            return
        print(f"\n{len(idx.targets)} targets -> {path}")
        for t in idx.targets[: args.show]:
            print(f"  [{t.source}/{t.kind}] {t.title[:60]!r} {t.url}")
        return

    if args.cmd in ("publish", "wp-status"):
        # the Studio's path: the configured output folder, the keys saved in the Studio, the
        # article's lock, the version and the post named before the send (core/publication)
        from core import api_keys, publication, store
        from core.config import load_core_config
        from core.qa_changes import Stale

        cfg = load_core_config()
        api_keys.apply(store.output_root(cfg))
        try:
            art = store.Repository(config=cfg).get(args.slug)
        except KeyError:
            raise SystemExit(f"No article '{args.slug}' in {store.output_root(cfg)}") from None
        if args.cmd == "wp-status":
            res = publication.check_post(art.slug)
            _emit(args, {"ok": True, **res}, [f"  {res.get('status') or res.get('detail')}"
                                                 + (f" (modified {res['modified']})" if res.get("modified") else "")])
            return
        action = ("publish" if args.live else "update_live" if args.update_live
                  else "unpublish" if args.to_draft else "draft")
        version = art.version()            # one version: announced, checked, sent (review R638-04)
        state = publication.post_state(art, version)
        post = (store.read_json_lenient(art.path("_wp.json"), {}) or {}).get("post_id")
        checks = publication.pre_send_checks(art)
        if not args.json:
            print(f"Action : {action.replace('_', ' ')} · version {version} · "
                  + (f"post #{post} ({state['status'] or 'status unknown'})" if post else "a new post"))
            for c in checks:
                print(f"  check  : {c['check']}: {c['detail']}")
        try:
            res = publication.send(art.slug, version, action, post, category=args.category, who="cli")
        except store.Busy as e:
            _refuse(args, "busy", f"Not sent: {e}")
        except Stale as e:
            _refuse(args, "stale", f"Not sent: {e}")
        if args.json:
            _emit(args, {"ok": res["outcome"] == "sent", "action": action, "version": version, "checks": checks, **res})
            if res["outcome"] != "sent":
                raise Refusal(1)
            return
        if res["outcome"] != "sent":
            raise SystemExit(("Unknown: " if res["outcome"] == "unknown" else "Not sent: ") + str(res.get("detail")))
        print(f"SENT post #{res['post_id']} [{res['status']}]")
        print(f"  preview        : {res.get('link')}")
        for w in res.get("warnings") or []:
            print(f"  not confirmed  : {w}")
        return

    if args.cmd == "generate":
        overrides = {}
        if args.no_ahrefs:
            overrides["use_ahrefs"] = False
        if args.words:
            overrides["target_words"] = args.words
        if args.processor:
            overrides["processor"] = args.processor
        if args.reuse_from:
            overrides["reuse_from"] = args.reuse_from
            if args.out:
                overrides["reuse_out"] = args.out
            if args.in_place:
                overrides["reuse_in_place"] = True
        elif args.out or args.in_place:
            ap.error("--out and --in-place only apply with --reuse-from")

        _, _, root, prices = _context()
        from core import setup

        refused = setup.ready_to_write(root)
        if refused:
            _refuse(args, "not ready", f"{refused} Run `cli.py doctor` to see what is missing.")
        est = _paid(args, root, prices, "generation", f"Write an article on “{args.topic}”")
        res = generate_article(
            args.topic, status_callback=(lambda s: None) if args.json else (lambda s: print(f"  → {s}")), **overrides
        )
        if args.json:
            from core import store
            from core.agent_door import facts

            art = store.Repository(root).get(res["slug"]) if res.get("slug") else None
            _emit(args, {"ok": res["status"] in ("success", "needs_review"), "status": res["status"],
                         "slug": res.get("slug"), "error": res.get("error"), "estimate": est,
                         "facts": facts(art, prices) if art else None})
            if res["status"] == "aborted":
                raise SystemExit(2)
            return
        print()
        print(f"Status : {res['status']}")
        _print_cost()
        if res["status"] == "aborted":
            # a missing input is a hard stop, not a warning: nothing downstream was worth
            # running, and no article.md was written
            print(f"Aborted: {res.get('error')}")
            print("Nothing was written. Re-run the topic once the input is available.")
            raise SystemExit(2)
        print(f"Article: {res['final_path']}")
        print(f"Visuals planned: {len(res['visuals'])}")
        for line in res["log"]:
            print(f"  · {line}")


if __name__ == "__main__":
    main()
