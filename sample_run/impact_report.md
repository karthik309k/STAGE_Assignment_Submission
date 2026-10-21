# Impact report: agent vs randomised holdout

All numbers are **intention-to-treat (ITT)**: every viewer is analysed in the arm they were randomised to (`replay/arms.csv`), whether or not the agent acted on them. Holdout viewers have a *shadow* goal assigned by the same planner on the same pre-treatment facts at their first live visit, so both arms are judged on identical goal definitions (final, corrected rules). Shadow goals are measurement only: no holdout viewer received any action.

## 1. Headline (replay day)

| | Agent arm | Holdout arm |
|---|---|---|
| Triggered viewers | 758 | 746 |
| With an assigned goal | 684 | 686 |
| Conclusive goal outcomes | 684 | 686 |
| Goal success rate | **11.7%** | **10.5%** |

**Difference: +1.2 pts** (95% CI -2.1 pts to +4.5 pts; bootstrap -2.0 pts to +4.4 pts; p = 0.479).

**The interval includes zero: this replay does not show a statistically detectable effect.** We report the point estimate for completeness but do not claim an uplift.
With these sample sizes the smallest effect detectable at 80% power is roughly ±4.8 pts; smaller true effects cannot be distinguished from noise in one replay day.

## 2. By goal (ITT, same definitions in both arms)

| Goal | Agent n | Agent success | Holdout n | Holdout success | Diff | 95% CI |
|---|---|---|---|---|---|---|
| G01 | 51 | 39.2% (20) | 43 | 32.6% (14) | +6.7 pts | -12.7 pts to +26.0 pts |
| G02 | 34 | 20.6% (7) | 28 | 10.7% (3) | +9.9 pts | -7.9 pts to +27.6 pts |
| G03 | 1 | 0.0% (0) | 0 | n/a (0) | n/a | n/a |
| G04 | 0 | n/a (0) | 1 | 0.0% (0) | n/a | n/a |
| G05 | 129 | 4.7% (6) | 120 | 5.0% (6) | -0.3 pts | -5.7 pts to +5.0 pts |
| G06 | 43 | 11.6% (5) | 60 | 15.0% (9) | -3.4 pts | -16.5 pts to +9.8 pts |
| G07 | 339 | 3.5% (12) | 335 | 3.9% (13) | -0.3 pts | -3.2 pts to +2.5 pts |
| G08 | 40 | 30.0% (12) | 52 | 21.2% (11) | +8.8 pts | -9.2 pts to +26.9 pts |
| G09 | 18 | 16.7% (3) | 13 | 15.4% (2) | +1.3 pts | -24.8 pts to +27.4 pts |
| G12 | 29 | 51.7% (15) | 34 | 41.2% (14) | +10.5 pts | -14.0 pts to +35.1 pts |

Small cells (a handful of viewers) are noise; treat per-goal rows as descriptive, not as findings.

## 3. Secondary metric: any play within 14 days, all triggered viewers

Agent 75.8% vs holdout 74.3%: +1.5 pts (95% CI -2.6 pts to +5.5 pts). Plays per viewer: agent 2.90, holdout 2.78. This includes viewers for whom no goal could be assigned, so it does not depend on the goal catalogue.

## 4. Why "served vs everyone else" misleads

Computed on ALL triggered viewers with the goal-free metric (any play in 14 days), because on the goal metric above nearly every agent-arm viewer with a goal is served (the planner only assigns a goal to viewers it can act on), which would hide the problem.

- Pilot-report method: served 84.4% (n=688) vs everyone else 68.9% (n=1058) = **+15.5 pts**.
- Randomised ITT on the same metric: agent 75.8% vs holdout 74.3% = **+1.5 pts**.
- Selection check: agent-arm viewers who were NOT served (n=175) play 41.7% of the time, the holdout plays 74.3%. Randomisation makes agent arm and holdout exchangeable, so any gap between the unserved part of the agent arm and the holdout is not an effect of serving: it is who *ends up* unserved.

Why: the agent only acts on viewers who have a live session, a valid dialect and an eligible goal; viewers on stale offline triggers, with an invalid app setting or no goal are not served, and they behave differently. Splitting by 'served' therefore sorts viewers by how engaged they already were. ITT keeps the groups exchangeable because assignment was random.

## 5. Pilot re-analysis (`pilot/`)

The internal report claims 40.1% vs 17.7% (+22.4 pts). Recomputed from `pilot_outcomes.csv`:

| Comparison | Group A | Group B | Difference |
|---|---|---|---|
| **Randomised (ITT)**: all treatment (n=1912) vs all holdout (n=1979) | 18.3% | 19.6% | **-1.3 pts** (95% CI -3.8 pts to +1.2 pts, p=0.299) |
| Report's method: served (n=217) vs everyone else | 40.1% | 17.7% | +22.4 pts |
| Selection check: unserved *treatment* viewers vs holdout | 15.5% | 19.6% | -4.1 pts |

Per goal (ITT):

| Goal | Treatment n | Treatment | Holdout n | Holdout | Diff | 95% CI |
|---|---|---|---|---|---|---|
| G02 | 177 | 19.2% | 183 | 19.7% | -0.5 pts | -8.6 pts to +7.7 pts |
| G04 | 63 | 23.8% | 66 | 22.7% | +1.1 pts | -13.5 pts to +15.7 pts |
| G05 | 1045 | 1.2% | 1073 | 0.6% | +0.7 pts | -0.1 pts to +1.5 pts |
| G06 | 157 | 0.6% | 174 | 2.9% | -2.2 pts | -5.0 pts to +0.5 pts |
| G09 | 470 | 60.9% | 483 | 67.3% | -6.4 pts | -12.5 pts to -0.4 pts |

## 6. Simulator facts

`simulator_report.json` counts: {"actions_total": 949, "home_page_accepted": 907, "home_page_published_but_not_shown": 2, "home_page_shown_personalised": 824, "in_app_message_queued": 5, "in_app_message_requested": 5, "organic_purchases_scripted": 36, "push_delivered": 30, "push_requested": 35, "push_undeliverable_token": 5, "whatsapp_delivered": 2, "whatsapp_requested": 2}; policy problems detected by the simulator: **0**.

## 7. Limits of this evidence

- One replay day, ~870 viewers per arm: wide intervals. The simulator is a model of behaviour, not reality; an effect measured against it shows the agent's pages do what the simulator rewards, not that real viewers behave this way.
- Goal success counts organic events as well as agent-caused ones; only the *difference between arms* is attributable to the agent.
- Evaluators will rerun with another seed; expect the point estimate to move within the interval above.

## 8. Robustness across simulator seeds

The same published actions were re-simulated with 6 different seeds (same viewers, same arms). This isolates simulator randomness; the confidence intervals in sections 1-3 are what cover sampling uncertainty across viewers.

| Metric (agent minus holdout) | Mean over seeds | SD over seeds | Min | Max |
|---|---|---|---|---|
| Goal success rate | +1.4 pts | 0.8 pts | +0.6 pts | +2.9 pts |
| Any play within 14 days | -0.6 pts | 2.2 pts | -2.5 pts | +2.5 pts |
| Plays per viewer | +0.12 (+4.3% of the holdout's 2.75) | 0.06 | +0.03 | +0.20 |

Reading: single-seed differences in the binary metrics swing by about their own size from seed to seed, so no single run (including the headline) should be quoted as the effect. Plays per viewer is a less noisy measure; where it is positive on every seed that is a small, consistent engagement signal, but it is not a goal outcome and it is simulator-based.
