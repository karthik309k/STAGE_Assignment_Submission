"""The four tests the brief requires: late/duplicate trigger, conflicting source, tool failure, corrected goal rule."""
from datetime import timedelta

from conftest import NOW, trig, raw_event
from oua.facts import build_facts
from oua.goals import evaluate_success
from oua.loader import make_ev
from oua.replay import apply_goal_rule_correction
from oua.runtime import h01
from oua.timeutil import iso, parse


# ------------------------------------------------------------------ 1. late or duplicate trigger
def test_duplicate_trigger_creates_no_second_action(builder, rig_factory):
    builder.viewer("U1")
    rig = rig_factory()
    t = trig("U1", eid="dup-1")
    d1 = rig.fire("U1", t)
    d2 = rig.agent("U1").on_trigger(trig("U1", at=NOW + timedelta(seconds=40), eid="dup-1"), NOW + timedelta(seconds=40))
    assert d1["status"] == "PUBLISHED"
    assert d2["status"] == "DUPLICATE_IGNORED" and d2["action"] == "NO_ACTION"
    assert d2["duplicate_of"] == d1["decision_id"]
    assert len(rig.actions("U1", "home_page")) == 1


def test_offline_buffered_trigger_is_not_a_live_session(builder, rig_factory):
    builder.viewer("U2")
    rig = rig_factory()
    d = rig.fire("U2", trig("U2", buf=3 * 86400))      # opened 3 days ago on the phone, delivered now
    assert d["status"] == "NO_ACTION" and d["no_action_reason"].startswith("stale trigger")
    assert d["trigger"]["event_age_s"] == 3 * 86400 and d["trigger"]["buffered"]
    assert rig.actions("U2") == []


def test_login_and_open_in_one_session_make_one_decision(builder, rig_factory):
    builder.viewer("U3")
    rig = rig_factory()
    d1 = rig.fire("U3", trig("U3", session="S1", typ="login_success"))
    d2 = rig.fire("U3", trig("U3", session="S1", typ="app_open"))
    assert d1["status"] == "PUBLISHED" and d2["no_action_reason"].startswith("session already handled")


def test_phone_clock_offset_does_not_make_a_trigger_stale(builder, rig_factory):
    builder.viewer("U4")
    rig = rig_factory()
    raw = raw_event("U4", "app_open", NOW, deliver=True)
    raw["occurred_at"] = raw["sent_at"] = iso(NOW - timedelta(hours=5))   # phone clock 5 h slow, but sent == occurred
    d = rig.fire("U4", make_ev(raw))
    assert d["status"] == "PUBLISHED"       # age = sent - occurred = 0, so it is live


# ------------------------------------------------------------------ 2. conflicting source
def test_three_dialects_app_setting_wins_and_conflicts_are_listed(builder, rig_factory):
    builder.viewer("U5", dialect="d1")                       # profile says d1
    rig = rig_factory()
    t = trig("U5", dialect="d3", props={"deeplink": "ott://c/cmp1/d5/title/T0101"})   # app d3, campaign d5
    d = rig.fire("U5", t)
    assert d["dialect"]["used"] == "d3" and d["dialect"]["source"] == "app_setting"
    assert {"profile:d1", "deeplink:d5"} <= set(d["dialect"]["conflicts"])
    page = rig.actions("U5", "home_page")[0]
    ids = [page["hero_title_id"]] + [t for r in page["rows"] for t in r["title_ids"]]
    assert ids and all(rig.pack.catalogue.get(i).dialect == "d3" for i in ids)


def test_newer_purchase_event_beats_stale_profile(builder, rig_factory):
    builder.viewer("U6", status="never")
    builder.ev("U6", "purchase_completed", parse("2026-09-30T10:00:00Z"), props={"plan_id": "P_MONTH", "is_trial": False})
    rig = rig_factory()
    t = trig("U6")
    f = build_facts(rig.pack, "U6", rig.pack.store.events("U6", NOW), t, NOW, rig.settings)
    assert f.sub_status == "active" and f.sub_source == "event"
    assert any(c.startswith("subscription:profile=never overridden") for c in f.conflicts)
    d = rig.fire("U6", t)
    assert d["goal"]["id"] != "G05"          # 'first paid plan' would be wrong for someone who already pays


def test_duplicate_profile_rows_and_consent_most_restrictive(builder, rig_factory):
    builder.viewer("U7", push=True, updated="2026-09-20T00:00:00.000Z")
    builder.profile("U7", push=False, status="never", updated="2026-09-29T00:00:00.000Z")
    rig = rig_factory()
    t = trig("U7")
    f = build_facts(rig.pack, "U7", rig.pack.store.events("U7", NOW), t, NOW, rig.settings)
    assert any(c.startswith("profile_duplicate_row") for c in f.conflicts)
    assert f.consent_push is False


def test_opt_out_event_overrides_profile_consent(builder, rig_factory):
    builder.viewer("U8", push=True)
    builder.ev("U8", "notification_opt_out", parse("2026-09-30T12:00:00Z"), props={"channel": "push"})
    rig = rig_factory()
    f = build_facts(rig.pack, "U8", rig.pack.store.events("U8", NOW), trig("U8"), NOW, rig.settings)
    assert f.consent_push is False


# ------------------------------------------------------------------ 3. tool failure
def _failing_trigger_id(user_prefix="x"):
    for i in range(1000):
        eid = f"{user_prefix}{i}"
        if h01("store", eid, "plan") < 0.2:
            return eid


def test_data_store_failure_falls_back_to_cached_facts_without_messages(builder, rig_factory):
    builder.viewer("U9", push=True)
    rig = rig_factory()
    first = rig.fire("U9", trig("U9", at=NOW - timedelta(hours=2)))
    assert first["status"] == "PUBLISHED"
    rig.world.deps.add("user_data_store", "degraded", NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
    d = rig.fire("U9", trig("U9", eid=_failing_trigger_id()))
    assert "data_store_timeout" in d["fallback_used"] and "stale_cached_facts" in d["fallback_used"]
    assert d["status"] == "PUBLISHED" and d["decided_in_ms"] < 60000
    assert rig.actions("U9", "push") == [] and rig.actions("U9", "whatsapp") == []     # no messages on stale data
    assert any("cached" in a for a in d["assumptions"])


def test_data_store_failure_for_unknown_viewer_gives_minimal_safe_page(builder, rig_factory):
    builder.viewer("U10")
    rig = rig_factory()
    rig.world.deps.add("user_data_store", "degraded", NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
    d = rig.fire("U10", trig("U10", eid=_failing_trigger_id("y")))
    assert "cold_minimal_page" in d["fallback_used"] and d["goal"] is None
    pg = rig.actions("U10", "home_page")[0]
    assert {r["row_type"] for r in pg["rows"]} <= {"popular_in_dialect", "new_releases"}


def test_model_timeout_falls_back_to_rules_within_budget(builder, rig_factory):
    builder.viewer("U11")
    rig = rig_factory()
    rig.world.model.faults["t-timeout"] = {"fault": "timeout", "seconds": 45}
    d = rig.fire("U11", trig("U11", eid="t-timeout"))
    assert "model_timeout" in d["fallback_used"] and d["goal"]["id"] == "G07"
    assert 2000 <= d["decided_in_ms"] < 60000          # waited our timeout (2 s), not the provider's 45 s


def test_model_outage_opens_circuit_breaker(builder, rig_factory):
    for i in range(5):
        builder.viewer(f"UO{i}")
    rig = rig_factory()
    rig.world.deps.add("model_provider", "unavailable", NOW - timedelta(minutes=1), NOW + timedelta(minutes=5))
    ds = [rig.fire(f"UO{i}") for i in range(5)]
    assert all(d["status"] == "PUBLISHED" for d in ds)
    assert sum("model_unavailable" in d["fallback_used"] for d in ds) == 3
    assert all("model_skipped_circuit_open" in d["fallback_used"] for d in ds[3:])


def test_malformed_model_json_is_retried_once_then_accepted(builder, rig_factory):
    builder.viewer("U12")
    rig = rig_factory()
    rig.world.model.faults["t-bad"] = {"fault": "malformed_output", "model_output": '{"goal_id": "G07", '}
    d = rig.fire("U12", trig("U12", eid="t-bad"))
    assert d["model_calls"] == 2 and "model_output_malformed" in d["fallback_used"] and d["status"] == "PUBLISHED"


def test_tool_budget_exhaustion_degrades_instead_of_crashing(builder, rig_factory):
    builder.viewer("U13")
    rig = rig_factory(max_tool_calls=1)
    d = rig.fire("U13")
    assert d["tool_calls"] <= 1 and d["status"] in ("NO_ACTION", "PUBLISHED") and "tool_budget_exceeded" in d["fallback_used"]


# ------------------------------------------------------------------ 4. corrected goal rule
def _g03_goal(world, user, started, hours=48):
    from oua.goals import new_goal_record, Elig
    e = Elig("G03", True, ["signed up within 2 days"], [], [], "strong", {})
    g = new_goal_record(e, started, world.rb, world.s)
    ag = world.agents[user] if user in world.agents else None
    return g


def test_rule_correction_touches_only_open_g03_goals(builder, rig_factory):
    for u in ("A", "B", "C", "D", "E"):
        builder.viewer("U-" + u)
    rig = rig_factory()
    w = rig.world
    from oua.goals import new_goal_record, Elig
    def give(u, gid, start):
        ag = rig.agent("U-" + u)
        g = new_goal_record(Elig(gid, True, [], [], [], "strong", {}), start, w.rb, w.s)
        ag.state["active_goal"] = g
        w.open_goals[gid].add("U-" + u)
        return g
    ga = give("A", "G03", NOW - timedelta(hours=3))      # open
    gb = give("B", "G03", NOW - timedelta(hours=10))     # open
    gc = give("C", "G03", NOW - timedelta(hours=60))     # deadline already passed
    gd = give("D", "G05", NOW - timedelta(hours=3))      # other goal
    ctrl = {"at": iso(NOW), "goal_id": "G03", "new_success": {"event": "play_started"}, "note": "fix"}
    out = apply_goal_rule_correction(w, ctrl, NOW)
    assert sorted(out["affected_viewers"]) == ["U-A", "U-B"] and out["reprocessed_unaffected_viewers"] == 0
    assert ga["rule_version"] == gb["rule_version"] == "v2"
    assert gc["rule_version"] == "v1" and gd["rule_version"] == "v1"
    assert w.rb.success_rule("G03", "v1")["event"] == "app_open" and w.rb.success_rule("G03", "v2")["event"] == "play_started"


def test_v1_and_v2_judge_the_same_events_differently(builder, rig_factory):
    builder.viewer("U-X")
    rig = rig_factory()
    w = rig.world
    from oua.goals import new_goal_record, Elig
    start = parse("2026-10-01T06:00:00Z")
    nxt = parse("2026-10-02T06:00:00Z")      # later IST day
    opened = make_ev(raw_event("U-X", "app_open", nxt))
    played = make_ev(raw_event("U-X", "play_started", nxt, props={"title_id": "T0001", "episode_id": "f1-d1-s1e1"}))
    g1 = new_goal_record(Elig("G03", True, [], [], [], "strong", {}), start, w.rb, w.s)
    apply_goal_rule_correction(w, {"at": iso(start), "goal_id": "G03", "new_success": {"event": "play_started"}}, start)
    g2 = dict(g1, rule_version="v2")
    end = parse("2026-10-10T00:00:00Z")
    assert evaluate_success(rig.pack, w.rb, g1, [opened], end)[0] == "succeeded"      # old rule: an open counts
    assert evaluate_success(rig.pack, w.rb, g2, [opened], end)[0] == "failed"         # corrected: it does not
    assert evaluate_success(rig.pack, w.rb, g2, [played], end)[0] == "succeeded"
