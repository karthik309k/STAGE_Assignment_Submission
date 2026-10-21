# Final report

Replay of 2769 trigger deliveries (1746 viewers) against the supplied pack; simulator seed `oua-practice`.

## Results at a glance

- Decisions: {'PUBLISHED': 895, 'NO_ACTION': 1784, 'DUPLICATE_IGNORED': 90}. Agent arm 1397, holdout 1372.
- Actions published: {'home_page': 907, 'push': 35, 'whatsapp': 2, 'in_app_message': 5}. **Actions to holdout viewers: 0.** Simulator policy problems: 0.
- Latency (decided_in_ms, includes simulated dependency time and queue wait): p50 11.3, p95 371.9, max 2229.4; decisions over 60 s: 0, over 5 min: 0; shed: 0.
- Goal outcomes judged: {'failed': 620, 'succeeded': 81}.
- **Impact (ITT, agent vs holdout): 11.7% vs 10.5%, +1.2 pts, 95% CI -2.1 pts to +4.5 pts.** The interval includes zero: we do not claim an uplift.
- Seed robustness (impact_report.md section 8): re-simulating the same actions with other seeds moves the binary-metric differences by about their own size; plays per viewer is higher for the agent arm on most seeds (small, simulator-based). Do not quote any single seed as the effect.
- Pilot: the report's +22.4 pts is a selection artefact; the randomised pilot difference is -1.3 pts (95% CI -3.8 pts to +1.2 pts).

## Why the agent did nothing (NO_ACTION reasons, count)

- holdout arm: 997
- stale trigger (offline-buffered, not a live session): 424
- session already handled: 198
- no valid dialect: 85
- page already live (same dialect, >=30 min of TTL left): 69
- no eligible measurable goal with supporting evidence: G01: not signed up within 7 days (or: 11

## Data issues found and how each was handled

| Issue | Size | Handling |
|---|---|---|
| Duplicate events (same event_id) | 2176 history lines, 90 trigger lines | First delivery wins in the event store; a repeated trigger gets a DUPLICATE_IGNORED decision (idempotency key = event_id), never a second action |
| Late events / wrong phone clocks | clock skew in ~15% of events | Ordering and freshness use received_at; occurrence time = received_at minus (sent_at - occurred_at), which is immune to a constant clock offset |
| Offline-buffered app opens | 432 triggers older than 10 min (424 reached the agent arm) | Not a live session: NO_ACTION with reason and age recorded; their facts still enter state at the estimated true time |
| Stale / duplicated profile snapshot | 30 viewers with two rows | Latest updated_at wins, the other row is listed as a conflict; newer subscription events override the snapshot; consent takes the most restrictive value |
| Brand-new viewers without a profile | 103 trigger viewers | Facts built from signup_completed; consent unknown = no push/WhatsApp (in-app only); 'never paid' inferred only when whole history is visible |
| Dialect conflicts (app / profile / deeplink) | see dialect.conflicts in decisions | App setting on the trigger decides; every disagreeing source is listed, never used |
| Invalid dialect values (d7, NA, null) | 185 triggers | No valid dialect = no page and no content; recorded as NO_ACTION |
| Language variants of one content_slug | 514 slugs | One title per slug per page; variant chosen by the viewer's app language, in the page dialect |
| Availability windows / withdrawal at 07:00Z | 34 viewers held the withdrawn title | Availability checked at publish time by planner and verifier; on withdrawal live pages were replaced and scheduled messages cancelled (12 viewers changed) |
| Instructions inside catalogue descriptions | 2 titles | Treated as data: flagged, logged, excluded from promotion; the verifier fails any action naming them. Goal selection never targets them |
| Unmeasurable goals | G10, G11 | G10 has success.event=null; G11's tv_app_open never occurs in any event file. Never selected; reported as unmeasurable |
| Underspecified goal rules | G06 'recently', G09 'rarely', G12 'stopped', 'never' claims | Interpretations written down in goals.py and attached as assumptions; G06 requires a lapse event within 30 days (no guessing for lapsed profiles with no date) |
| Goal success rule corrected mid-replay (G03) | 0 open goals affected | Rule versioned (v1 -> v2); only open G03 goals (indexed by goal id) are switched; nothing else reprocessed |
| Pilot report compares served vs everyone else | see impact_report.md section 5 | Recomputed with randomised arms (ITT) |
| Model faults (unsupported claim / malformed / timeout) and outages | fallbacks: model_claim_rejected_by_verifier=1, model_output_malformed=1, model_skipped_circuit_open=5, model_unavailable=5 | Model advisory only; verifier rejects unsupported claims; malformed JSON retried once; timeout/503 -> rule priority (circuit breaker); data-store failure -> cached facts, no messages, no new goals |

## Surprise events: what the system noticed and did

- `2026-10-01T07:00:00.000Z` title_withdrawn: {"detail": {"title_id": "T0001", "reason": "licence ended early; the title can no longer be shown or promoted"}}
- `2026-10-01T07:00:00.000Z` withdrawal_response: {"title_id": "T0001", "slug": "rava-baya-d1", "viewers_checked": 34, "viewers_changed": 12, "detail": "live pages replaced and scheduled messages cancelled; later pages exclude the title by availability check"}
- `2026-10-01T08:30:00.000Z` goal_rule_corrected: {"detail": {"goal_id": "G03", "old_success": {"event": "app_open", "condition": "on a later IST calendar day within the window"}, "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"}, "applies_to": "every G03 goal that is still open, including goals 
- `2026-10-01T08:30:00.000Z` rule_correction_applied: {"goal_id": "G03", "new_version": "v2", "affected_open_goals": 0, "total_viewers_with_agents": 866, "reprocessed_unaffected_viewers": 0}
- `2026-10-01T10:35:00.000Z` dependency_unavailable: {"detail": {"dependency": "model_provider", "until": "2026-10-01T10:42:00.000Z", "detail": "every model call fails with HTTP 503"}}
- `2026-10-01T13:10:00.000Z` dependency_degraded: {"detail": {"dependency": "user_data_store", "until": "2026-10-01T13:17:00.000Z", "detail": "p95 read latency 8 s and 20% of reads time out"}}

## Re-planning during the replay (replans.jsonl)

47 re-plans: {'purchase observed': 13, 'title withdrawn': 34}. Scheduled messages cancelled before sending: 3 (see actions_cancelled.jsonl for each reason). Re-plans are triggered by events for the affected viewer only; no other viewer was reprocessed.

## Honest caveats

- The mock model never adds information beyond rule priority; the architecture limits its blast radius rather than relying on it. See KNOWN_LIMITATIONS.md.
- Several injected faults landed on triggers handled earlier in the pipeline (stale or holdout viewers); the timeout path is exercised by tests rather than by this replay.
- Per-goal impact cells are small. One replay day cannot distinguish small effects from noise.
