# One User, One Agent

A per-viewer agent that chooses **one measurable goal**, decides what to do (home page, one message, or **nothing**) from
evidence it can cite, acts inside a strict time budget, checks itself with an independent verifier, judges the outcome from
attributable signals, and proves impact against the randomised holdout.

* Pure Python standard library, **no API key needed** (the model is a deterministic mock by default).
* Python 3.10+. `pytest` only for the tests.

## Setup and the one command

```bash
# 1. put the data pack next to this README (or pass its path)
#      submission/one_user_one_agent_pack/{config,data,replay,pilot,simulator}
pip install -r requirements.txt        # only needed for the tests

# 2. replay the trigger script, run the simulator, judge outcomes, write every report
make replay                            # = PYTHONPATH=src python3 -m oua replay --pack one_user_one_agent_pack --out sample_run
# other pack location / simulator seed:
make replay PACK=/path/to/pack SEED=any-seed
```

~30 seconds (add `--robustness-seeds 0` to skip the extra simulator seeds). It (1) replays `replay/triggers.jsonl`, `live_events.jsonl`, `control_events.jsonl`, `model_faults.jsonl` in delivery
order, (2) runs `simulator/simulate_outcomes.py` on the published actions, (3) judges every goal, (4) writes the impact,
latency and final reports into `sample_run/`. Nothing is hard-coded to the practice replay day: the replay day is inferred from
the triggers and the simulator is re-run with whatever seed you pass.

```bash
make test                              # 44 tests, ~5 s, self-contained (synthetic mini-pack; no data pack needed)
PYTHONPATH=src python3 -m oua audit U-04734 --run sample_run     # what the agent knew, decided and saw for one viewer
```

Independent audit of a finished run (does not import the agent or verifier; re-checks actions against the pack):
```bash
python tools/check_run.py --pack one_user_one_agent_pack --run sample_run     # 18 PASS/FAIL checks
```

## What you get in `sample_run/`

| File | Content |
|---|---|
| `decisions.jsonl` | One record per trigger delivery, including every "do nothing" with its reason (goal, action, dialect + conflicts, evidence with freshness, confidence + reason, assumptions, checks, model/tool calls, latency, status) |
| `actions.jsonl` | The actions fed to the simulator (home pages, pushes, WhatsApp, in-app). Cancelled ones are in `actions_cancelled.jsonl` with the reason |
| `viewer_state/` | One durable JSON per viewer: goal + history, decisions, actions, live page, scheduled messages, idempotency ledgers, outcomes, learnings |
| `outcomes.jsonl` | Each goal judged succeeded / failed / inconclusive with evidence |
| `impact_report.md` | Agent vs holdout (intention-to-treat), per goal, pilot re-analysis, why "served vs everyone else" misleads |
| `latency_report.json` | p50/p95/p99, queue wait, peak minute, fallbacks, shed count, budget maxima |
| `final_report.md` | Summary, NO_ACTION reasons, **data-issue table (issue -> handling)**, surprise events, replans |
| `replans.jsonl`, `assignments.jsonl`, `ops_log.jsonl`, `sim/` | Re-plans, ITT goal assignment for both arms, operational log, simulator output |

## Layout

```
src/oua/  loader.py facts.py goals.py interventions.py verifier.py model.py agent.py runtime.py state.py
          replay.py judge.py impact.py cli.py config.py timeutil.py
tools/    check_run.py (independent audit of a run)
tests/    test_required.py (the 4 required scenarios + more)  test_safety.py  test_burst.py   fixtures/config/
```

Settings (budgets 8 model / 20 tool calls, timeouts, thresholds, worker count) are in `src/oua/config.py`; override with `--config file.json`.
Optional real model: `model_mode: "anthropic"` + `ANTHROPIC_API_KEY` (off by default; sends only goal ids and rule reasons; see KNOWN_LIMITATIONS).

See `ARCHITECTURE.md`, `AI_COLLABORATION.md`, `KNOWN_LIMITATIONS.md`, and `DISCUSSION_PREP.md`.
