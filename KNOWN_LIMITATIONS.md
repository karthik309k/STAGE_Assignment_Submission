# Known limitations (deliberate, and why)

**Measurement**
- **The replay shows no detectable uplift** (+1.2 pts, 95% CI -2.1 to +4.5, one day, ~750 viewers per arm). Power is low: only effects of roughly 5 points or more are detectable. We report that rather than a number we cannot defend. Per-goal cells are tiny and descriptive only.
- **The result depends on the simulator seed.** The same actions gave +1.2 pts (seed `oua-practice`), -1.9 pts (`another-seed`) and a mean of about +1 pt across extra seeds (`impact_report.md` section 8). Quote the interval and the robustness table, never one run. The only consistent positive is a small rise in plays per viewer (about +4%), which is an engagement measure, not a goal outcome.
- Evidence comes from a **simulator**. An effect (or none) there says how the simulator's behaviour model responds to our pages, not how real viewers behave.
- The holdout's goal is a **shadow assignment** by the same planner on pre-treatment facts. Agent-arm viewers who hit degraded paths may in principle be assigned slightly differently by the live agent; the ITT analysis uses the shadow assignment for both arms so the groups stay comparable.
- The corrected G03 rule affected **0 goals** in this replay: G01 outranks G03 for new viewers, and the only G03 goal opened after the correction. The mechanism (version bump, open-goal index, no reprocessing) is proven by tests (`test_rule_correction_touches_only_open_g03_goals`), not by this replay.

**Model**
- The default model is a **deterministic mock**. It never adds information beyond rule priority, so this submission cannot show that a model improves decisions; it shows that a model's failure modes (unsupported claim, malformed JSON, timeout, 503) are contained. The optional Anthropic provider has only been unit-tested against a fake transport, never the live API.
- Three of the five injected model faults landed on triggers handled earlier in the pipeline (two stale offline opens, one holdout viewer), so the **timeout path is exercised by a test, not by the replay**.

**Goals and content**
- Goal priority is a hand-written order (see ARCHITECTURE.md), not learned. "Learnings" currently only demote a goal that failed twice for a viewer; there is no cross-viewer learning.
- Interpretations of vague rules (G06 recent = lapse event within 30 days; G09; G12; 'never' needs full visible history) are my choices. G06 therefore excludes viewers whose profile says `lapsed` but who have no lapse event in the pack (recency unknown).
- **Measurability is inferred from the event stream** (an event type that never occurs makes its goal unmeasurable). That correctly caught `tv_app_open` (G11) but would wrongly disable a goal if a pack happened to contain no purchase at all. A real system would read the event schema.
- `wa_new_episode_v2` is never used: the pack has no episode release times, so "a new episode is out" cannot be evidenced.
- WhatsApp is used only for viewers whose language is `hin` (templates are Hindi); `dialect_name` is the catalogue dialect code (d1..d6), the only value the pack offers.
- Ranking is simple (history popularity, genre taste, availability). No personalised model, no diversity tuning, nothing optimised against the simulator's formula (deliberately).
- A trigger with an invalid app dialect (d7 / NA / null) gets **no page**; a fallback to the profile dialect would be a guess about what the app requests.
- Withdrawal is applied to the whole content_slug (all language variants): conservative, because the licence presumably covers the content.

**Systems**
- Latency = measured compute + **simulated** dependency time + queue wait; the replay itself runs faster than real time. Real network latency was not measured.
- Single process, in-memory indexes, JSON files for state. No distributed locking, no real queue, no backpressure to upstream. Per-viewer ordering relies on one process handling a viewer at a time (see ARCHITECTURE.md for the 10M-viewer design).
- The cache used when the data store is down lives in memory; after a restart during an outage a viewer is "cold" and gets the minimal safe page.
- Scheduled messages whose send time is after the replay ends are verified as of the end of the replay and left `scheduled`; the pack gives no way to re-verify them later.
- No UI, no real push/WhatsApp integration, no PII redaction in the logs beyond using the pack's synthetic ids.
