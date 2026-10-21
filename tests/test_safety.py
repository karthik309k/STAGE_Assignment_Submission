"""Extra tests: verifier can reject, holdout gets nothing, quiet hours, caps, injection, withdrawal, replanning, restart."""
import pytest
from datetime import timedelta

from conftest import NOW, trig, raw_event
from oua.agent import ViewerAgent
from oua.facts import build_facts
from oua.goals import rank_eligible
from oua.interventions import build_page, plan_message
from oua.loader import make_ev
from oua.runtime import World
from oua.state import StateStore
from oua.timeutil import in_quiet_hours, next_allowed_send, parse, iso, IST, ist_day


def _env(rig, user, arm="agent", ledger=(), t=None):
    t = t or trig(user)
    ev = rig.pack.store.events(user, NOW)
    return dict(facts=build_facts(rig.pack, user, ev, t, NOW, rig.settings), events=ev, store=rig.pack.store, arm=arm,
                handled_triggers={}, ledger_days=set(ledger))


def _prop(user, t=None, page=None, msgs=(), goal=None, dialect="d1"):
    return dict(user=user, trigger=t or trig(user), now=NOW, goal=goal, evidence=[], dialect=dialect, page=page, messages=list(msgs))


def _push(**kw):
    m = dict(type="push", channel="push", title_id="T0001", goal_id="G06", send_at=iso(NOW + timedelta(hours=2)),
             text={"title": "Film 1", "body": "Aapke liye: Film 1. Abhi dekhiye."}, deeplink="ott://title/T0001")
    m.update(kw)
    return m


def _failed(res):
    return {f["check"] for f in res["failed"]}


# ----------------------------------------------------------------------------- holdout
def test_holdout_viewer_gets_zero_actions_but_a_shadow_goal(builder, rig_factory):
    builder.viewer("H1", arm="holdout")
    rig = rig_factory()
    d = rig.fire("H1")
    assert d["status"] == "NO_ACTION" and d["no_action_reason"].startswith("holdout")
    assert d["shadow_goal"]["goal_id"] == "G07" and rig.actions("H1") == []


def test_verifier_blocks_any_action_for_holdout(builder, rig_factory):
    builder.viewer("H2", arm="holdout")
    rig = rig_factory()
    page = build_page(rig.pack, _env(rig, "H2")["facts"], "G07", {}, rig.settings, NOW)
    res = rig.world.verifier.verify(_prop("H2", page=page), _env(rig, "H2", arm="holdout"))
    assert "holdout" in _failed(res)


# ----------------------------------------------------------------------------- consent, quiet hours, cap, copy, template
def test_verifier_rejects_push_without_consent(builder, rig_factory):
    builder.viewer("C1", push=False)
    rig = rig_factory()
    assert "consent" in _failed(rig.world.verifier.verify(_prop("C1", msgs=[_push()]), _env(rig, "C1")))


def test_whatsapp_needs_consent_and_hindi(builder, rig_factory):
    builder.viewer("C2", wa=True, lang="hin")
    rig = rig_factory()
    wa = dict(type="whatsapp", channel="whatsapp", title_id="T0001", goal_id="G06", send_at=iso(NOW + timedelta(hours=2)),
              template_id="wa_recommend_title_v1", variables={"title_name": "Film 1", "dialect_name": "d1"})
    assert _failed(rig.world.verifier.verify(_prop("C2", msgs=[wa]), _env(rig, "C2"))) == set()
    bad = dict(wa, variables={"title_name": "Film 1 - FREE for you", "dialect_name": "d1"})
    assert "template_values_from_catalogue_or_pricing" in _failed(rig.world.verifier.verify(_prop("C2", msgs=[bad]), _env(rig, "C2")))
    assert "template_approved" in _failed(rig.world.verifier.verify(_prop("C2", msgs=[dict(wa, template_id="wa_made_up")]), _env(rig, "C2")))


def test_quiet_hours_are_ist_and_messages_move_to_morning():
    late = parse("2026-10-01T16:30:00Z")            # 22:00 IST
    assert in_quiet_hours(late) and in_quiet_hours(parse("2026-10-01T02:00:00Z"))     # 07:30 IST
    assert not in_quiet_hours(parse("2026-10-01T03:00:00Z"))                          # 08:30 IST
    t = next_allowed_send(late, late + timedelta(hours=2))
    assert not in_quiet_hours(t) and t.astimezone(IST).hour == 8 and t.astimezone(IST).day == 2


def test_verifier_rejects_message_inside_quiet_hours(builder, rig_factory):
    builder.viewer("Q1")
    rig = rig_factory()
    m = _push(send_at="2026-10-01T18:00:00.000Z")   # 23:30 IST
    assert "quiet_hours" in _failed(rig.world.verifier.verify(_prop("Q1", msgs=[m]), _env(rig, "Q1")))


def test_frequency_cap_one_push_or_whatsapp_per_ist_day(builder, rig_factory):
    builder.viewer("F1", push=True, wa=True)
    rig = rig_factory()
    a = _push(send_at="2026-10-01T09:00:00.000Z")
    b = dict(a, type="whatsapp", template_id="wa_recommend_title_v1", variables={"title_name": "Film 1", "dialect_name": "d1"},
             send_at="2026-10-01T10:00:00.000Z")
    b.pop("text"); b.pop("deeplink")
    res = rig.world.verifier.verify(_prop("F1", msgs=[a, b]), _env(rig, "F1"))
    assert "frequency_cap" in _failed(res)                     # both channels share one daily budget
    res2 = rig.world.verifier.verify(_prop("F1", msgs=[a]), _env(rig, "F1", ledger={"2026-10-01"}))
    assert "frequency_cap" in _failed(res2)                    # already used today per the ledger


def test_planner_does_not_message_a_viewer_messaged_recently(builder, rig_factory):
    _lapsed_viewer(builder, "F2")
    builder.ev("F2", "message_delivered", parse("2026-09-30T05:00:00Z"), props={"channel": "push"})
    rig = rig_factory()
    t = trig("F2")
    f = build_facts(rig.pack, "F2", rig.pack.store.events("F2", NOW), t, NOW, rig.settings)
    ordered, _ = rank_eligible(f, rig.pack, rig.world.rb, rig.settings)
    g = next(e for e in ordered if e.goal_id == "G06")
    goal = dict(goal_id="G06", deadline=iso(NOW + timedelta(days=14)), context={})
    page = build_page(rig.pack, f, "G06", {}, rig.settings, NOW)
    m, why = plan_message(rig.pack, f, goal, rig.agent("F2").state, rig.settings, NOW, page)
    assert m is None and "within the last 3 days" in why


def test_push_copy_rules(builder, rig_factory):
    builder.viewer("P1")
    rig = rig_factory()
    v = rig.world.verifier
    for bad, check in [(dict(text={"title": "x" * 41, "body": "ok"}), "copy_length"),
                       (dict(text={"title": "Film", "body": "y" * 121}), "copy_length"),
                       (dict(text={"title": "Last chance!", "body": "ok"}), "copy_forbidden_phrase"),
                       (dict(text={"title": "Film", "body": "Only ₹99 today"}), "copy_price_claim"),
                       (dict(text={"title": "Film", "body": "Get 50% off"}), "copy_price_claim"),
                       (dict(deeplink="https://evil.example/x"), "deeplink"),
                       (dict(deeplink="ott://title/T0002"), "deeplink")]:
        assert check in _failed(v.verify(_prop("P1", msgs=[_push(**bad)]), _env(rig, "P1"))), bad
    assert _failed(v.verify(_prop("P1", msgs=[_push(text={"title": "Film 1", "body": "Real price ₹149 monthly"})]), _env(rig, "P1"))) == set()


# ----------------------------------------------------------------------------- content safety
def test_injected_catalogue_instruction_is_flagged_and_never_promoted(builder, rig_factory):
    builder.viewer("I1")
    rig = rig_factory()
    inj = rig.pack.catalogue.get("T0031")
    assert inj.flagged and "T0031" in rig.pack.catalogue.flagged
    d = rig.fire("I1")
    assert "title:T0031" not in d["content"]
    page = dict(dialect="d1", hero_title_id="T0031", rows=[], evidence=[], assumptions=[])
    res = rig.world.verifier.verify(_prop("I1", page=page), _env(rig, "I1"))
    assert "injected_content" in _failed(res)
    assert len(rig.actions("I1")) <= 2                         # the description never caused a broadcast


def test_page_rows_below_three_titles_are_dropped_and_rejected(builder, rig_factory):
    builder.viewer("R1", dialect="d2")
    rig = rig_factory()
    f = build_facts(rig.pack, "R1", rig.pack.store.events("R1", NOW), trig("R1", dialect="d2"), NOW, rig.settings)
    page = build_page(rig.pack, f, "G05", {}, rig.settings, NOW)
    assert all(len(r["title_ids"]) >= 3 for r in page["rows"]) and page["hero_title_id"]      # d2 has only 3 titles
    bad = dict(dialect="d2", hero_title_id="T0201", rows=[{"row_type": "genre", "title_ids": ["T0202", "T0203"]}], evidence=[], assumptions=[])
    res = rig.world.verifier.verify(_prop("R1", t=trig("R1", dialect="d2"), page=bad, dialect="d2"), _env(rig, "R1"))
    assert "page_structure" in _failed(res)


def test_wrong_dialect_and_duplicate_slug_are_rejected(builder, rig_factory):
    builder.viewer("W1")
    rig = rig_factory()
    page = dict(dialect="d1", hero_title_id="T0101", rows=[{"row_type": "genre", "title_ids": ["T0001", "T0002", "T0003"]}], evidence=[], assumptions=[])
    assert "title_dialect" in _failed(rig.world.verifier.verify(_prop("W1", page=page), _env(rig, "W1")))
    dup = dict(page, hero_title_id="T0001", rows=[{"row_type": "genre", "title_ids": ["T0001", "T0032", "T0003"]}])   # T0032 = en variant of f1
    assert "page_structure" in _failed(rig.world.verifier.verify(_prop("W1", page=dup), _env(rig, "W1")))


def test_continue_watching_row_type_is_not_allowed(builder, rig_factory):
    builder.viewer("W2")
    rig = rig_factory()
    page = dict(dialect="d1", hero_title_id=None, rows=[{"row_type": "continue_watching", "title_ids": ["T0001", "T0002", "T0003"]}], evidence=[], assumptions=[])
    assert "page_structure" in _failed(rig.world.verifier.verify(_prop("W2", page=page), _env(rig, "W2")))


def test_one_title_per_content_slug_prefers_viewer_language(builder, rig_factory):
    builder.viewer("L1", lang="hin")
    rig = rig_factory()
    c = rig.pack.catalogue
    assert c.pick_variant("f1-d1", "d1", "en", NOW).id == "T0032" and c.pick_variant("f1-d1", "d1", "hin", NOW).id == "T0001"


# ----------------------------------------------------------------------------- withdrawal
def test_withdrawn_title_is_not_used_and_live_pages_are_replaced(builder, rig_factory):
    builder.viewer("X1")
    rig = rig_factory()
    d = rig.fire("X1")
    page = rig.actions("X1", "home_page")[0]
    victim = page["rows"][0]["title_ids"][1]
    slug = rig.pack.catalogue.get(victim).slug
    later = NOW + timedelta(minutes=10)
    rig.pack.catalogue.withdraw(victim, later)
    assert slug in rig.world.live_pages and "X1" in rig.world.live_pages[slug]
    rec = rig.agent("X1").on_withdrawal(slug, later)
    new = rig.actions("X1", "home_page")[-1]
    assert rec["replacement_page"] == new["action_id"] and new["replaces"] == page["action_id"]
    ids = [new["hero_title_id"]] + [t for r in new["rows"] for t in r["title_ids"]]
    assert victim not in ids
    f = build_facts(rig.pack, "X1", rig.pack.store.events("X1", later), trig("X1", at=later), later, rig.settings)
    assert victim not in [t.id for t in __import__("oua.interventions", fromlist=["x"]).candidate_pool(rig.pack, "d1", "hin", later)]


# ----------------------------------------------------------------------------- model claims & goals
def test_unsupported_model_eligibility_claim_is_rejected_and_recorded(builder, rig_factory):
    builder.viewer("M1", status="never")
    rig = rig_factory()
    rig.world.model.faults["t-claim"] = {"fault": "unsupported_claim", "model_output": {
        "goal_id": "G04", "eligible": True, "confidence": 0.97, "reason": "billing confirms trial", "evidence": []}}
    d = rig.fire("M1", trig("M1", eid="t-claim"))
    assert d["goal"]["id"] == "G07" and len(d["unsupported_model_claims"]) == 1
    assert "subscription_status=never" in d["unsupported_model_claims"][0]["problem"]
    assert "model_claim_rejected_by_verifier" in d["fallback_used"]


def test_unmeasurable_goals_are_never_chosen(builder, rig_factory):
    builder.viewer("G1")
    rig = rig_factory()
    rb = rig.world.rb
    assert set(rb.unmeasurable) == {"G10", "G11"}
    f = build_facts(rig.pack, "G1", rig.pack.store.events("G1", NOW), trig("G1"), NOW, rig.settings)
    ordered, rejected = rank_eligible(f, rig.pack, rb, rig.settings)
    assert not {"G10", "G11"} & {e.goal_id for e in ordered}
    assert all("unmeasurable" in r for g, r in rejected if g in ("G10", "G11"))


def test_unknown_signup_history_does_not_license_never_claims(builder, rig_factory):
    builder.viewer("G2")                 # signed up in March: older than the 30-day window
    rig = rig_factory()
    f = build_facts(rig.pack, "G2", rig.pack.store.events("G2", NOW), trig("G2"), NOW, rig.settings)
    ordered, rejected = rank_eligible(f, rig.pack, rig.world.rb, rig.settings)
    assert "G02" not in {e.goal_id for e in ordered}


def test_invalid_dialect_means_no_page(builder, rig_factory):
    builder.viewer("D1")
    rig = rig_factory()
    d = rig.fire("D1", trig("D1", dialect="d7"))
    assert d["status"] == "NO_ACTION" and "no valid dialect" in d["no_action_reason"] and rig.actions("D1") == []


def test_no_eligible_goal_is_a_recorded_decision(builder, rig_factory):
    builder.profile("N1", status="lapsed")                  # lapsed, no lapse event: recency unknown; no series
    builder.ev("N1", "app_open", NOW - timedelta(days=3))   # visited recently, so not a G12 absence case
    builder.arms["N1"] = "agent"
    rig = rig_factory()
    d = rig.fire("N1")
    assert d["status"] == "NO_ACTION" and "no eligible measurable goal" in d["no_action_reason"] and d["confidence"] > 0


# ----------------------------------------------------------------------------- replanning & durability
def _lapsed_viewer(builder, u):
    builder.viewer(u, push=True)
    builder.ev(u, "purchase_completed", parse("2026-08-20T00:00:00Z"), props={"plan_id": "P_MONTH", "is_trial": False})
    builder.ev(u, "subscription_lapsed", parse("2026-09-25T00:00:00Z"), props={})
    builder.profiles[-1][2] = "lapsed"


def test_purchase_after_decision_cancels_pending_message_and_replans(builder, rig_factory):
    _lapsed_viewer(builder, "PU")
    rig = rig_factory()
    d = rig.fire("PU")
    assert d["goal"]["id"] == "G06" and len(rig.actions("PU", "push")) == 1
    push = rig.actions("PU", "push")[0]
    assert push["status"] == "scheduled"
    buy = make_ev(raw_event("PU", "purchase_completed", NOW + timedelta(minutes=2), props={"plan_id": "P_MONTH", "is_trial": False}, deliver=True))
    rig.pack.store.ingest(buy)
    rig.agent("PU").observe(buy, buy.rt)
    assert push["status"] == "cancelled" and "purchase" in push["cancel_reason"]
    st = rig.agent("PU").state
    assert st["goal_history"][-1]["closed_reason"].startswith("success_observed")
    # re-planned from the purchase goal to the next best goal for a payer: the idle series (G07 outranks G08)
    assert st["active_goal"]["goal_id"] == "G07" and st["replans"][-1]["new_goal"] == "G07"


def test_opt_out_cancels_scheduled_push(builder, rig_factory):
    _lapsed_viewer(builder, "OO")
    rig = rig_factory()
    rig.fire("OO")
    push = rig.actions("OO", "push")[0]
    ev = make_ev(raw_event("OO", "notification_opt_out", NOW + timedelta(minutes=5), props={"channel": "push"}, deliver=True))
    rig.pack.store.ingest(ev)
    rig.agent("OO").observe(ev, ev.rt)
    assert push["status"] == "cancelled"


def test_restart_is_idempotent_because_state_is_durable(builder, tmp_path):
    builder.viewer("S1")
    pack = builder.build()
    sd = str(tmp_path / "vs")
    w1 = World(pack, __import__("oua.config", fromlist=["x"]).Settings(), sd)
    t = trig("S1", eid="same")
    pack.store.ingest(t)
    ag1 = ViewerAgent("S1", w1)
    assert ag1.on_trigger(t, t.rt)["status"] == "PUBLISHED"
    w2 = World(pack, __import__("oua.config", fromlist=["x"]).Settings(), sd)       # "process restart"
    d = ViewerAgent("S1", w2).on_trigger(t, t.rt)
    assert d["status"] == "DUPLICATE_IGNORED" and w2.actions == []


def test_every_decision_carries_evidence_confidence_reason_assumptions_and_checks(builder, rig_factory):
    builder.viewer("E1")
    rig = rig_factory()
    d = rig.fire("E1")
    for k in ("decision_id", "viewer_id", "trigger", "goal", "action", "dialect", "content", "confidence", "confidence_reason",
              "evidence", "assumptions", "checks", "decided_in_ms", "status", "model_calls", "tool_calls"):
        assert k in d and d[k] not in (None, "") or k in ("assumptions",), k
    assert d["evidence"] and d["checks"]["passed"] and not d["checks"]["failed"] and d["goal"]["deadline"]
    assert d["model_calls"] <= 8 and d["tool_calls"] <= 20
