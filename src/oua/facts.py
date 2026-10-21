"""Fact resolver: turns raw events + profile rows into per-viewer facts with provenance, and records every conflict.

PRECEDENCE RULES (also in ARCHITECTURE.md)
  subscription_status : newest subscription event (purchase/renewed/lapsed, ordered by received_at) beats the profile
                        snapshot only if it was received after the profile row's updated_at; otherwise the profile row
                        wins and a disagreeing event is shown as a conflict. No profile and no event -> 'never' is
                        inferred only if the viewer's whole history is visible (signed up inside the event window).
  profile row         : of duplicate rows, latest updated_at wins; every other row is listed as a conflict.
  dialect             : the app setting on the trigger (that is what the app will request). Profile, last
                        app_dialect_changed and campaign deeplink are listed as conflicts, never used. An invalid
                        value (d7, NA, null) yields no dialect and therefore no page.
  consent             : most restrictive wins: every profile row must allow it AND no opt_out event for that channel
                        ever. No profile row -> no consent (unknown is treated as no).
  signup_at           : profile (warehouse) beats the signup_completed event; event is used for new viewers.
  all event-derived   : ordered by received_at (server clock). Occurrence time = received_at, except offline-buffered
                        events (sent_at - occurred_at > 120 s) where it is received_at minus that buffering delay.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .loader import VALID_DIALECT
from .timeutil import ist_day, iso


@dataclass
class Fact:
    name: str
    value: object
    source: str          # profile | event | derived | trigger | none
    ref: str             # citable evidence id
    observed_at: object  # server time of the supporting evidence (None for absence/derived)
    note: str = ""


@dataclass
class Facts:
    user: str
    now: object
    items: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    refs: dict = field(default_factory=dict)       # ref -> observed_at, for freshness reporting
    # plain derived attributes (filled by build_facts)
    signup_at: object = None
    history_complete: bool = False
    account_age_days: float = None
    sub_status: str = None
    sub_source: str = "unknown"
    plan_ends_at: object = None
    lapsed_at: object = None
    dialect: object = None
    language: object = None
    consent_push: bool = False
    consent_wa: bool = False
    plays: list = field(default_factory=list)        # (t_est, title_id, slug, episode_id)
    completes: list = field(default_factory=list)    # (t_est, title_id, slug, episode_id)
    series: list = field(default_factory=list)       # dicts: slug,title_id,next_ep,remaining,last_play,ctype
    play_days_14d: set = field(default_factory=set)
    last_open_before: object = None
    paywall_14d: bool = False
    genres: Counter = field(default_factory=Counter)
    message_days: set = field(default_factory=set)
    is_stale_cache: bool = False
    degraded_reason: str = ""

    def add(self, name, value, source, ref, observed_at=None, note=""):
        self.items[name] = Fact(name, value, source, ref, observed_at, note)
        if ref:
            self.refs[ref] = observed_at
        return value

    def evidence_with_freshness(self, refs):
        out = {}
        for r in refs:
            at = self.refs.get(r)
            out[r] = None if at is None else round((self.now - at).total_seconds())
        return out


def _dialect_of_deeplink(dl):
    if not dl:
        return None
    parts = dl.split("/")
    # ott://c/<campaign>/<dialect>/title/<id>  -> ['ott:', '', 'c', campaign, dialect, 'title', id]
    return parts[4] if len(parts) >= 7 else None


def build_facts(pack, user, events, trigger, now, settings, profile_rows=None):
    f = Facts(user=user, now=now)
    src = pack.profiles.get(user, []) if profile_rows is None else profile_rows
    rows = sorted(src, key=lambda r: r["updated_at"], reverse=True)
    prof = rows[0] if rows else None
    if prof:
        pref = f"profile:{user}@{iso(prof['updated_at'])}"
        f.refs[pref] = prof["snapshot_at"]
        for alt in rows[1:]:
            f.conflicts.append(f"profile_duplicate_row:status={alt['status']},dialect={alt['dialect']}@{iso(alt['updated_at'])}")
    else:
        pref = None

    # ---- signup --------------------------------------------------------------------------------------
    sign_ev = next((e for e in events if e.type == "signup_completed"), None)
    if prof:
        f.signup_at = f.add("signup_at", prof["signup_at"], "profile", pref, prof["snapshot_at"])
        if sign_ev and abs((sign_ev.rt - prof["signup_at"]).total_seconds()) > 86400:
            f.conflicts.append(f"signup_event_disagrees:{iso(sign_ev.rt)}")
    elif sign_ev:
        f.signup_at = f.add("signup_at", sign_ev.rt, "event", sign_ev.ref, sign_ev.rt,
                            "no profile row (new viewer); built from signup_completed")
    f.history_complete = bool(f.signup_at and f.signup_at >= pack.history_start)
    if f.signup_at:
        f.account_age_days = (now - f.signup_at).total_seconds() / 86400
    elif events:
        f.account_age_days = (now - min(e.t_est for e in events)).total_seconds() / 86400

    # ---- subscription -------------------------------------------------------------------------------
    sub_evs = [e for e in events if e.type in ("purchase_completed", "subscription_renewed", "subscription_lapsed")]
    ev_status = ev_ref = ev_at = ev_ends = lapsed_at = None
    for e in sub_evs:  # ordered by received_at
        days = pack.plans.get(e.props.get("plan_id"), {}).get("days", 30)
        if e.type == "purchase_completed":
            ev_status = "trial_active" if e.props.get("is_trial") else "active"
            ev_ends = e.rt + timedelta(days=days)
        elif e.type == "subscription_renewed":
            ev_status, ev_ends = "active", e.rt + timedelta(days=days)
        else:
            ev_status, ev_ends, lapsed_at = "lapsed", None, e.rt
        ev_ref, ev_at = e.ref, e.rt
    if ev_status == "trial_active" and ev_ends and now > ev_ends:
        ev_status = "trial_ended_unresolved"   # trial renews_to P_MONTH but no renewal/lapse event seen: do not guess
    if prof and ev_status and ev_at > prof["updated_at"]:
        f.sub_status = f.add("subscription_status", ev_status, "event", ev_ref, ev_at,
                             f"event newer than profile row ({prof['status']})")
        f.sub_source, f.plan_ends_at, f.lapsed_at = "event", ev_ends, lapsed_at
        if prof["status"] != ev_status:
            f.conflicts.append(f"subscription:profile={prof['status']} overridden by newer {ev_status} event")
    elif prof:
        f.sub_status = f.add("subscription_status", prof["status"], "profile", pref, prof["snapshot_at"],
                             "profile snapshot (up to a day stale)")
        f.sub_source, f.plan_ends_at = "profile", prof["plan_ends_at"]
        f.lapsed_at = lapsed_at
        if ev_status and ev_status != prof["status"] and ev_at <= prof["updated_at"]:
            f.conflicts.append(f"subscription:profile={prof['status']} vs older event={ev_status}")
    elif ev_status:
        f.sub_status = f.add("subscription_status", ev_status, "event", ev_ref, ev_at)
        f.sub_source, f.plan_ends_at, f.lapsed_at = "event", ev_ends, lapsed_at
    elif f.history_complete:
        f.sub_status = f.add("subscription_status", "never", "derived", f"absence:purchase_completed:{user}:n={len(events)}",
                             None, "inferred: whole history visible and no subscription event")
        f.sub_source = "inferred"
    else:
        f.sub_source = "unknown"

    # ---- dialect, language ------------------------------------------------------------------------
    td = trigger.dialect
    if td is not None and VALID_DIALECT.match(str(td)):
        f.dialect = f.add("dialect", td, "trigger", trigger.ref, trigger.rt, "app setting on the trigger")
    else:
        f.conflicts.append(f"app_setting_invalid:{td}")
        f.degraded_reason = f.degraded_reason or "no_valid_dialect"
    if f.dialect:
        if prof and prof["dialect"] != f.dialect:
            f.conflicts.append(f"profile:{prof['dialect'] or '(blank)'}")
        changed = [e for e in events if e.type == "app_dialect_changed"]
        if changed and changed[-1].props.get("to") != f.dialect:
            f.conflicts.append(f"app_dialect_changed:{changed[-1].props.get('to')}")
        dl = _dialect_of_deeplink(trigger.props.get("deeplink"))
        if trigger.props.get("deeplink") and dl != f.dialect:
            f.conflicts.append(f"deeplink:{dl}")
    f.language = trigger.lang if trigger.lang in ("hin", "en") else (prof["language"] if prof else None)

    # ---- consent (most restrictive wins) --------------------------------------------------------------
    outs = {e.props.get("channel") for e in events if e.type == "notification_opt_out"}
    if prof:
        push = all(r["push"] for r in rows) and "push" not in outs
        wa = all(r["wa"] for r in rows) and "whatsapp" not in outs
        f.consent_push = f.add("consent_push", push, "profile", pref, prof["snapshot_at"],
                               "opt_out event overrides" if "push" in outs else "")
        f.consent_wa = f.add("consent_whatsapp", wa, "profile", pref, prof["snapshot_at"],
                             "opt_out event overrides" if "whatsapp" in outs else "")
        if len(rows) > 1 and (any(r["push"] for r in rows) and not all(r["push"] for r in rows)):
            f.conflicts.append("consent_push:duplicate rows disagree -> treated as no consent")
        if len(rows) > 1 and (any(r["wa"] for r in rows) and not all(r["wa"] for r in rows)):
            f.conflicts.append("consent_whatsapp:duplicate rows disagree -> treated as no consent")
    else:
        f.add("consent_push", False, "none", f"absence:profile:{user}", None, "no profile row -> no consent")
        f.add("consent_whatsapp", False, "none", f"absence:profile:{user}", None, "no profile row -> no consent")

    # ---- behaviour -----------------------------------------------------------------------------------
    cat = pack.catalogue
    cut14 = now - timedelta(days=14)
    prog = defaultdict(lambda: {"started": set(), "completed": set(), "last_play": None, "title_id": None})
    for e in events:
        if e.type == "play_started":
            t = cat.get(e.props.get("title_id"))
            if not t:
                continue
            f.plays.append((e.t_est, t.id, t.slug, e.props.get("episode_id")))
            p = prog[t.slug]
            p["started"].add(e.props.get("episode_id"))
            if p["last_play"] is None or e.t_est > p["last_play"]:
                p["last_play"], p["title_id"] = e.t_est, t.id
            for g in t.genres:
                f.genres[g] += 1
            if e.t_est >= cut14:
                f.play_days_14d.add(ist_day(e.t_est))
        elif e.type == "episode_completed":
            t = cat.get(e.props.get("title_id"))
            if t:
                f.completes.append((e.t_est, t.id, t.slug, e.props.get("episode_id")))
                prog[t.slug]["completed"].add(e.props.get("episode_id"))
        elif e.type == "paywall_viewed" and e.t_est >= cut14:
            f.paywall_14d = True
        elif e.type == "message_delivered" and e.props.get("channel") in ("push", "whatsapp"):
            f.message_days.add(ist_day(e.t_est))
    f.plays.sort()
    for slug, p in prog.items():
        eps = pack.episodes.get(slug, [])
        ids = [x[2] for x in eps]
        if not eps:
            continue
        idx_done = max((ids.index(i) for i in p["completed"] if i in ids), default=-1)
        idx_started = max((ids.index(i) for i in p["started"] if i in ids), default=-1)
        nxt = idx_done + 1
        if idx_done == -1 and idx_started >= 0:
            nxt = idx_started  # started but never finished an episode: resume that episode
        t0 = cat.get(p["title_id"])
        if nxt < len(eps) and t0 and t0.ctype in ("show", "micro_drama"):
            f.series.append(dict(slug=slug, title_id=p["title_id"], ctype=t0.ctype, next_ep=eps[nxt][2],
                                 next_ep_no=eps[nxt][1], remaining=len(eps) - nxt, last_play=p["last_play"],
                                 completed_any=bool(p["completed"])))
    f.series.sort(key=lambda s: s["last_play"], reverse=True)
    opens = [e for e in events if e.type == "app_open" and e.id != trigger.id and e.session != trigger.session]
    f.last_open_before = max((e.t_est for e in opens), default=None)
    if f.plays:
        f.add("last_play", f.plays[-1][0], "event", f"event-play:{user}:{iso(f.plays[-1][0])}", f.plays[-1][0])
    f.add("plays_seen", len(f.plays), "derived", f"absence:play_started:{user}:n={len(events)}" if not f.plays
          else f"derived:plays:{user}:n={len(f.plays)}", None)
    f.add("completes_seen", len(f.completes), "derived", f"absence:episode_completed:{user}:n={len(events)}"
          if not f.completes else f"derived:completes:{user}:n={len(f.completes)}", None)
    return f
