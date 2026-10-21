"""Replay harness. Feeds triggers, live events and control events in delivery order; models a worker pool with
fresh-first prioritisation and load shedding; injects the faults named in the pack; measures latency.

Time is virtual (the replay runs far faster than real time) but every decision's latency is real compute time plus the
SIMULATED service time of the dependencies it touched, plus queue wait. published_at = deliver_at + that total."""
import heapq
import json
import os
from collections import defaultdict, Counter
from datetime import timedelta

from .agent import ViewerAgent
from .runtime import World
from .timeutil import iso, parse


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def apply_goal_rule_correction(world, ctrl, at):
    """Version the rule and switch ONLY the goals of that id that are still open at `at` (looked up through the
    open-goal index, not by scanning or re-deciding every viewer). Goals closed earlier keep the version they ran under."""
    gid = ctrl["goal_id"]
    ver = world.rb.apply_correction(ctrl)
    affected = []
    for u in sorted(world.open_goals.get(gid, ())):
        g = world.agents[u].state["active_goal"]
        if g and g["goal_id"] == gid and parse(g["deadline"]) > at:
            g["rule_version"], g["success"] = ver, world.rb.success_rule(gid, ver)
            g.setdefault("rule_history", []).append(dict(at=iso(at), to=ver, note=ctrl.get("note", "")))
            affected.append(u)
            rec = dict(viewer_id=u, at=iso(at), reason=f"goal rule {gid} corrected -> {ver}", old_goal=gid, new_goal=gid,
                       cancelled_actions=[])
            world.agents[u].state["replans"].append(rec)
            world.replans.append(rec)
    return dict(goal_id=gid, new_version=ver, at=ctrl["at"], affected_open_goals=len(affected), affected_viewers=affected,
                total_viewers_with_agents=len(world.agents), reprocessed_unaffected_viewers=0)


def run_replay(pack, settings, out_dir, model=None, verbose=True, resume=False):
    import shutil
    os.makedirs(out_dir, exist_ok=True)
    state_dir = os.path.join(out_dir, "viewer_state")
    if not resume and os.path.isdir(state_dir):
        shutil.rmtree(state_dir)      # a fresh run starts from clean viewer state; --resume keeps it (idempotent)
    world = World(pack, settings, state_dir, model=model)
    ops = []   # operational log

    for t in pack.catalogue.flagged:
        ops.append(dict(at=None, type="injected_instruction_ignored", title_id=t,
                        detail="catalogue record contains text addressed to the agent; treated as content, excluded from promotion"))

    # ---- fault windows come from control events but are only *used* when a dependency is touched at that time ---------
    for c in pack.controls:
        if c["type"] == "dependency_unavailable":
            world.deps.add(c["dependency"], "unavailable", parse(c["at"]), parse(c["until"]))
        elif c["type"] == "dependency_degraded":
            world.deps.add(c["dependency"], "degraded", parse(c["at"]), parse(c["until"]))

    # ---- timeline ------------------------------------------------------------------------------------------------
    order = {"control": 0, "live": 1, "trigger": 2}
    timeline = [(parse(c["at"]), 0, "control", c) for c in pack.controls]
    timeline += [(e.deliver_at or e.rt, 1, "live", e) for e in pack.live]
    timeline += [(e.deliver_at or e.rt, 2, "trigger", e) for e in pack.triggers]
    timeline.sort(key=lambda x: (x[0], x[1]))

    workers = [timeline[0][0]] * settings.workers
    heapq.heapify(workers)
    queue = []            # waiting triggers
    pending_heap = []     # (send_at, viewer) scheduled messages
    assigned = {}
    assignments = []
    latency_rows = []
    rule_fix = []

    def agent_for(u):
        if u not in world.agents:
            world.agents[u] = ViewerAgent(u, world)
        return world.agents[u]

    def release_until(t):
        while pending_heap and pending_heap[0][0] <= t:
            at, u = heapq.heappop(pending_heap)
            agent_for(u).release_due(at)

    def pick(start):
        cands = [q for q in queue if q[0] <= start]
        cls = lambda ev: 0 if ev.buf_s <= settings.fresh_trigger_s else (1 if ev.buf_s <= settings.stale_trigger_s else 2)
        best = min(cands, key=lambda q: (cls(q[1]), q[0], q[2]))
        queue.remove(best)
        return best

    def dispatch(until):
        while queue:
            free = heapq.heappop(workers)
            first = min(q[0] for q in queue)
            start = max(free, first)
            if until is not None and start > until:
                heapq.heappush(workers, free)
                return
            release_until(start)
            deliver, ev, _seq = pick(start)
            wait = (start - deliver).total_seconds()
            shed = wait > settings.drop_after_wait_s
            ag = agent_for(ev.user)
            # measurement-only assignment (same planner, pre-treatment facts) for the intention-to-treat comparison
            if ev.user not in assigned and ev.id not in ag.state["processed_triggers"] and ev.buf_s <= settings.stale_trigger_s \
                    and ev.session not in ag.state["handled_sessions"] and not shed:
                g = ag._shadow_goal(ev, start)
                assigned[ev.user] = True
                assignments.append(dict(viewer_id=ev.user, arm=pack.arms.get(ev.user, "unassigned"), trigger_event_id=ev.id,
                                        goal_id=g["goal_id"] if g else None, context=g["context"] if g else {},
                                        started_at=iso(start)))
            dec = ag.on_trigger(ev, start, wait, shed=shed)
            finish = start + timedelta(milliseconds=dec["decided_in_ms"] - wait * 1000)
            heapq.heappush(workers, max(finish, start))
            for aid in dec.get("action_ids", []):
                a = next((x for x in reversed(world.actions) if x["action_id"] == aid), None)
                if a and a["type"] in ("push", "whatsapp"):
                    heapq.heappush(pending_heap, (parse(a["send_at"]), ev.user))
            latency_rows.append(dict(decision_id=dec["decision_id"], viewer_id=ev.user, status=dec["status"],
                                     deliver_at=iso(deliver), decided_in_ms=dec["decided_in_ms"], queue_wait_s=round(wait, 3),
                                     action=dec["action"], fallback=dec["fallback_used"], model_calls=dec["model_calls"],
                                     tool_calls=dec["tool_calls"]))

    # ---- main loop ------------------------------------------------------------------------------------------------
    for t, _, kind, item in timeline:
        dispatch(t - timedelta(microseconds=1))
        release_until(t)
        if kind == "control":
            ops.append(dict(at=item["at"], type=item["type"], detail={k: v for k, v in item.items() if k not in ("at", "type")}))
            if item["type"] == "title_withdrawn":
                tt = pack.catalogue.withdraw(item["title_id"], t)
                if tt:
                    users = set(world.live_pages.get(tt.slug, ())) | set(world.pending_by_slug.get(tt.slug, ()))
                    n_rep = 0
                    for u in sorted(users):
                        rec = agent_for(u).on_withdrawal(tt.slug, t)
                        n_rep += bool(rec["replacement_page"] or rec["cancelled_actions"])
                    ops.append(dict(at=item["at"], type="withdrawal_response", title_id=item["title_id"], slug=tt.slug,
                                    viewers_checked=len(users), viewers_changed=n_rep,
                                    detail="live pages replaced and scheduled messages cancelled; later pages exclude the title by availability check"))
            elif item["type"] == "goal_rule_corrected":
                rule_fix.append(apply_goal_rule_correction(world, item, t))
                ops.append(dict(rule_fix[-1], type="rule_correction_applied"))
        else:
            if not pack.store.ingest(item):
                world.logs.append(f"duplicate delivery ignored: {item.id}")
                if kind == "trigger":   # still reaches the agent, which answers 'duplicate' via its idempotency ledger
                    heapq.heappush(queue, (item.deliver_at or item.rt, item, len(queue) + t.microsecond))
                continue
            if kind == "live":
                agent_for(item.user).observe(item, t) if item.user in world.agents or pack.arms.get(item.user) == "agent" else None
            else:
                heapq.heappush(queue, (item.deliver_at or item.rt, item, len(queue) + t.microsecond))
    dispatch(None)
    end = timeline[-1][0]
    release_until(end)
    for u, ag in world.agents.items():
        ag.release_due(end, final=True)
    world.states.save_all()

    # ---- outputs -------------------------------------------------------------------------------------------------------
    def wj(name, rows):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    wj("decisions.jsonl", world.decisions)
    live_actions = [a for a in world.actions if a["status"] != "cancelled"]
    live_actions.sort(key=lambda a: a["published_at"])
    wj("actions.jsonl", live_actions)
    wj("actions_cancelled.jsonl", [a for a in world.actions if a["status"] == "cancelled"])
    wj("replans.jsonl", world.replans)
    wj("assignments.jsonl", assignments)
    wj("ops_log.jsonl", ops)

    hold = [a for a in live_actions if pack.arms.get(a["user_id"]) != "agent"]
    ms = [r["decided_in_ms"] for r in latency_rows]
    acted = [r["decided_in_ms"] for r in latency_rows if r["status"] == "PUBLISHED"]
    per_min = Counter(r["deliver_at"][:16] for r in latency_rows)
    peak = per_min.most_common(1)[0]
    fb = Counter(x for r in latency_rows for x in r["fallback"])
    rep = dict(
        decisions=len(ms), published=len(acted), settings=settings.to_dict(),
        latency_ms_all=dict(p50=_pct(ms, 50), p95=_pct(ms, 95), p99=_pct(ms, 99), max=max(ms)),
        latency_ms_published=dict(p50=_pct(acted, 50), p95=_pct(acted, 95), p99=_pct(acted, 99), max=max(acted) if acted else None),
        over_60s=sum(1 for x in ms if x > 60000), over_300s=sum(1 for x in ms if x > 300000),
        queue_wait_s=dict(max=max(r["queue_wait_s"] for r in latency_rows), p95=_pct([r["queue_wait_s"] for r in latency_rows], 95)),
        peak_minute=dict(minute=peak[0], decisions=peak[1], median_minute=_pct(list(per_min.values()), 50)),
        slowest=sorted(latency_rows, key=lambda r: -r["decided_in_ms"])[:5],
        fallbacks=dict(fb), shed=sum(1 for r in latency_rows if r["status"] == "SHED_OVERLOAD"),
        status_counts=dict(Counter(r["status"] for r in latency_rows)),
        model_calls_max=max(r["model_calls"] for r in latency_rows), tool_calls_max=max(r["tool_calls"] for r in latency_rows),
        holdout_actions=len(hold),
        note="decided_in_ms = measured compute + simulated dependency latency + queue wait; replay itself runs faster than real time",
    )
    with open(os.path.join(out_dir, "latency_report.json"), "w") as f:
        json.dump(rep, f, indent=2)
    summary = dict(world=world, ops=ops, rule_fix=rule_fix, latency=rep, assignments=assignments)
    if verbose:
        print(f"replay: {len(ms)} decisions, {len(acted)} published, holdout actions={len(hold)}, "
              f"p95={rep['latency_ms_all']['p95']}ms max={rep['latency_ms_all']['max']}ms")
    return summary
