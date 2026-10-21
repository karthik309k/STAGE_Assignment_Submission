"""Outcome judge: after the deadline (or when new events arrive) judge each goal succeeded / failed / inconclusive from
ATTRIBUTABLE signals only, record it in the viewer's state and in outcomes.jsonl, and feed what was learned back."""
import glob
import json
import os
from collections import defaultdict, Counter

from .goals import RuleBook, evaluate_success
from .loader import make_ev, read_jsonl
from .timeutil import iso, parse


def rulebook_with_corrections(pack):
    rb = RuleBook(pack)
    for c in pack.controls:
        if c["type"] == "goal_rule_corrected":
            rb.apply_correction(c)
    return rb


def full_stream(pack, sim_dir):
    """history + replay-day live events + triggers + simulated outcomes, deduplicated by event_id."""
    by_user, seen, last = defaultdict(list), set(), None
    def add(ev):
        nonlocal last
        if ev.id in seen:
            return
        seen.add(ev.id)
        by_user[ev.user].append(ev)
    for lst in pack.store.by_user.values():
        for e in lst:
            add(e)
    for e in pack.live + pack.triggers:
        add(e)
    for raw in read_jsonl(os.path.join(sim_dir, "outcome_events.jsonl")):
        ev = make_ev(raw)
        add(ev)
        last = ev.rt if last is None or ev.rt > last else last
    for lst in by_user.values():
        lst.sort(key=lambda e: e.rt)
    return by_user, last


def judge_all(pack, rb, out_dir, sim_dir, world_states=None):
    stream, observed_until = full_stream(pack, sim_dir)
    state_dir = os.path.join(out_dir, "viewer_state")
    rows = []
    for path in sorted(glob.glob(os.path.join(state_dir, "*.json"))):
        st = json.load(open(path))
        goals = list(st["goal_history"]) + ([st["active_goal"]] if st["active_goal"] else [])
        st["outcomes"] = []
        for g in goals:
            status, refs, detail = evaluate_success(pack, rb, g, stream.get(st["viewer_id"], []), observed_until)
            row = dict(viewer_id=st["viewer_id"], arm=st["arm"], goal_id=g["goal_id"], started_at=g["started_at"],
                       deadline=g["deadline"], rule_version=g.get("rule_version"), rule_history=g.get("rule_history", []),
                       closed_reason=g.get("closed_reason"), status=status, evidence=refs, detail=detail,
                       observed_until=iso(observed_until))
            rows.append(row)
            st["outcomes"].append(row)
            L = st["learnings"]
            key = "goal_successes" if status == "succeeded" else ("goal_failures" if status == "failed" else None)
            if key:
                L[key][g["goal_id"]] = L[key].get(g["goal_id"], 0) + 1
                L["notes"].append(f"{g['goal_id']} {status} (started {g['started_at']}): "
                                  + ("keep using" if status == "succeeded" else "after 2 failures this goal is tried last next time"))
        with open(path, "w") as f:
            json.dump(st, f, indent=1, default=str)
    with open(os.path.join(out_dir, "outcomes.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows, stream, observed_until
