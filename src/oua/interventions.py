"""Intervention planner: builds a home page and (rarely) one message. Pure functions of facts + catalogue.

It proposes; it never decides that something is allowed. The verifier does that independently."""
from collections import Counter
from datetime import timedelta

from .goals import new_release_candidates
from .timeutil import next_allowed_send, ist_day, iso

MESSAGE_GOALS = {"G04", "G03", "G06"}
# Why only these: the goal needs reach OUTSIDE the session (trial about to be lost, come back tomorrow, win-back of a lapsed
# payer). For G01/G07/G12/G02/G05/G08/G09 the viewer is in the app right now and the home page already puts the one
# concrete thing in front of them, so a second nudge is noise; they get the home page only.
ROW_ORDER = {
    "G07": ["next_episode", "because_you_watched", "new_releases", "popular_in_dialect", "genre"],
    "G08": ["new_releases", "next_episode", "because_you_watched", "popular_in_dialect", "genre"],
    "G09": ["new_releases", "next_episode", "because_you_watched", "popular_in_dialect", "genre"],
    "G04": ["popular_in_dialect", "new_releases", "next_episode", "because_you_watched", "genre"],
    "G05": ["popular_in_dialect", "new_releases", "next_episode", "because_you_watched", "genre"],
    "G06": ["popular_in_dialect", "new_releases", "next_episode", "because_you_watched", "genre"],
    "G12": ["popular_in_dialect", "because_you_watched", "new_releases", "next_episode", "genre"],
    None: ["popular_in_dialect", "new_releases", "next_episode", "because_you_watched", "genre"],
}


def _clip(s, n):
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def candidate_pool(pack, dialect, language, now):
    """One title per content_slug in `dialect`, available at `now`, language-preferred; flagged records excluded."""
    cat = pack.catalogue
    out = []
    for slug in pack.slugs_in_dialect(dialect):
        t = cat.pick_variant(slug, dialect, language, now)
        if t and not t.flagged:
            out.append(t)
    return out


def build_page(pack, f, goal_id, goal_ctx, settings, now, minimal=False):
    """Returns dict(dialect, hero_title_id, rows, evidence, assumptions). Rows below the minimum are never emitted."""
    d, lang = f.dialect, f.language
    pool = candidate_pool(pack, d, lang, now)
    pop = pack.popularity(d)
    by_pop = sorted(pool, key=lambda t: (-pop.get(t.slug, 0), t.id))
    watched = {p[2] for p in f.plays} if not minimal else set()
    completed_slugs = {c[2] for c in f.completes} if not minimal else set()
    fresh_pool = [t for t in by_pop if t.slug not in watched] or by_pop
    cat = pack.catalogue
    assumptions = []

    # ---- candidate lists per row type -------------------------------------------------------------------
    cands = {}
    cands["popular_in_dialect"] = list(fresh_pool)
    if goal_id == "G02" and not minimal:   # finishing an episode is easier with a short one
        top = fresh_pool[:40]
        top.sort(key=lambda t: (pack.episodes[t.slug][0][3] if pack.episodes.get(t.slug) else 999))
        cands["popular_in_dialect"] = top + fresh_pool[40:]
    cands["new_releases"] = [t for t in new_release_candidates(pack, d, lang, now, settings.new_release_days)
                             if not t.flagged and t.slug not in completed_slugs]
    nxt = []
    if not minimal:
        for s in f.series:
            if s["completed_any"]:
                t = cat.pick_variant(s["slug"], d, lang, now)
                if t and not t.flagged:
                    nxt.append(t)
    cands["next_episode"] = nxt
    taste = Counter()
    for _, tid, slug, _ep in f.plays[-6:]:
        t0 = cat.get(tid)
        if t0:
            taste.update(t0.genres)
    top_g = [g for g, _ in taste.most_common(2)]
    cands["because_you_watched"] = [t for t in fresh_pool if set(t.genres) & set(top_g)] if top_g and not minimal else []
    overall = f.genres.most_common(1)
    cands["genre"] = [t for t in fresh_pool if overall and overall[0][0] in t.genres] if overall and not minimal else []

    # ---- hero ----------------------------------------------------------------------------------------------
    hero = None
    if goal_id == "G07" and goal_ctx.get("title_id"):
        t = cat.get(goal_ctx["title_id"])
        hero = t if t and cat.available(t, now) and not t.flagged else None
    elif goal_id in ("G08", "G09") and cands["new_releases"]:
        hero = cands["new_releases"][0]
    elif goal_id in ("G04", "G05", "G06"):
        hero = next((t for t in cands["popular_in_dialect"] if t.premium), None)
    if hero is None and cands["popular_in_dialect"]:
        hero = cands["popular_in_dialect"][0]
    used = {hero.slug} if hero else set()

    # ---- rows ------------------------------------------------------------------------------------------------
    rows = []
    for rt in ROW_ORDER.get(goal_id, ROW_ORDER[None]):
        if len(rows) >= settings.max_rows:
            break
        size = 8 if rt == "popular_in_dialect" else 6
        picked = []
        for t in cands.get(rt, []):
            if t.slug in used or t.slug in {p.slug for p in picked}:
                continue
            picked.append(t)
            if len(picked) >= min(size, settings.max_titles_per_row):
                break
        if len(picked) >= settings.min_titles_per_row:
            rows.append({"row_type": rt, "title_ids": [t.id for t in picked]})
            used.update(t.slug for t in picked)
        else:
            assumptions.append(f"row {rt} dropped: only {len(picked)} valid title(s)")
    ev = [f"catalogue:{hero.id}"] if hero else []
    ev += [f"catalogue:{r['title_ids'][0]}" for r in rows]
    ev.append(f"popularity:{d}")
    return dict(dialect=d, hero_title_id=hero.id if hero else None, rows=rows, evidence=ev, assumptions=assumptions)


def sanitize_page(pack, page, bad_title_ids, settings):
    """Repair: drop offending titles; rows that fall below the minimum are dropped. Used after a failed verification."""
    bad = set(bad_title_ids)
    rows = []
    for r in page["rows"]:
        ids = [t for t in r["title_ids"] if t not in bad]
        if len(ids) >= settings.min_titles_per_row:
            rows.append({"row_type": r["row_type"], "title_ids": ids})
    hero = page["hero_title_id"] if page["hero_title_id"] not in bad else None
    return dict(page, hero_title_id=hero, rows=rows)


# ---------------------------------------------------------------------------------------------------------- messages
def _copy(goal_id, name, ep_no):
    n = _clip(name, 26)
    if goal_id == "G07":
        return {"title": _clip(f"{n}: episode {ep_no}", 40),
                "body": _clip(f"Aapne {name} adhoora chhoda tha. Episode {ep_no} se dekhiye.", 120)}
    if goal_id == "G04":
        return {"title": "Aapka trial chal raha hai", "body": _clip(f"{name} dekhte rahiye.", 120)}
    if goal_id == "G01":
        return {"title": _clip(f"Pehla episode: {n}", 40), "body": _clip(f"Apna pehla episode dekhiye: {name}.", 120)}
    return {"title": _clip(f"Aapke liye: {n}", 40), "body": _clip(f"Aapke liye: {name}. Abhi dekhiye.", 120)}


def plan_message(pack, f, goal, state, settings, now, page):
    """Returns (proposal | None, reason). One message per goal, at most one per IST day, never in quiet hours."""
    gid = goal["goal_id"]
    if gid not in MESSAGE_GOALS:
        return None, f"no_message: goal {gid} gets the home page only (nothing concrete and honest to say)"
    if goal.get("message_planned"):
        return None, "no_message: one outreach per goal already made"
    recent = [d for d in f.message_days if (ist_day(now) - d).days <= 3]
    if recent:
        return None, "no_message: viewer was messaged within the last 3 days"
    cat = pack.catalogue
    ctx = goal.get("context", {})
    tid = ctx.get("title_id") if gid == "G07" else (page.get("hero_title_id") if page else None)
    t = cat.get(tid) if tid else None
    if t is None or not cat.available(t, now) or t.dialect != f.dialect:
        return None, "no_message: no valid title to promote in the viewer's dialect"
    ep_no = ctx.get("next_ep_no")
    channel = None
    if f.consent_push:
        channel = "push"
    elif f.consent_wa and f.language == "hin":      # approved templates are Hindi (hin)
        channel = "whatsapp"
    else:
        channel = "in_app_message"
    from .timeutil import parse
    deadline = parse(goal["deadline"])
    base = {"channel": channel, "title_id": t.id, "goal_id": gid}
    if channel == "in_app_message":
        base.update(type="in_app_message", text=_copy(gid, t.name, ep_no), send_at=None)
        return base, "in_app message: no push/WhatsApp consent"
    send_at = next_allowed_send(now, now + timedelta(minutes=settings.message_min_delay_min),
                                settings.quiet_start_h, settings.quiet_end_h)
    taken = set(state["message_ledger"]) | {str(d) for d in f.message_days}
    for _ in range(3):
        if str(ist_day(send_at)) not in taken:
            break
        send_at = next_allowed_send(send_at + timedelta(days=1), send_at + timedelta(days=1),
                                    settings.quiet_start_h, settings.quiet_end_h)
    else:
        return None, "no_message: frequency cap leaves no free IST day inside the goal window"
    if send_at >= deadline:
        return None, "no_message: earliest allowed send time is after the goal deadline"
    base["send_at"] = iso(send_at)
    if channel == "push":
        base.update(type="push", text=_copy(gid, t.name, ep_no), deeplink=f"ott://title/{t.id}")
    else:
        plan = pack.plans["P_MONTH"]
        if gid == "G04" and f.plan_ends_at and f.plan_ends_at - now <= timedelta(hours=48):
            base.update(type="whatsapp", template_id="wa_trial_ending_v1",
                        variables={"plan_price": f"₹{plan['price_inr']}"})
        else:
            base.update(type="whatsapp", template_id="wa_recommend_title_v1",
                        variables={"title_name": t.name, "dialect_name": t.dialect})
    return base, f"{channel}: consent={'yes' if channel != 'in_app_message' else 'n/a'}, send_at={base['send_at']}"
