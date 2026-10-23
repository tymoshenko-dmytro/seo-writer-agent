"""Accept, what the Studio knows of the WordPress post, and sending.

**Accept** is the person's mark "I looked, this version is good" (`accepted` in `_article.json`).
What it also does is the setting `publishing.on_accept`: nothing (the default), send or update the
draft, or publish. Its label says which, and names a live post when it would update one:
*Accept this version* · *Accept and send to the draft* · *Accept and publish* · *Accept and update
the live post*. The request carries the version shown and the action the label promised; under
the article's lock either differing changes nothing (Stale).

**Sending** is never blocked by the findings, the score or a missing acceptance; the checks are
shown first (`pre_send_checks`), each as what it is. Still, technically exact:

- the send carries the version shown and the post it names; under the lock a different version or
  post changes nothing;
- before writing to an existing post, its status is read from WordPress: a live post is touched
  only by *Update the live post* (or the CLI's explicit unpublish), and a post whose status cannot
  be read is not touched at all;
- one post per article: with no binding, the post with the article's slug is looked for first and
  bound when it exists, so a repeated or retried send never makes a second post;
- an API error is an error; an outcome the Studio cannot confirm is "unknown: check the post in
  WordPress", never "sent".

`published` records what the Studio last sent and when (`sent_version`, `sent_at`) and the post's
status as last read (`last_known`). Nothing here writes WordPress except `send` (and `accept`
through it); migration, import and opening a page never send.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import qa_model as qm
from . import store
from .qa_changes import Stale, _session, check_version, records

ACTIONS = ("draft", "publish", "update_live", "unpublish")
ON_ACCEPT = ("none", "draft", "publish")
# a post not in draft: writing it as a draft would take it off the blog
LIVE = ("publish", "future", "private")
SEND_LOG = 20


class SendError(RuntimeError):
    """Nothing was sent: the reason says why (a refused action, a post that could not be read,
    credentials, an API error before anything was written)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# what the Studio knows
# ---------------------------------------------------------------------------------------------
def published(art: store.Article) -> dict | None:
    """The `published` record, or, for an article not migrated yet, the same read from `_wp.json`
    without writing anything."""
    try:
        rec = art.record()
    except store.CorruptFile:
        rec = {}
    if "published" in rec:
        return rec.get("published")
    from .qa_migration import published_from_wp

    return published_from_wp(art, _now()[:10])


def status_of(pub: dict | None) -> str | None:
    if not pub:
        return None
    return (pub.get("last_known") or {}).get("status") or pub.get("wp_status")


def is_live(pub: dict | None) -> bool:
    return status_of(pub) in LIVE


def post_state(art: store.Article, version: str | None = None) -> dict:
    """The WordPress fact for the page and the dashboard: the post, what was last sent and when,
    whether the article changed since, and the status as last read with the time it was read."""
    pub = published(art) or {}
    version = version if version is not None else art.version()
    sent = pub.get("sent_version")
    return {"post_id": pub.get("post_id"), "link": pub.get("link"), "status": status_of(pub),
            "live": is_live(pub), "sent_version": sent, "sent_at": pub.get("sent_at"),
            "changed_since_sent": None if not sent else sent != version,
            "last_known": pub.get("last_known"), "unknown": pub.get("unknown"),
            "last_attempt": pub.get("last_attempt")}


def accepted_state(art: store.Article, version: str | None = None) -> dict:
    """The person's fact: accepted this version / changed since it was accepted / not accepted."""
    try:
        acc = art.record().get("accepted")
    except store.CorruptFile:
        acc = None
    version = version if version is not None else art.version()
    if not acc:
        return {"state": "not_accepted", "accepted": None}
    return {"state": "accepted" if acc.get("version") == version else "changed_since", "accepted": acc}


def on_accept(project) -> str:
    v = str(((project.seo or {}).get("publishing") or {}).get("on_accept") or "none")
    return v if v in ON_ACCEPT else "none"


def accept_action(setting: str, pub: dict | None) -> str | None:
    """What Accept also does, from the setting and the post as last known."""
    if setting not in ("draft", "publish"):
        return None
    if pub and pub.get("post_id") and is_live(pub):
        return "update_live"
    return setting


def accept_label(action: str | None, pub: dict | None = None) -> str:
    post = f" #{pub['post_id']}" if pub and pub.get("post_id") else ""
    return {None: "Accept this version", "draft": "Accept and send to the draft",
            "publish": "Accept and publish",
            "update_live": f"Accept and update the live post{post}"}[action]


def send_options(pub: dict | None) -> list[tuple[str, str]]:
    """The sends the page offers, each with the label that names it and the post it touches."""
    pid = (pub or {}).get("post_id")
    if not pid:
        return [("draft", "Send to WordPress as a draft"), ("publish", "Publish on the blog")]
    if is_live(pub):
        return [("update_live", f"Update the live post #{pid}")]
    return [("draft", f"Update the draft #{pid}"), ("publish", f"Publish draft #{pid} on the blog")]


def _uniqueness_since(art: store.Article, uniq: dict, md: str) -> tuple[str, str]:
    """The uniqueness row for a result of an earlier version: what that version scored, how much
    of the text is new or rewritten since, and whether that much new text could bring it under
    the warn line (plagiarism_report.since_check)."""
    from .plagiarism_report import DEFAULTS, since_check

    if uniq.get("uniqueness") is None:
        return "check_again", "the check of an earlier version gave no result"
    s = since_check(art, uniq, md)
    if s is None:
        return "check_again", "an earlier version was checked, and the Studio no longer keeps it to compare"
    warn = int((uniq.get("thresholds") or {}).get("warn_below") or DEFAULTS["warn_below"])
    head = f"v{s['n']} was checked: {uniq.get('uniqueness')}% unique."
    since = f"{s['new']}% of the text is new since" if s["new"] else "No sentence is new since"
    if uniq.get("verdict") in ("warn", "fail"):
        return "found", f"v{s['n']} was checked: {uniq.get('uniqueness')}% unique ({uniq['verdict']}). {since}: check it again"
    if uniq.get("verdict") == "ok" and s["lowest"] >= warn:
        return "clear", (f"{head} {since}" + (f"; even if all of it were copied, it stays {s['lowest']}% unique or more"
                                              if s["new"] else ""))
    return "check_again", f"{head} {since}: if it were copied, the text could fall under {warn}% unique"


def pre_send_checks(art: store.Article) -> list[dict]:
    """What the checks say before a send, each as what it is: {"check", "state", "detail"} with
    state "found", "clear", "not_checked", "check_again" (a result of an earlier version that the
    edits since may have moved) or "failed" (a check that could not run is never a pass)."""
    from .validators import analyze_structure, meta_commentary

    out = []
    md = art.text()
    fm, body, _ = art.read()
    try:
        from .config import project_root
        from .tools.link_index import LinkIndex

        idx = LinkIndex.load(project_root())
    except Exception:  # noqa: BLE001
        idx = None
    if not idx or not idx.targets:
        out.append({"check": "internal links", "state": "failed",
                    "detail": "the check could not run: there is no link index (cli.py refresh-links)"})
    else:
        built = datetime.fromtimestamp(idx.generated_at).strftime("%Y-%m-%d") if idx.generated_at else "an unknown date"
        inv = analyze_structure(md, allowed_urls=[t.url for t in idx.targets],
                               article_dir=art.dir).get("invented_internal") or []
        out.append({"check": "internal links", "state": "found" if inv else "clear",
                    "detail": (f"not in our link index (built {built}): " + ", ".join(inv[:5])) if inv
                    else f"every internal link is in our link index (built {built})"})
    faq = fm.get("faq") if isinstance(fm.get("faq"), list) else []
    faq_text = " ".join(f"{q.get('question', '')} {q.get('answer', '')}" for q in faq if isinstance(q, dict))
    mc = meta_commentary(body) + meta_commentary(faq_text)
    out.append({"check": "writing-process sentences", "state": "found" if mc else "clear",
                "detail": f"a sentence that may describe how the article was written: «{mc[0][:160]}»" if mc
                else "no sentence about how the article was written"})
    from .plagiarism_report import article_text, text_sha
    from .tools.plagiarism import estimate_credits

    uniq = store.read_json_lenient(art.path("_plagiarism.json"), {}) or {}
    # the check runs again from its own row, at its price (operator, 2026-10-23)
    again = {"again": "Check again" if uniq else "Check the uniqueness",
             "again_cost": f"about {estimate_credits(article_text(md)):,} Winston credits"}
    if not uniq:
        out.append({"check": "uniqueness", "state": "not_checked", "detail": "uniqueness not checked", **again})
    else:
        # clear only for a result of this text that says it is unique (review R638-06), or for a
        # result of an earlier version when the text since cannot bring it under the warn line
        # (operator, 2026-10-23: "not checked" after every edit alarmed people)
        sha = uniq.get("text_sha")
        if not sha:
            state, detail = "check_again", "an older result that does not say which version of the text it read"
        elif sha != text_sha(md):
            state, detail = _uniqueness_since(art, uniq, md)
        elif uniq.get("verdict") in ("warn", "fail"):
            state, detail = "found", f"{uniq.get('uniqueness')}% unique ({uniq.get('verdict')})"
        elif uniq.get("verdict") == "ok" and uniq.get("uniqueness") is not None:
            state, detail = "clear", f"{uniq.get('uniqueness')}% unique"
        else:
            state, detail = "failed", "the uniqueness check gave no result for this text"
        out.append({"check": "uniqueness", "state": state, "detail": detail, **again})
    missing = [p for p, h in art.pictures().items() if h is None]
    out.append({"check": "pictures", "state": "found" if missing else "clear",
                "detail": ("missing on disk: " + ", ".join(missing[:5])) if missing
                else "every picture the article shows is on disk"})
    try:
        rv = (art.run().get("review") or {})
    except store.CorruptFile:
        rv = {}
    last = (rv.get("reviews") or [None])[-1]
    if not last:
        out.append({"check": "review", "state": "not_checked", "detail": "not reviewed in the new system"})
    elif last.get("valid") is False:
        out.append({"check": "review", "state": "failed",
                    "detail": f"review {last['id']} did not work: {last.get('valid_reads', 0)} of "
                              f"{last.get('reads_requested', '?')} reads came back"})
    else:
        open_ = [f for f in rv.get("findings") or [] if f.get("review") == last["id"] and f.get("status") == "open"]
        # a change the check says left its problem in place needs the person too, as on the
        # Review tab (R6FA-03)
        half = [g for g in rv.get("changes") or [] if g.get("review") == last["id"] and not g.get("reverted")
                and qm.half_done(g)]
        said = ([f"{len(open_)} finding(s) still open"] if open_ else []) + (
            [f"{len(half)} change(s) the check says left the problem in place"] if half else [])
        out.append({"check": "review", "state": "found" if said else "clear",
                    "detail": (f"review {last['id']}: {' and '.join(said)}; score "
                               f"{qm.score([f for f in rv['findings'] if f.get('review') == last['id']])}")
                    if said else f"nothing open from review {last['id']}"})
    return out


# ---------------------------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------------------------
def _outcome_of(e: Exception) -> str:
    """"error" when the failure says nothing was written; "unknown" when WordPress may have."""
    import requests

    if isinstance(e, (requests.Timeout, requests.ConnectionError)):
        return "unknown"
    if isinstance(e, requests.HTTPError):
        code = getattr(getattr(e, "response", None), "status_code", None)
        return "error" if code and 400 <= int(code) < 500 else "unknown"
    from .tools.wp_publisher import NotSent

    return "error" if isinstance(e, (SendError, NotSent, ValueError, FileNotFoundError)) else "unknown"


def _slug_of(art: store.Article) -> str:
    return (art.read()[0] or {}).get("slug") or art.slug


def _wp_state(art: store.Article) -> dict:
    return store.read_json_lenient(art.path("_wp.json"), {}) or {}


def send_locked(art: store.Article, version: str, action: str, post, category: str | None = None,
                who: str = "studio") -> dict:
    """The send, inside the caller's lock (send, accept). Returns {"outcome": "sent" | "error" |
    "unknown", "action", "post_id", "status", "link", "warnings", "detail"}; raises Stale when
    the page showed another version or post."""
    from .tools.wp_publisher import WPPublisher

    check_version(art, version)
    if action not in ACTIONS:
        raise ValueError(f"unknown action '{action}'")
    run, rv = records(art)
    rec = art.record()
    pub = dict(rec.get("published") or {})
    bound = _wp_state(art).get("post_id")
    if (bound or None) != (int(post) if str(post or "").isdigit() else None):
        raise Stale(f"the article's WordPress post is {'#' + str(bound) if bound else 'none yet'}, not the one "
                    f"the page named: load the page again")
    result = {"outcome": "error", "action": action, "post_id": bound, "status": None, "link": pub.get("link"),
              "warnings": [], "detail": ""}
    wp = WPPublisher()
    try:
        if not wp.configured:
            raise SendError(f"not sent: {wp.refused}" if wp.refused else
                            "not sent: WordPress credentials missing (Configure > Connections)")
        status, adopted, seen = _status_now(wp, art, bound, pub)
        if seen is not None:
            # what WordPress said of the post is a fact the page and Check post need, whatever
            # the send does next; it says nothing about which version the post holds (R638-02)
            pub.update(post_id=seen["id"], link=seen.get("link") or pub.get("link"), site=wp.site,
                       last_known={"status": seen.get("status"), "checked_at": _now(),
                                   "source": "found by the article's slug" if adopted else "a read of the post",
                                   "modified": seen.get("modified_gmt")})
            result.update(post_id=seen["id"], link=pub.get("link"))
        if adopted:
            bound = adopted["id"]
            result["warnings"].append(f"bound to post #{bound}, already on WordPress under this article's slug")
        live = status in LIVE
        if live and action not in ("update_live", "unpublish"):
            raise SendError(f"post #{bound} is live on the blog: the action is «Update the live post #{bound}»; "
                            f"nothing was sent")
        if action == "update_live" and not live:
            raise SendError(f"post #{bound or '-'} is not live, as WordPress says now: nothing was sent")
        if action == "unpublish" and not live:
            raise SendError(f"post #{bound or '-'} is not live: nothing to take off the blog")
    except SendError as e:
        result["detail"] = str(e)
        _record_attempt(art, rec, pub, result, version, who)
        return result
    wp_status = {"draft": "draft", "publish": "publish", "update_live": None, "unpublish": "draft"}[action]
    before = (_wp_state(art).get("last_post_write") or {}).get("at")
    try:
        res = wp.publish_article(art.dir, category=category, status=wp_status)
        result.update(outcome="sent", post_id=res.get("post_id"), status=res.get("status"), link=res.get("link"),
                      warnings=result["warnings"] + list(res.get("warnings") or []))
    except Exception as e:  # noqa: BLE001 — every failure is classified, none is "sent"
        after = _wp_state(art)
        if (after.get("last_post_write") or {}).get("at") != before:
            # WordPress confirmed the post write; what failed came after it (the FAQ read-back)
            result.update(outcome="sent", post_id=after.get("post_id"), status=after.get("status"),
                          link=after.get("link"))
            result["warnings"].append(f"after the post was saved: {type(e).__name__}: {str(e)[:200]}")
        else:
            result["outcome"] = _outcome_of(e)
            result["detail"] = (f"{type(e).__name__}: {str(e)[:200]}" if result["outcome"] == "error" else
                                f"unknown: check the post in WordPress ({type(e).__name__}: {str(e)[:160]})")
    if result["outcome"] == "sent":
        now = _now()
        pub.update(post_id=result["post_id"], wp_status=result["status"], sent_version=version, sent_at=now,
                   link=result["link"], site=wp.site,
                   last_known={"status": result["status"], "checked_at": now, "source": "the send's answer"})
        pub.pop("unknown", None)
    elif result["outcome"] == "unknown":
        now = _now()
        pub["last_known"] = {"status": "unknown", "checked_at": now, "source": result["detail"]}
        pub["unknown"] = f"the send of {now} was not confirmed: check the post in WordPress"
    _record_attempt(art, rec, pub, result, version, who)
    return result


def _read_bound(wp, art: store.Article, bound) -> dict:
    """The bound post as this site has it, or SendError: a binding made on another site is not
    read there (a post with the same id is somebody else's), and an old binding without its site
    counts only when the post carries this article's slug (review R638-05)."""
    site = _wp_state(art).get("site")
    if site and site != wp.site:
        raise SendError(f"post #{bound} is on {site}, and the Studio is set to {wp.site}: nothing was read or sent")
    try:
        post = wp.read_post(bound)
    except Exception as e:  # noqa: BLE001
        raise SendError(f"post #{bound} could not be read, so whether it is live is unknown: nothing was sent "
                        f"({str(e)[:160]})") from None
    if not site and post.get("slug") != _slug_of(art):
        raise SendError(f"post #{bound} on {wp.site} is not this article (its slug differs): nothing was sent")
    return post


def _status_now(wp, art: store.Article, bound, pub: dict) -> tuple[str | None, dict | None, dict | None]:
    """(the post's status as WordPress says it now, the post found by the slug when the article
    had none bound (then bound to it), the post as read). With no binding, a post is created only
    after WordPress confirmed there is none with this slug: a search that fails stops the send
    (review R638-01). A status that cannot be read stops it too."""
    if bound:
        post = _read_bound(wp, art, bound)
        return post.get("status"), None, post
    try:
        found = wp.find_by_slug(_slug_of(art))
    except Exception as e:  # noqa: BLE001
        raise SendError("WordPress could not be searched for a post with this article's slug, so a send could "
                        f"make a second one: nothing was sent ({str(e)[:160]})") from None
    if found:
        wp.bind(art.dir, found)
        return found.get("status"), found, found
    return None, None, None


def _record_attempt(art: store.Article, rec: dict, pub: dict, result: dict, version: str, who: str) -> None:
    pub["last_attempt"] = {"at": _now(), "action": result["action"], "outcome": result["outcome"],
                           "version": version, "by": who, "detail": result.get("detail") or None}
    log = list(rec.get("sends") or [])
    log.append({k: pub["last_attempt"][k] for k in ("at", "action", "outcome", "version", "by")}
               | {"post_id": result.get("post_id")})
    art.save_record({**{k: v for k, v in rec.items() if k != "schema"}, "published": pub or None,
                     "sends": log[-SEND_LOG:]})


def send(slug: str, version: str, action: str, post=None, category: str | None = None, who: str = "studio",
         config=None, project=None) -> dict:
    """Send the version the page showed, with the action and to the post the page named."""
    with _session(slug, "send", action, config, project) as (art, cfg, proj):
        cat = category if category is not None else (((proj.seo or {}).get("publishing") or {}).get("category") or None)
        return send_locked(art, version, action, post, category=cat, who=who)


def check_post(slug: str, config=None, project=None) -> dict:
    """Read the post's status from WordPress now: `last_known` with the time it was read, and
    whether WordPress modified it after the Studio's last send. Changes nothing on WordPress."""
    from .tools.wp_publisher import WPPublisher

    with _session(slug, "check-post", "", config, project) as (art, cfg, proj):
        records(art)
        rec = art.record()
        pub = dict(rec.get("published") or {})
        if not pub.get("post_id"):
            return {"outcome": "error", "detail": "the article has no WordPress post"}
        wp = WPPublisher()
        bound = _wp_state(art).get("post_id") or pub["post_id"]
        try:
            if not wp.configured:
                raise SendError(wp.refused or "WordPress credentials missing (Configure > Connections)")
            post = _read_bound(wp, art, bound)
        except SendError as e:
            return {"outcome": "error", "detail": str(e)}
        now = _now()
        pub["last_known"] = {"status": post.get("status"), "checked_at": now, "source": "a read of the post",
                             "modified": post.get("modified_gmt")}
        pub.pop("unknown", None)
        art.save_record({**{k: v for k, v in rec.items() if k != "schema"}, "published": pub})
        return {"outcome": "read", "status": post.get("status"), "modified": post.get("modified_gmt")}


# ---------------------------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------------------------
def accept(slug: str, version: str, action: str | None, who: str, config=None, project=None,
           post=None) -> dict:
    """The person's mark on the version the page showed, and the action its label promised
    (`action`: None, "draft", "publish" or "update_live") to the post it named (`post`). A
    different version, a setting or post that now means another action, or another post, changes
    nothing (Stale)."""
    with _session(slug, "accept", "", config, project) as (art, cfg, proj):
        check_version(art, version)
        records(art)
        rec = art.record()
        now_action = accept_action(on_accept(proj), rec.get("published"))
        if (action or None) != now_action:
            raise Stale(f"Accept now means «{accept_label(now_action, rec.get('published'))}», not what the page "
                        f"showed: load the page again")
        bound = _wp_state(art).get("post_id")
        if now_action and (bound or None) != (int(post) if str(post or "").isdigit() else None):
            # the label named a post; the binding moved since (review R638-03)
            raise Stale(f"the article's WordPress post is {'#' + str(bound) if bound else 'none yet'}, not the one "
                        f"the Accept button named: load the page again")
        acc = qm.accepted_record(version, who=who or "operator", origin="person")
        art.save_record({**{k: v for k, v in rec.items() if k != "schema"}, "accepted": acc})
        out = {"accepted": acc, "send": None}
        if now_action:
            cat = ((proj.seo or {}).get("publishing") or {}).get("category") or None
            out["send"] = send_locked(art, version, now_action, bound, category=cat, who=who)
        return out
