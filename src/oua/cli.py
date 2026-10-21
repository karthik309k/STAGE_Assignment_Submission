"""CLI.  One command runs everything:   python -m oua replay --pack <pack dir> --out sample_run --seed <seed>"""
import argparse
import json
import os
import subprocess
import sys
from collections import Counter

from .config import Settings
from .impact import pilot_reanalysis, replay_impact, write_impact_report, pct, pts
from .judge import judge_all, rulebook_with_corrections
from .loader import Pack
from .replay import run_replay


def run_simulator(pack_dir, actions, out_dir, seed):
    sim = os.path.join(pack_dir, "simulator", "simulate_outcomes.py")
    r = subprocess.run([sys.executable, sim, "--pack", pack_dir, "--actions", actions, "--out", out_dir, "--seed", str(seed)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("simulator failed:\n" + r.stderr)
    return json.load(open(os.path.join(out_dir, "simulator_report.json")))


def seed_robustness(pack, rb, assignments, actions, pack_dir, actions_path, seeds, out):
    """Re-run ONLY the simulator on the same actions with several seeds. The user sets of both arms are fixed, so this measures
    simulator randomness (not sampling uncertainty across users, which the confidence intervals above cover)."""
    import statistics as st
    import tempfile
    from .judge import full_stream
    rows = []
    for i in range(seeds):
        d = tempfile.mkdtemp()
        run_simulator(pack_dir, actions_path, d, f"robust-{i}")
        stream, obs = full_stream(pack, d)
        rep, _ = replay_impact(pack, rb, assignments, stream, obs, actions, d)
        rows.append((rep["primary"]["diff"], rep["secondary_any_play"]["diff"],
                     rep["secondary_plays_per_user"]["agent"] - rep["secondary_plays_per_user"]["holdout"],
                     rep["secondary_plays_per_user"]["holdout"]))
    m = lambda j: st.mean(r[j] for r in rows)
    sd = lambda j: st.stdev(r[j] for r in rows) if len(rows) > 1 else float("nan")
    base = m(3)
    return ["## 8. Robustness across simulator seeds", "",
            f"The same published actions were re-simulated with {seeds} different seeds (same viewers, same arms). This isolates simulator randomness; "
            "the confidence intervals in sections 1-3 are what cover sampling uncertainty across viewers.", "",
            "| Metric (agent minus holdout) | Mean over seeds | SD over seeds | Min | Max |", "|---|---|---|---|---|",
            f"| Goal success rate | {pts(m(0))} | {100 * sd(0):.1f} pts | {pts(min(r[0] for r in rows))} | {pts(max(r[0] for r in rows))} |",
            f"| Any play within 14 days | {pts(m(1))} | {100 * sd(1):.1f} pts | {pts(min(r[1] for r in rows))} | {pts(max(r[1] for r in rows))} |",
            f"| Plays per viewer | {m(2):+.2f} ({100 * m(2) / base:+.1f}% of the holdout's {base:.2f}) | {sd(2):.2f} | {min(r[2] for r in rows):+.2f} | {max(r[2] for r in rows):+.2f} |", "",
            "Reading: single-seed differences in the binary metrics swing by about their own size from seed to seed, so no single run (including the headline) should be quoted as the effect. "
            "Plays per viewer is a less noisy measure; where it is positive on every seed that is a small, consistent engagement signal, but it is not a goal outcome and it is simulator-based."]


def data_issue_rows(pack, decisions, ops, rule_fix):
    I, C = pack.issues, Counter()
    for d in decisions:
        for x in d["fallback_used"]:
            C[x] += 1
        C["status:" + d["status"]] += 1
    wd = next((o for o in ops if o["type"] == "withdrawal_response"), {})
    return [
        ("Duplicate events (same event_id)", f"{I['history_duplicate_event_lines']} history lines, {I['trigger_duplicate_lines']} trigger lines",
         "First delivery wins in the event store; a repeated trigger gets a DUPLICATE_IGNORED decision (idempotency key = event_id), never a second action"),
        ("Late events / wrong phone clocks", "clock skew in ~15% of events",
         "Ordering and freshness use received_at; occurrence time = received_at minus (sent_at - occurred_at), which is immune to a constant clock offset"),
        ("Offline-buffered app opens", f"{I['trigger_offline_buffered_over_600s']} triggers older than 10 min ({C['stale_trigger_skipped']} reached the agent arm)",
         "Not a live session: NO_ACTION with reason and age recorded; their facts still enter state at the estimated true time"),
        ("Stale / duplicated profile snapshot", f"{I['profile_users_with_two_rows']} viewers with two rows",
         "Latest updated_at wins, the other row is listed as a conflict; newer subscription events override the snapshot; consent takes the most restrictive value"),
        ("Brand-new viewers without a profile", f"{I['trigger_users_without_profile_row']} trigger viewers",
         "Facts built from signup_completed; consent unknown = no push/WhatsApp (in-app only); 'never paid' inferred only when whole history is visible"),
        ("Dialect conflicts (app / profile / deeplink)", "see dialect.conflicts in decisions",
         "App setting on the trigger decides; every disagreeing source is listed, never used"),
        ("Invalid dialect values (d7, NA, null)", f"{I['trigger_invalid_app_dialect']} triggers",
         "No valid dialect = no page and no content; recorded as NO_ACTION"),
        ("Language variants of one content_slug", f"{I['catalogue_slugs_with_language_variants']} slugs",
         "One title per slug per page; variant chosen by the viewer's app language, in the page dialect"),
        ("Availability windows / withdrawal at 07:00Z", f"{wd.get('viewers_checked', 0)} viewers held the withdrawn title",
         f"Availability checked at publish time by planner and verifier; on withdrawal live pages were replaced and scheduled messages cancelled ({wd.get('viewers_changed', 0)} viewers changed)"),
        ("Instructions inside catalogue descriptions", f"{I['catalogue_titles_with_injected_instructions']} titles",
         "Treated as data: flagged, logged, excluded from promotion; the verifier fails any action naming them. Goal selection never targets them"),
        ("Unmeasurable goals", "G10, G11",
         "G10 has success.event=null; G11's tv_app_open never occurs in any event file. Never selected; reported as unmeasurable"),
        ("Underspecified goal rules", "G06 'recently', G09 'rarely', G12 'stopped', 'never' claims",
         "Interpretations written down in goals.py and attached as assumptions; G06 requires a lapse event within 30 days (no guessing for lapsed profiles with no date)"),
        ("Goal success rule corrected mid-replay (G03)", f"{rule_fix[0]['affected_open_goals'] if rule_fix else 0} open goals affected",
         "Rule versioned (v1 -> v2); only open G03 goals (indexed by goal id) are switched; nothing else reprocessed"),
        ("Pilot report compares served vs everyone else", "see impact_report.md section 5", "Recomputed with randomised arms (ITT)"),
        ("Model faults (unsupported claim / malformed / timeout) and outages", f"fallbacks: " + ", ".join(f"{k}={v}" for k, v in sorted(C.items()) if k.startswith("model_") or k.startswith("data_store") or k.startswith("stale_cached")),
         "Model advisory only; verifier rejects unsupported claims; malformed JSON retried once; timeout/503 -> rule priority (circuit breaker); data-store failure -> cached facts, no messages, no new goals"),
    ]


def write_final_report(path, pack, decisions, actions, outcomes, rep, pilot, latency, ops, rule_fix, sim_report, seed):
    sc = Counter(d["status"] for d in decisions)
    arms = Counter(d["arm"] for d in decisions)
    st = Counter(o["status"] for o in outcomes)
    acts = Counter(a["type"] for a in actions)
    P = rep["primary"]
    import re
    norm = lambda r: re.sub(r"page already live.*", "page already live (same dialect, >=30 min of TTL left)", re.sub(r"stale trigger.*", "stale trigger (offline-buffered, not a live session)", re.sub(r"session already handled.*", "session already handled", r.split(": ")[0] if r.startswith(("no valid dialect", "holdout")) else r)))
    na = Counter(norm(d["no_action_reason"] or "")[:90] for d in decisions if d["action"] == "NO_ACTION" and d["status"] == "NO_ACTION")
    L = ["# Final report", "",
         f"Replay of {len(decisions)} trigger deliveries ({pack.issues['trigger_users']} viewers) against the supplied pack; simulator seed `{seed}`.", "",
         "## Results at a glance", "",
         f"- Decisions: {dict(sc)}. Agent arm {arms['agent']}, holdout {arms['holdout']}.",
         f"- Actions published: {dict(acts)}. **Actions to holdout viewers: {latency['holdout_actions']}.** Simulator policy problems: {sim_report['policy_problem_count']}.",
         f"- Latency (decided_in_ms, includes simulated dependency time and queue wait): p50 {latency['latency_ms_all']['p50']}, p95 {latency['latency_ms_all']['p95']}, max {latency['latency_ms_all']['max']}; "
         f"decisions over 60 s: {latency['over_60s']}, over 5 min: {latency['over_300s']}; shed: {latency['shed']}.",
         f"- Goal outcomes judged: {dict(st)}.",
         f"- **Impact (ITT, agent vs holdout): {pct(P['p_agent'])} vs {pct(P['p_control'])}, {pts(P['diff'])}, 95% CI {pts(P['ci_lo'])} to {pts(P['ci_hi'])}.** "
         + ("The interval excludes zero." if (P['ci_lo'] > 0 or P['ci_hi'] < 0) else "The interval includes zero: we do not claim an uplift."),
         "- Seed robustness (impact_report.md section 8): re-simulating the same actions with other seeds moves the binary-metric differences by about their own size; "
         "plays per viewer is higher for the agent arm on most seeds (small, simulator-based). Do not quote any single seed as the effect.",
         f"- Pilot: the report's +22.4 pts is a selection artefact; the randomised pilot difference is {pts(pilot['treatment']['diff'])} (95% CI {pts(pilot['treatment']['ci_lo'])} to {pts(pilot['treatment']['ci_hi'])}).", "",
         "## Why the agent did nothing (NO_ACTION reasons, count)", ""]
    L += [f"- {k}: {v}" for k, v in na.most_common()]
    L += ["", "## Data issues found and how each was handled", "", "| Issue | Size | Handling |", "|---|---|---|"]
    for a, b, c in data_issue_rows(pack, decisions, ops, rule_fix):
        L.append(f"| {a} | {b} | {c} |")
    L += ["", "## Surprise events: what the system noticed and did", ""]
    for o in ops:
        if o["type"] in ("dependency_unavailable", "dependency_degraded", "title_withdrawn", "goal_rule_corrected", "withdrawal_response", "rule_correction_applied"):
            L.append(f"- `{o['at']}` {o['type']}: {json.dumps({k: v for k, v in o.items() if k not in ('at', 'type', 'affected_viewers')})[:300]}")
    rp = [json.loads(l) for l in open(os.path.join(os.path.dirname(path), "replans.jsonl"))]
    kinds = Counter(("purchase observed" if r["reason"].startswith("purchase") else "goal achieved" if r["reason"].startswith("goal achieved")
                     else "title withdrawn" if r["reason"].startswith("title withdrawn") else "rule corrected") for r in rp)
    canc = sum(1 for l in open(os.path.join(os.path.dirname(path), "actions_cancelled.jsonl")))
    L += ["", "## Re-planning during the replay (replans.jsonl)", "",
          f"{len(rp)} re-plans: {dict(kinds)}. Scheduled messages cancelled before sending: {canc} (see actions_cancelled.jsonl for each reason). "
          "Re-plans are triggered by events for the affected viewer only; no other viewer was reprocessed."]
    L += ["", "## Honest caveats", "",
          "- The mock model never adds information beyond rule priority; the architecture limits its blast radius rather than relying on it. See KNOWN_LIMITATIONS.md.",
          "- Several injected faults landed on triggers handled earlier in the pipeline (stale or holdout viewers); the timeout path is exercised by tests rather than by this replay.",
          "- Per-goal impact cells are small. One replay day cannot distinguish small effects from noise."]
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")


def cmd_replay(a):
    pack = Pack(a.pack)
    settings = Settings.load(a.config)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    summary = run_replay(pack, settings, out, resume=a.resume)
    sim_dir = os.path.join(out, "sim")
    sim_report = run_simulator(os.path.abspath(a.pack), os.path.join(out, "actions.jsonl"), sim_dir, a.seed)
    rb = rulebook_with_corrections(pack)
    outcomes, stream, observed_until = judge_all(pack, rb, out, sim_dir)
    actions = [json.loads(l) for l in open(os.path.join(out, "actions.jsonl"))]
    decisions = [json.loads(l) for l in open(os.path.join(out, "decisions.jsonl"))]
    rep, recs = replay_impact(pack, rb, summary["assignments"], stream, observed_until, actions, out)
    pilot = pilot_reanalysis(pack)
    extra = seed_robustness(pack, rb, summary["assignments"], actions, os.path.abspath(a.pack),
                            os.path.join(out, "actions.jsonl"), a.robustness_seeds, out) if a.robustness_seeds else None
    write_impact_report(os.path.join(out, "impact_report.md"), pilot, rep, sim_report, extra)
    write_final_report(os.path.join(out, "final_report.md"), pack, decisions, actions, outcomes, rep, pilot,
                       summary["latency"], summary["ops"], summary["rule_fix"], sim_report, a.seed)
    json.dump(dict(replay_impact=rep, pilot=pilot), open(os.path.join(out, "impact_numbers.json"), "w"), indent=1, default=str)
    if not a.keep_sim_events:
        import gzip
        import shutil
        src = os.path.join(sim_dir, "outcome_events.jsonl")      # portable: no external gzip command (Windows has none)
        with open(src, "rb") as fi, gzip.open(src + ".gz", "wb") as fo:
            shutil.copyfileobj(fi, fo)
        os.remove(src)
    P = rep["primary"]
    print(f"\nITT goal success: agent {pct(P['p_agent'])} vs holdout {pct(P['p_control'])} ({pts(P['diff'])}, "
          f"95% CI {pts(P['ci_lo'])}..{pts(P['ci_hi'])}); simulator policy problems: {sim_report['policy_problem_count']}")
    print(f"outputs in {out}")


def cmd_audit(a):
    path = os.path.join(a.run, "viewer_state", f"{a.user}.json")
    st = json.load(open(path))
    decs = [json.loads(l) for l in open(os.path.join(a.run, "decisions.jsonl"))]
    mine = [d for d in decs if d["viewer_id"] == a.user]
    print(f"viewer {a.user} arm={st['arm']}")
    for d in mine:
        print(f"- {d['decision_id']} {d['trigger']['received_at']} {d['status']}/{d['action']} goal={(d.get('goal') or {}).get('id')} "
              f"conf={d.get('confidence')} in {d['decided_in_ms']}ms\n    reason: {d.get('no_action_reason')}\n    checks: {d['checks']}\n    conflicts: {d['conflicts']}")
    print("goal history:", json.dumps(st["goal_history"] + ([st["active_goal"]] if st["active_goal"] else []), indent=1)[:1500])
    print("outcomes:", json.dumps(st["outcomes"], indent=1)[:1500])
    print("learnings:", st["learnings"])


def main():
    ap = argparse.ArgumentParser(prog="oua")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="replay the trigger script, run the simulator, judge outcomes, write all reports")
    r.add_argument("--pack", default="one_user_one_agent_pack")
    r.add_argument("--out", default="sample_run")
    r.add_argument("--seed", default="oua-practice")
    r.add_argument("--config", default=None)
    r.add_argument("--resume", action="store_true", help="keep existing viewer_state (idempotent re-run)")
    r.add_argument("--keep-sim-events", action="store_true")
    r.add_argument("--robustness-seeds", type=int, default=6, help="extra simulator seeds for the robustness table (0 = skip)")
    r.set_defaults(fn=cmd_replay)
    u = sub.add_parser("audit", help="show what the agent knew, decided and observed for one viewer")
    u.add_argument("user")
    u.add_argument("--run", default="sample_run")
    u.set_defaults(fn=cmd_audit)
    a = ap.parse_args()
    a.fn(a)
