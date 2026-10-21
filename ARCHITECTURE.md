# Architecture

**Principle:** the core is deterministic and rule-based. The model is *advisory*: it may pick among goals the rules already found
eligible. Nothing it says is evidence, and nothing it says can approve an action.

```
trigger ─► idempotency ─► staleness ─► session ─► arm ─► read facts ─► (re)validate goal ─► choose goal ─► plan page/message
                                                                              ▲ rules (priority)      │
                                                                  model advice (optional, validated)  ▼
                          persist state ◄─ publish ◄─ repair once ◄─ INDEPENDENT VERIFIER (can reject) ◄─ proposal
```

## Ownership
`ViewerAgent` (one per viewer, `agent.py`) is the only writer of that viewer's goal, plan and history. A viewer has at most one
`active_goal`; the planner, the model and the verifier never hold goals. Events for a viewer reach only that agent (`observe`),
and a rule fix is applied through an index of open goals, so nothing else is touched.

## Planning
* **Goal choice** (`goals.py`): evaluate all 12 goals against resolved facts; G10/G11 are unmeasurable and skipped. Priority:
  `G04 > G01 > G03 > G02 > G06 > G07 > G12 > G05(with purchase intent) > G09 > G08 > G05(default)`. Rationale: time-critical and
  high-value first (trial about to be lost, first play, day-2 return), then specific and evidenced (recent lapse, an actual idle
  series), broad defaults last. A goal that failed twice for the viewer is tried last (learning fed back by the judge).
* **Revision, not a schedule:** at each trigger the open goal is re-validated (deadline, success already observed, eligibility on
  fresh facts). Events re-plan immediately: a purchase closes G04/G05/G06 as achieved, cancels purchase-oriented messages and picks
  the next goal; a play that meets the goal does the same.
* **Intervention** (`interventions.py`): a home page for every live, valid-dialect, goal-bearing trigger unless an equivalent page is
  still live (>=30 min TTL left). A message only for goals that need reach *outside* the session (G04 trial, G03 day-2 return,
  G06 win-back); one per goal, one per IST day, >=2 h after the visit, never in 22:00-08:00 IST (pushed to 08:30), never within 3 days of
  another message. Channel: push if consented, else WhatsApp if consented and `hin`, else in-app (needs no consent).
* **Abstention is a decision**: holdout, duplicate, stale, same session, no valid dialect, no eligible measurable goal, page already live,
  load shed; each writes a decision with `no_action_reason`.

## Source handling (precedence per fact; conflicts stay visible in `decision.conflicts`)
| Fact | Rule |
|---|---|
| subscription | newest subscription event (received_at order) beats the profile only if received after the profile row's `updated_at`; else profile, with a disagreeing event shown as a conflict; no profile + no event -> `never` inferred only if the whole history is visible |
| profile row | latest `updated_at` wins; other rows listed as conflicts |
| dialect | the app setting on the trigger (what the app will request). Profile, last `app_dialect_changed`, deeplink are listed, never used. Invalid (d7/NA/null) -> no page |
| consent | most restrictive: every profile row AND no `notification_opt_out` ever; no profile row -> no consent |
| signup | profile beats `signup_completed` event; event used for brand-new viewers |
| time | `received_at` for ordering/freshness; true occurrence = received_at - (sent_at - occurred_at) for buffered events (offset-immune) |
| "never ..." claims | trusted only when the viewer signed up inside the 30-day window (full history visible) |
Every fact carries source, evidence ref (`event:<id>`, `profile:<user>@<updated_at>`, `catalogue:<tid>`, `goal_rule:<id>@<ver>`, `absence:<what>:<user>`) and age.

## Memory
`viewer_state/<viewer>.json`, written atomically (temp + rename) after every decision: goal + history (with rule version),
decisions, actions, live page, scheduled messages, idempotency ledgers (`processed_triggers`, `handled_sessions`), message ledger,
re-plans, outcomes, learnings. A restart with the same state directory is idempotent (tested). `audit` rebuilds any viewer's story.

## Verification (`verifier.py`)
Independent in three structural ways: (1) it only sees a *proposal* and rebuilds facts from the event store / profile at verify time
(a second counted data read), (2) availability, withdrawal, consent, caps and arm come from authoritative stores, not from the planner or
model, (3) it validates model claims against rules. Checks: holdout, duplicate, goal measurable, **eligibility**, deadline, **evidence refs
resolve** (including re-checking claimed absences), dialect, title dialect, **availability at publish time**, injected content, page structure
(<=6 rows, 3-10 titles, unique content_slug, allowed row types, no Continue Watching), **consent**, **quiet hours**, **frequency cap**,
copy length / forbidden phrases / price-or-offer claims (only 1/149/399), deeplink format, approved WhatsApp templates and variable
provenance. On page failures the agent removes the offending titles and re-verifies once; a failing message is dropped, never "fixed".
If the data store is unreadable the verifier **fails closed** for eligibility and consent.

## Latency and budgets
* Per decision: <=8 model calls, <=20 tool calls (`Budget`; breaches degrade, never crash). Typical decision: 1 model call, 5-6 tool calls.
* Model timeout 2 s with no retry (a 45 s provider hang costs us 2 s); malformed JSON is retried once; model skipped if past a 20 s soft
  deadline, if the queue wait exceeds 5 s, or if the circuit breaker is open (3 failures -> 60 s).
* Worker pool with **fresh-first** priority (fresh < aging < stale, FIFO inside a class). Under load, optional model calls are shed first;
  a trigger that cannot start within 45 s is *shed and recorded* rather than published uselessly late.
* Published page time = `deliver_at + queue wait + compute + simulated dependency time`; the simulator needs it before the viewer's home
  view (3-40 s after open), so the deterministic path (no model on the critical path unless needed) matters.

## Failure recovery
| Failure | Response |
|---|---|
| duplicate trigger / delivery | event store dedupes; agent answers `DUPLICATE_IGNORED` (idempotency key = event_id) |
| stale offline-buffered trigger | no action, age recorded |
| model unsupported claim | verifier rejects, recorded in `unsupported_model_claims`, rule priority decides |
| model malformed / timeout / 503 | retry once / fall back to rules / circuit breaker |
| data store slow or down | cached facts (in-memory) else minimal popular-only page; no messages, no new goal commitment |
| purchase after decision | goal closed as achieved, pending purchase messages cancelled, re-plan |
| title withdrawn | live pages replaced (catalogue availability check from that instant), scheduled messages cancelled |
| goal rule corrected | version bump, only open goals of that id switched through an index |
| opt-out event | pending push/WhatsApp cancelled |
| overload | shed model calls, prioritise fresh, shed+record when hopeless |

## Trust boundaries
Untrusted: catalogue text (descriptions, titles), deeplinks, search queries, phone timestamps, model output, profile snapshot freshness.
Catalogue text is only *displayed* as a title name; descriptions are never read by planning code, and records matching instruction-like
patterns are flagged, logged and excluded from promotion. Deeplink contributes only a dialect string for conflict reporting. Message copy is
generated from templates using catalogue names and pricing only. The model gets goal ids and rule reasons only, never raw events or profiles
(and the default mock gets nothing off-machine). Holdout is enforced in the agent *and* by the verifier from `arms.csv`.

## Measurement loop
Judge (`judge.py`) evaluates each goal from attributable events only (occurred after goal start and by its deadline; buffered events that
really happened earlier are excluded; deduplicated), with the rule version recorded on the goal. Outcomes go back into viewer state and
`learnings`. Impact (`impact.py`) is ITT on randomised arms with Wald and bootstrap intervals, plus a goal-free secondary metric.
