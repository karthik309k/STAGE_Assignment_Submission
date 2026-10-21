"""Goal catalogue: measurability audit, versioned success rules, eligibility, selection and success evaluation.

INTERPRETATIONS OF UNDERSPECIFIED RULES (each is also recorded as an assumption in every decision that uses it)
  G06 'recently lapsed'   : a subscription_lapsed event received within the last 30 days. A lapsed profile with no lapse
                            event in the pack has unknown recency -> NOT eligible (we will not guess).
  G09 'rarely watches'    : active subscriber, account older than 14 days, plays on <= 1 IST day in the last 14 days.
  G12 'had stopped'       : account older than 14 days and no earlier app_open (other than the trigger's session)
                            in the 14 days before the trigger.
  'never ...' claims (G01, G02, G05 inference): trusted only when the viewer's whole history is visible, i.e. they signed
                            up inside the 30-day event window. Older accounts cannot prove 'never' from 30 days of data.
UNMEASURABLE GOALS (never selected): G10 has success.event = null; G11's success event tv_app_open appears nowhere in the
  event stream (history, live, triggers), so success could never be observed.
"""
from collections import namedtuple
from datetime import timedelta

from .timeutil import ist_day, iso

Elig = namedtuple("Elig", "goal_id eligible reasons refs assumptions strength context")

# Priority when several goals are eligible. Rationale (see ARCHITECTURE.md): time-critical, high-value and most
# specific first; broad/low-evidence defaults last.
PRIORITY = ["G04", "G01", "G03", "G02", "G06", "G07", "G12", "G05:intent", "G09", "G08", "G05"]


class RuleBook:
    """Versioned success rules. A correction creates a new version; goals remember which version judges them."""

    def __init__(self, pack):
        self.goals = {g["goal_id"]: g for g in pack.goals_cfg["goals"]}
        self.versions = {gid: [dict(version="v1", success=dict(g["success"]), effective_at=None, note="initial")]
                         for gid, g in self.goals.items()}
        self.unmeasurable = {}
        for gid, g in self.goals.items():
            ev = g["success"]["event"]
            if ev is None:
                self.unmeasurable[gid] = "success.event is null: 'engages meaningfully' has no observable signal"
            elif ev not in pack.known_event_types:
                self.unmeasurable[gid] = f"success event '{ev}' never occurs in the event stream"

    def current_version(self, gid):
        return self.versions[gid][-1]["version"]

    def success_rule(self, gid, version=None):
        for v in self.versions[gid]:
            if v["version"] == version:
                return dict(v["success"])
        return dict(self.versions[gid][-1]["success"])

    def window_hours(self, gid):
        return self.goals[gid]["success"]["window_hours"]

    def apply_correction(self, ctrl):
        gid = ctrl["goal_id"]
        n = len(self.versions[gid]) + 1
        new = dict(self.versions[gid][-1]["success"])
        new.update(ctrl["new_success"])
        self.versions[gid].append(dict(version=f"v{n}", success=new, effective_at=ctrl["at"], note=ctrl.get("note", "")))
        return f"v{n}"


def new_release_candidates(pack, dialect, language, now, days=30):
    """Titles in `dialect`, available at `now`, released in the `days` before `now`; one per content_slug."""
    out, seen = [], set()
    cat = pack.catalogue
    for slug, variants in cat.by_slug.items():
        t = cat.pick_variant(slug, dialect, language, now)
        if t and now - timedelta(days=days) <= t.release <= now and slug not in seen:
            seen.add(slug)
            out.append(t)
    out.sort(key=lambda t: (t.release, t.id), reverse=True)
    return out


def evaluate(gid, f, pack, rb, settings):
    """Eligibility of goal `gid` for the viewer described by facts `f`. Pure: no I/O, no model."""
    rule = f"goal_rule:{gid}@{rb.current_version(gid)}"
    no = lambda why: Elig(gid, False, [why], [], [], "none", {})
    if gid in rb.unmeasurable:
        return no("unmeasurable: " + rb.unmeasurable[gid])
    st = f.sub_status
    sub_ref = f.items["subscription_status"].ref if "subscription_status" in f.items else None
    age = f.account_age_days
    cat = pack.catalogue
    if gid == "G01":
        if f.signup_at is None or age is None or age > settings.new_user_days:
            return no("not signed up within 7 days (or signup unknown)")
        if not f.history_complete:
            return no("history not complete enough to prove 'never played'")
        if f.plays:
            return no("has already started a play")
        return Elig(gid, True, ["signed up within 7 days; no play_started in full history"],
                    [rule, f.items["signup_at"].ref, f.items["plays_seen"].ref], [
                        "all of the viewer's events are in the pack (signup is inside the event window)"], "strong", {})
    if gid == "G02":
        if not f.history_complete:
            return no("cannot prove 'never completed an episode' without the viewer's full history")
        if f.completes:
            return no("has completed an episode")
        return Elig(gid, True, ["full history visible; no episode_completed"],
                    [rule, f.items["signup_at"].ref, f.items["completes_seen"].ref],
                    ["all of the viewer's events are in the pack"], "strong", {})
    if gid == "G03":
        if f.signup_at is None or age is None or age > 2:
            return no("not signed up within 2 days (or signup unknown)")
        return Elig(gid, True, ["signed up within 2 days"], [rule, f.items["signup_at"].ref], [], "strong", {})
    if gid == "G04":
        if st != "trial_active":
            return no(f"subscription_status={st}, not trial_active")
        return Elig(gid, True, ["subscription_status=trial_active"], [rule, sub_ref], [], "strong", {})
    if gid == "G05":
        if st != "never":
            return no(f"subscription_status={st}, not never")
        strength = "medium" if f.sub_source in ("event", "profile") else "weak"
        return Elig(gid, True, ["never had a paid plan"], [rule, sub_ref],
                    [] if f.sub_source != "inferred" else ["status inferred from absence of subscription events"],
                    strength, {"intent": f.paywall_14d})
    if gid == "G06":
        if st != "lapsed":
            return no(f"subscription_status={st}, not lapsed")
        if f.lapsed_at is None:
            return no("lapsed but no lapse event in the pack: recency unknown, will not guess")
        if f.now - f.lapsed_at > timedelta(days=settings.recent_lapse_days):
            return no("lapsed more than 30 days ago")
        return Elig(gid, True, ["lapsed within the last 30 days"], [rule, sub_ref],
                    ["'recently lapsed' interpreted as a lapse event within 30 days"], "strong", {})
    if gid == "G07":
        if not f.dialect:
            return no("no valid dialect: nothing promotable")
        for s in f.series:
            if f.now - s["last_play"] < timedelta(days=settings.resume_gap_days):
                continue
            t = cat.pick_variant(s["slug"], f.dialect, f.language, f.now)
            if t is None or t.flagged:
                continue     # flagged records (agent-directed text) are never promoted
            return Elig(gid, True, [f"unfinished {s['ctype']} {s['slug']}: next episode {s['next_ep']}, idle >=3 days"],
                        [rule, f"catalogue:{t.id}", f"episodes:{s['slug']}", f"derived:plays:{f.user}"],
                        ["viewer has not played any episode of this series since the last play"], "strong",
                        dict(slug=s["slug"], title_id=t.id, next_ep=s["next_ep"], next_ep_no=s["next_ep_no"]))
        return no("no unfinished series idle >=3 days that is available in the viewer's dialect")
    if gid == "G08":
        if st != "active":
            return no(f"subscription_status={st}, not active")
        if not f.dialect:
            return no("no valid dialect")
        c = new_release_candidates(pack, f.dialect, f.language, f.now, settings.new_release_days)
        if not c:
            return no("no new release available in the viewer's dialect")
        return Elig(gid, True, ["active subscriber and recent releases exist in dialect"],
                    [rule, sub_ref, f"catalogue:{c[0].id}"], [], "medium", {"title_id": c[0].id})
    if gid == "G09":
        if st != "active":
            return no(f"subscription_status={st}, not active")
        if age is None or age < settings.absence_days:
            return no("account younger than 14 days: 'rarely watches' not provable")
        if len(f.play_days_14d) > 1:
            return no(f"plays on {len(f.play_days_14d)} IST days in last 14")
        return Elig(gid, True, [f"active; plays on {len(f.play_days_14d)} IST day(s) in last 14"],
                    [rule, sub_ref, f"derived:plays:{f.user}"],
                    ["'rarely watches' = plays on <= 1 IST day in 14 days, account older than 14 days"], "medium", {})
    if gid == "G12":
        if age is None or age < settings.absence_days:
            return no("account younger than 14 days")
        if f.last_open_before is not None and f.now - f.last_open_before <= timedelta(days=settings.absence_days):
            return no("opened the app within the last 14 days")
        return Elig(gid, True, ["no earlier app_open in the 14 days before this trigger"],
                    [rule, f"absence:app_open_14d:{f.user}"],
                    ["absence judged from received events only; other devices are unknown"], "medium", {})
    return no("no rule implemented")  # G10, G11 return earlier as unmeasurable


def rank_eligible(f, pack, rb, settings, learnings=None):
    """All eligible goals in priority order, plus a list of (goal, reason) for the ones rejected."""
    learnings = learnings or {}
    fails = learnings.get("goal_failures", {})
    results = {gid: evaluate(gid, f, pack, rb, settings) for gid in rb.goals}
    ordered, key_used = [], set()
    for key in PRIORITY:
        gid, _, tag = key.partition(":")
        e = results[gid]
        if not e.eligible or gid in key_used:
            continue
        if tag == "intent" and not e.context.get("intent"):
            continue
        key_used.add(gid)
        ordered.append(e)
    # a goal that already failed twice for this viewer is tried last (learning fed back from the judge)
    ordered.sort(key=lambda e: 1 if fails.get(e.goal_id, 0) >= 2 else 0)
    rejected = [(gid, e.reasons[0]) for gid, e in results.items() if not e.eligible]
    return ordered, rejected


def new_goal_record(elig, start, rb, settings):
    gid = elig.goal_id
    hours = rb.window_hours(gid)
    return dict(goal_id=gid, name=rb.goals[gid]["name"], started_at=iso(start),
                deadline=iso(start + timedelta(hours=hours)), window_hours=hours,
                rule_version=rb.current_version(gid), success=rb.success_rule(gid), status="open",
                context=dict(elig.context), assumptions=list(elig.assumptions))


# ----------------------------------------------------------------------------------------- success evaluation
def evaluate_success(pack, rb, goal, events, observed_until):
    """Judge one goal from attributable events only.

    Attributable = occurred (best estimate) after the goal started and no later than its deadline. Offline-buffered
    events whose real time precedes the goal start are not counted. Returns (status, evidence_refs, detail)."""
    from .timeutil import parse
    gid = goal["goal_id"]
    start, deadline = parse(goal["started_at"]), parse(goal["deadline"])
    if gid in rb.unmeasurable:
        return "inconclusive", [], "unmeasurable goal: " + rb.unmeasurable[gid]
    succ = rb.success_rule(gid, goal.get("rule_version"))
    window = [e for e in events if start < e.t_est <= deadline]
    cat = pack.catalogue
    hits = []
    if gid in ("G01", "G12"):
        hits = [e for e in window if e.type == "play_started"]
    elif gid == "G02":
        hits = [e for e in window if e.type == "episode_completed"]
    elif gid == "G03":
        later = [e for e in window if ist_day(e.t_est) > ist_day(start)]
        hits = [e for e in later if e.type == succ["event"]]
    elif gid in ("G04", "G05"):
        hits = [e for e in window if e.type == "purchase_completed" and not e.props.get("is_trial")]
    elif gid == "G06":
        hits = [e for e in window if e.type == "purchase_completed" and not e.props.get("is_trial")]
    elif gid == "G07":
        slug = goal["context"].get("slug")
        hits = [e for e in window if e.type == "play_started"
                and (cat.get(e.props.get("title_id")) and cat.get(e.props.get("title_id")).slug == slug)]
    elif gid == "G08":
        for e in window:
            t = cat.get(e.props.get("title_id")) if e.type == "play_started" else None
            if t and start - timedelta(days=30) <= t.release <= start:
                hits.append(e)
    elif gid == "G09":
        plays = [e for e in window if e.type == "play_started"]
        days = {ist_day(e.t_est) for e in plays}
        if len(days) >= 3:
            hits = plays
    else:
        return "inconclusive", [], "no judging rule implemented"
    if hits:
        return "succeeded", [h.ref for h in hits[:3]], f"{len(hits)} qualifying event(s) in window (rule {goal.get('rule_version')})"
    if observed_until is not None and observed_until >= deadline:
        return "failed", [], "window fully observed with no qualifying event"
    return "inconclusive", [], "observation ended before the goal deadline"
