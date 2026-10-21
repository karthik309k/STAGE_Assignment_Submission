"""Impact reporter: agent arm vs randomised holdout (intention-to-treat), pilot re-analysis, and the biased comparison
the pilot report used (shown only to explain why it misleads)."""
import csv
import json
import math
import os
import random
from collections import Counter, defaultdict
from datetime import timedelta

from .goals import evaluate_success
from .timeutil import parse, iso


def two_prop(s1, n1, s0, n0):
    p1, p0 = (s1 / n1 if n1 else 0.0), (s0 / n0 if n0 else 0.0)
    se = math.sqrt((p1 * (1 - p1)) / n1 + (p0 * (1 - p0)) / n0) if n1 and n0 else float("nan")
    d = p1 - p0
    z = d / se if se and se > 0 else 0.0
    p = math.erfc(abs(z) / math.sqrt(2))
    return dict(p_agent=p1, p_control=p0, diff=d, ci_lo=d - 1.96 * se, ci_hi=d + 1.96 * se, p_value=p, se=se)


def bootstrap_ci(a, b, reps=2000, seed=7):
    rnd = random.Random(seed)
    if not a or not b:
        return (float("nan"), float("nan"))
    diffs = []
    for _ in range(reps):
        ma = sum(rnd.choice(a) for _ in range(len(a))) / len(a)
        mb = sum(rnd.choice(b) for _ in range(len(b))) / len(b)
        diffs.append(ma - mb)
    diffs.sort()
    return diffs[int(0.025 * reps)], diffs[int(0.975 * reps)]


def pct(x):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


def pts(x):
    return f"{100 * x:+.1f} pts"


# ---------------------------------------------------------------------------------------------------- pilot
def pilot_reanalysis(pack):
    rows = list(csv.DictReader(open(os.path.join(pack.root, "pilot", "pilot_outcomes.csv"), encoding="utf-8")))
    T = [r for r in rows if r["arm"] == "treatment"]
    H = [r for r in rows if r["arm"] == "holdout"]
    ok = lambda r: r["succeeded"] == "true"
    served = [r for r in rows if r["served"] == "true"]
    others = [r for r in rows if r["served"] != "true"]
    unserved_T = [r for r in T if r["served"] != "true"]
    out = dict(
        n=len(rows), treatment=two_prop(sum(map(ok, T)), len(T), sum(map(ok, H)), len(H)),
        n_treatment=len(T), n_holdout=len(H), served_n=len(served),
        served_rate=sum(map(ok, served)) / len(served), others_rate=sum(map(ok, others)) / len(others),
        biased_diff=sum(map(ok, served)) / len(served) - sum(map(ok, others)) / len(others),
        unserved_treatment_rate=sum(map(ok, unserved_T)) / len(unserved_T), holdout_rate=sum(map(ok, H)) / len(H),
        served_holdout_in_rows=sum(1 for r in H if r["served"] == "true"))
    bg = {}
    for g in sorted({r["goal_id"] for r in rows}):
        t = [r for r in T if r["goal_id"] == g]
        h = [r for r in H if r["goal_id"] == g]
        bg[g] = dict(n_t=len(t), n_h=len(h), **two_prop(sum(map(ok, t)), len(t), sum(map(ok, h)), len(h)))
    out["by_goal"] = bg
    return out


# ------------------------------------------------------------------------------------------------ replay impact
def replay_impact(pack, rb, assignments, stream, observed_until, actions, out_dir):
    served = {a["user_id"] for a in actions}
    recs = []
    for a in assignments:
        if not a["goal_id"]:
            recs.append(dict(a, status=None))
            continue
        start = parse(a["started_at"])
        gid = a["goal_id"]
        goal = dict(goal_id=gid, started_at=a["started_at"], deadline=iso(start + timedelta(hours=rb.window_hours(gid))),
                    rule_version=rb.current_version(gid), context=a.get("context", {}))
        status, refs, detail = evaluate_success(pack, rb, goal, stream.get(a["viewer_id"], []), observed_until)
        recs.append(dict(a, status=status, served=a["viewer_id"] in served))
    A = [r for r in recs if r["arm"] == "agent"]
    H = [r for r in recs if r["arm"] == "holdout"]
    conc = lambda rs: [r for r in rs if r.get("status") in ("succeeded", "failed")]
    ok = lambda r: r["status"] == "succeeded"
    Ac, Hc = conc(A), conc(H)
    res = dict(n_agent_users=len(A), n_holdout_users=len(H),
               with_goal=dict(agent=sum(1 for r in A if r["goal_id"]), holdout=sum(1 for r in H if r["goal_id"])),
               inconclusive=dict(agent=sum(1 for r in A if r.get("status") == "inconclusive"),
                                 holdout=sum(1 for r in H if r.get("status") == "inconclusive")),
               primary=two_prop(sum(map(ok, Ac)), len(Ac), sum(map(ok, Hc)), len(Hc)),
               n_conclusive=dict(agent=len(Ac), holdout=len(Hc)))
    res["primary"]["boot_ci"] = bootstrap_ci([1 if ok(r) else 0 for r in Ac], [1 if ok(r) else 0 for r in Hc])
    # per goal
    bg = {}
    for g in sorted({r["goal_id"] for r in recs if r["goal_id"]}):
        a = [r for r in Ac if r["goal_id"] == g]
        h = [r for r in Hc if r["goal_id"] == g]
        bg[g] = dict(n_a=len(a), n_h=len(h), s_a=sum(map(ok, a)), s_h=sum(map(ok, h)), **two_prop(sum(map(ok, a)), len(a), sum(map(ok, h)), len(h)))
    res["by_goal"] = bg
    # secondary: any play in the 14 days after first trigger, ALL triggered users (including those with no goal)
    first = {}
    for t in sorted(pack.triggers, key=lambda e: e.rt):
        first.setdefault(t.user, t.rt)
    sec = defaultdict(list)
    for u, t0 in first.items():
        arm = pack.arms.get(u)
        if arm not in ("agent", "holdout"):
            continue
        plays = [e for e in stream.get(u, []) if e.type == "play_started" and t0 < e.t_est <= t0 + timedelta(days=14)]
        sec[arm].append((1 if plays else 0, len(plays)))
    sa, sh = sec["agent"], sec["holdout"]
    res["secondary_any_play"] = dict(n_agent=len(sa), n_holdout=len(sh), **two_prop(sum(x for x, _ in sa), len(sa), sum(x for x, _ in sh), len(sh)))
    res["secondary_plays_per_user"] = dict(agent=sum(y for _, y in sa) / len(sa), holdout=sum(y for _, y in sh) / len(sh))
    # biased comparison (the pilot report's method) on ALL triggered viewers, goal-free metric (any play in 14 days)
    rows = []
    for u, t0 in first.items():
        arm = pack.arms.get(u)
        if arm not in ("agent", "holdout"):
            continue
        any_play = any(e.type == "play_started" and t0 < e.t_est <= t0 + timedelta(days=14) for e in stream.get(u, []))
        rows.append((arm, u in served, any_play))
    sv = [r for r in rows if r[0] == "agent" and r[1]]
    ot = [r for r in rows if not (r[0] == "agent" and r[1])]
    ua = [r for r in rows if r[0] == "agent" and not r[1]]
    ho = [r for r in rows if r[0] == "holdout"]
    rate = lambda x: (sum(1 for r in x if r[2]) / len(x)) if x else None
    res["biased"] = dict(served_n=len(sv), others_n=len(ot), served_rate=rate(sv), others_rate=rate(ot),
                         unserved_agent_n=len(ua), unserved_agent_rate=rate(ua), holdout_rate=rate(ho),
                         served_share_of_agent=len(sv) / (len(sv) + len(ua)))
    # why: what distinguishes served users? (pre-treatment: they had a fresh, valid-dialect trigger and an eligible goal)
    res["mde_80pct_power"] = 2.8 * res["primary"]["se"] if res["primary"]["se"] == res["primary"]["se"] else None
    return res, recs


def write_impact_report(path, pilot, rep, sim_report, extra=None):
    P = rep["primary"]
    L = []
    L += ["# Impact report: agent vs randomised holdout", "",
          "All numbers are **intention-to-treat (ITT)**: every viewer is analysed in the arm they were randomised to "
          "(`replay/arms.csv`), whether or not the agent acted on them. Holdout viewers have a *shadow* goal assigned by the same planner on the same "
          "pre-treatment facts at their first live visit, so both arms are judged on identical goal definitions "
          "(final, corrected rules). Shadow goals are measurement only: no holdout viewer received any action.", "",
          "## 1. Headline (replay day)", "",
          f"| | Agent arm | Holdout arm |", "|---|---|---|",
          f"| Triggered viewers | {rep['n_agent_users']} | {rep['n_holdout_users']} |",
          f"| With an assigned goal | {rep['with_goal']['agent']} | {rep['with_goal']['holdout']} |",
          f"| Conclusive goal outcomes | {rep['n_conclusive']['agent']} | {rep['n_conclusive']['holdout']} |",
          f"| Goal success rate | **{pct(P['p_agent'])}** | **{pct(P['p_control'])}** |", "",
          f"**Difference: {pts(P['diff'])}** (95% CI {pts(P['ci_lo'])} to {pts(P['ci_hi'])}; bootstrap {pts(P['boot_ci'][0])} to {pts(P['boot_ci'][1])}; p = {P['p_value']:.3f}).", ""]
    sig = P["ci_lo"] > 0 or P["ci_hi"] < 0
    L += [("The interval excludes zero: there is evidence of an effect." if sig else
           "**The interval includes zero: this replay does not show a statistically detectable effect.** "
           "We report the point estimate for completeness but do not claim an uplift."),
          f"With these sample sizes the smallest effect detectable at 80% power is roughly ±{100 * rep['mde_80pct_power']:.1f} pts; smaller true effects cannot be distinguished from noise in one replay day.", ""]
    L += ["## 2. By goal (ITT, same definitions in both arms)", "", "| Goal | Agent n | Agent success | Holdout n | Holdout success | Diff | 95% CI |", "|---|---|---|---|---|---|---|"]
    for g, r in rep["by_goal"].items():
        ci = "n/a" if (r["ci_lo"] != r["ci_lo"] or 0 in (r["n_a"], r["n_h"])) else f"{pts(r['ci_lo'])} to {pts(r['ci_hi'])}"
        na = lambda n, p: "n/a" if n == 0 else p
        L.append(f"| {g} | {r['n_a']} | {na(r['n_a'], pct(r['p_agent']))} ({r['s_a']}) | {r['n_h']} | {na(r['n_h'], pct(r['p_control']))} ({r['s_h']}) | {'n/a' if 0 in (r['n_a'], r['n_h']) else pts(r['diff'])} | {ci} |")
    L += ["", "Small cells (a handful of viewers) are noise; treat per-goal rows as descriptive, not as findings.", ""]
    S = rep["secondary_any_play"]
    L += ["## 3. Secondary metric: any play within 14 days, all triggered viewers", "",
          f"Agent {pct(S['p_agent'])} vs holdout {pct(S['p_control'])}: {pts(S['diff'])} (95% CI {pts(S['ci_lo'])} to {pts(S['ci_hi'])}). "
          f"Plays per viewer: agent {rep['secondary_plays_per_user']['agent']:.2f}, holdout {rep['secondary_plays_per_user']['holdout']:.2f}. "
          "This includes viewers for whom no goal could be assigned, so it does not depend on the goal catalogue.", ""]
    B = rep["biased"]
    L += ["## 4. Why \"served vs everyone else\" misleads", "",
          "Computed on ALL triggered viewers with the goal-free metric (any play in 14 days), because on the goal metric above nearly every agent-arm viewer with a goal is served "
          "(the planner only assigns a goal to viewers it can act on), which would hide the problem.", "",
          f"- Pilot-report method: served {pct(B['served_rate'])} (n={B['served_n']}) vs everyone else {pct(B['others_rate'])} (n={B['others_n']}) = **{pts(B['served_rate'] - B['others_rate'])}**.",
          f"- Randomised ITT on the same metric: agent {pct(S['p_agent'])} vs holdout {pct(S['p_control'])} = **{pts(S['diff'])}**.",
          f"- Selection check: agent-arm viewers who were NOT served (n={B['unserved_agent_n']}) play {pct(B['unserved_agent_rate'])} of the time, the holdout plays {pct(B['holdout_rate'])}. "
          "Randomisation makes agent arm and holdout exchangeable, so any gap between the unserved part of the agent arm and the holdout is not an effect of serving: it is who *ends up* unserved.", "",
          "Why: the agent only acts on viewers who have a live session, a valid dialect and an eligible goal; viewers on stale offline triggers, with an invalid app setting or no goal are not served, "
          "and they behave differently. Splitting by 'served' therefore sorts viewers by how engaged they already were. ITT keeps the groups exchangeable because assignment was random.", ""]
    L += ["## 5. Pilot re-analysis (`pilot/`)", "",
          f"The internal report claims 40.1% vs 17.7% (+22.4 pts). Recomputed from `pilot_outcomes.csv`:", "",
          "| Comparison | Group A | Group B | Difference |", "|---|---|---|---|",
          f"| **Randomised (ITT)**: all treatment (n={pilot['n_treatment']}) vs all holdout (n={pilot['n_holdout']}) | {pct(pilot['treatment']['p_agent'])} | {pct(pilot['treatment']['p_control'])} | **{pts(pilot['treatment']['diff'])}** (95% CI {pts(pilot['treatment']['ci_lo'])} to {pts(pilot['treatment']['ci_hi'])}, p={pilot['treatment']['p_value']:.3f}) |",
          f"| Report's method: served (n={pilot['served_n']}) vs everyone else | {pct(pilot['served_rate'])} | {pct(pilot['others_rate'])} | {pts(pilot['biased_diff'])} |",
          f"| Selection check: unserved *treatment* viewers vs holdout | {pct(pilot['unserved_treatment_rate'])} | {pct(pilot['holdout_rate'])} | {pts(pilot['unserved_treatment_rate'] - pilot['holdout_rate'])} |", "",
          "Per goal (ITT):", "", "| Goal | Treatment n | Treatment | Holdout n | Holdout | Diff | 95% CI |", "|---|---|---|---|---|---|---|"]
    for g, r in pilot["by_goal"].items():
        ci = "n/a" if r["ci_lo"] != r["ci_lo"] else f"{pts(r['ci_lo'])} to {pts(r['ci_hi'])}"
        L.append(f"| {g} | {r['n_t']} | {pct(r['p_agent'])} | {r['n_h']} | {pct(r['p_control'])} | {pts(r['diff'])} | {ci} |")
    L += ["", "## 6. Simulator facts", "",
          f"`simulator_report.json` counts: {json.dumps(sim_report.get('counts', {}))}; policy problems detected by the simulator: **{sim_report.get('policy_problem_count')}**.", "",
          "## 7. Limits of this evidence", "",
          "- One replay day, ~870 viewers per arm: wide intervals. The simulator is a model of behaviour, not reality; an effect measured against it shows the agent's pages do what the simulator rewards, not that real viewers behave this way.",
          "- Goal success counts organic events as well as agent-caused ones; only the *difference between arms* is attributable to the agent.",
          "- Evaluators will rerun with another seed; expect the point estimate to move within the interval above."]
    if extra:
        L += [""] + extra
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
