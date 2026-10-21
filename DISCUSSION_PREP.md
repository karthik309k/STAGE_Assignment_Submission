# Preparing for the 60-minute discussion (not part of the required submission)

## Walk one viewer: U-04734 (`python -m oua audit U-04734 --run sample_run`)
1. **Trigger** 2026-10-01T00:59:10Z, app open, age 0 s, d4 on the app, profile says d3 -> dialect used d4 (app setting), conflict `profile:d3` recorded.
2. **Facts**: active subscriber (profile), series `bagi-baya-d4` started, episode 1 completed 3+ days ago, next episode 2.
3. **Goal**: G07 resume_series_3d, deadline +72 h, success = `play_started` of the same content_slug. Eligible rules: G07 (strong). Model advised the same.
4. **Action**: home page, hero = that series' title, rows because_you_watched / new_releases / popular / genre (next_episode dropped: only 2 valid titles). No message: G07 is not a message goal (the viewer is in the app and the hero already shows it).
5. **Checks**: 11 passed, none failed; published about 0.4 s after the trigger.
6. **Outcome**: simulator produced a `play_started` of that slug inside the window -> `succeeded`, recorded in state; learnings: `G07: 1 success`.
Be ready to say: this one viewer succeeded, but only the arm comparison says whether the agent caused it (it did not, detectably, in this replay; see the seed-robustness table).

## Answers to the brief's questions (short)
* **Current enough to act on?** Per-fact freshness: events seconds old; profile up to a day stale, so any subscription-dependent goal is checked against newer events first and the confidence drops 0.05 when it rests on the snapshot alone; stale cache -> no messages, no new goals.
* **When do nothing?** Holdout, stale/duplicate, no valid dialect, no eligible measurable goal, a live page already covers it. "Right" is judged by the ITT comparison and by NO_ACTION viewers' outcomes; not claimed beyond that.
* **Cause vs selection?** Randomised arms, ITT, shadow goals for the holdout; and the demonstration that served-vs-rest differs by ~14 pts from the randomised answer.
* **1,000 opens in a minute?** `test_burst.py`: all decided in budget; model calls shed first, fresh before stale, shed+record only in extreme overload. At scale: shard by viewer id, stateless workers, per-viewer queue.
* **Never automate?** Spend/price/offer decisions, anything touching minors' data or consent changes, rule corrections themselves, legal take-downs (withdrawal is applied automatically here but decided by a human).
* **10M viewers, 40 dialects?** Partition by viewer; precompute per-dialect candidate pools; event-sourced facts in a feature store; verifier as a separate service reading authoritative stores; shadow holdout always on; per-dialect availability index.
* **What did the AI suggest that you rejected?** See AI_COLLABORATION.md (model-authored copy, `wa_new_episode_v2`, profile-dialect fallback, tuning to the simulator, over-messaging).
