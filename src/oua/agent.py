"""One agent per viewer. The agent owns the viewer's single active goal, plan, history and learnings.

Pipeline per trigger:  idempotency -> staleness -> session -> holdout -> read facts -> (re)validate goal -> choose goal
(rules, model advisory) -> plan page/message -> INDEPENDENT VERIFY (repair once) -> publish -> persist.
Every exit path writes a decision, including 'do nothing', with its reason."""
import dataclasses
from datetime import timedelta
from time import perf_counter

from .facts import build_facts
from .goals import rank_eligible, new_goal_record, evaluate, evaluate_success
from .interventions import build_page, plan_message, sanitize_page
from .loader import VALID_DIALECT
from .model import (ModelOutputError, ModelTimeout, ModelUnavailable, parse_model_output)
from .runtime import Budget, BudgetExceeded, StoreTimeout
from .timeutil import iso, parse, ist_day

PURCHASE_GOALS = {"G04", "G05", "G06"}


class ViewerAgent:
    def __init__(self, user, world):
        self.user, self.w = user, world
        self.arm = world.pack.arms.get(user, "unassigned")
        self.state = world.states.load(user, self.arm)
        self.state["arm"] = self.arm
        self.last_trigger = None

    # ===================================================================================== trigger handling
    def on_trigger(self, trig, start_at, queue_wait_s=0.0, shed=False):
        w, s, st, pack = self.w, self.w.s, self.state, self.w.pack
        t0, b = perf_counter(), Budget(s)
        dec = self._new_decision(trig, start_at, queue_wait_s)
        elapsed = lambda: queue_wait_s + (perf_counter() - t0) + b.sim_ms / 1000.0
        pub = lambda: trig.rt + timedelta(seconds=elapsed())

        def done(status, action, reason=None, conf=1.0, conf_reason=""):
            return self._finish(dec, b, t0, queue_wait_s, trig, status, action, reason, conf, conf_reason)

        if trig.id in st["processed_triggers"]:       # 0. idempotency
            dec["duplicate_of"] = st["processed_triggers"][trig.id]
            return done("DUPLICATE_IGNORED", "NO_ACTION", "duplicate trigger: this event_id was already processed; no second action", 1.0,
                        "idempotency key = trigger event_id")
        st["processed_triggers"][trig.id] = dec["decision_id"]
        self.last_trigger = trig
        if shed:                                      # 1. overload shedding
            return done("SHED_OVERLOAD", "NO_ACTION", "load shed: the trigger could not be started within the latency budget "
                        "(fresher triggers were prioritised)", 0.9, "capacity exceeded; recorded, not silently dropped")
        if trig.buf_s > s.stale_trigger_s:            # 2. offline-buffered / stale
            dec["fallback_used"].append("stale_trigger_skipped")
            return done("NO_ACTION", "NO_ACTION", f"stale trigger: event is {int(trig.buf_s)}s old by phone clock (offline-buffered); "
                        "not a live session, so no home page or message is warranted", 0.95,
                        "age = sent_at - occurred_at is immune to phone clock offset")
        if trig.session in st["handled_sessions"]:    # 3. one decision per session
            return done("NO_ACTION", "NO_ACTION", f"session already handled by {st['handled_sessions'][trig.session]}", 0.95,
                        "login_success + app_open of one session are one visit")
        st["handled_sessions"][trig.session] = dec["decision_id"]
        if self.arm != "agent":                       # 4. holdout / unassigned: zero actions, shadow assignment only
            shadow = self._shadow_goal(trig, start_at)
            dec["shadow_goal"] = shadow
            why = "holdout arm: no agent action of any kind" if self.arm == "holdout" else "viewer not in arms.csv: treated as holdout"
            dec["checks"] = {"passed": ["holdout"], "failed": [], "degraded": False}
            return done("NO_ACTION", "NO_ACTION", why, 1.0, "randomised arm from replay/arms.csv; shadow_goal is measurement-only")

        # ---- 5. read facts (tool; may be degraded) ------------------------------------------------------------
        degraded = False
        try:
            events = w.read_viewer_data(self.user, trig, start_at, b)
            facts = build_facts(pack, self.user, events, trig, start_at, s)
            w.cached_facts[self.user] = facts
        except (StoreTimeout, BudgetExceeded) as e:
            degraded = True
            dec["fallback_used"].append("data_store_timeout" if isinstance(e, StoreTimeout) else "tool_budget_exceeded")
            cached = w.cached_facts.get(self.user)
            if cached:
                facts = dataclasses.replace(cached, now=start_at, is_stale_cache=True, conflicts=list(cached.conflicts))
                d = trig.dialect if (trig.dialect and VALID_DIALECT.match(str(trig.dialect))) else None
                facts.dialect, facts.language = d, trig.lang or cached.language
                dec["fallback_used"].append("stale_cached_facts")
            else:
                facts = build_facts(pack, self.user, [], trig, start_at, s, profile_rows=[])
                facts.degraded_reason = "cold_state"
                dec["fallback_used"].append("cold_minimal_page")
            events = []
        dec["dialect"] = {"used": facts.dialect, "source": "app_setting" if facts.dialect else None,
                          "conflicts": [c for c in facts.conflicts if c.split(":")[0] in
                                        ("profile", "deeplink", "app_dialect_changed", "app_setting_invalid")]}
        dec["conflicts"] = list(facts.conflicts)
        if facts.dialect is None:
            return done("NO_ACTION", "NO_ACTION", f"no valid dialect: app setting {trig.dialect!r} is not one of d1-d6, so no page "
                        "can be requested and no content can be named", 0.9, "dialect precedence: app setting only")

        # ---- 6. goal: revalidate the open goal, else choose one -----------------------------------------------
        goal, goal_is_new, elig_refs, rejected, model_note = None, False, [], [], None
        self._revalidate_goal(facts, events, start_at, dec, verify_data=not degraded)
        goal = st["active_goal"]
        if goal is None and not degraded:
            ordered, rejected = rank_eligible(facts, pack, w.rb, s, st["learnings"])
            dec["rejected_goals"] = [{"goal_id": g, "reason": r} for g, r in rejected if g in ("G10", "G11") or True][:12]
            chosen = ordered[0] if ordered else None
            if shed is False and not self._skip_model(b, elapsed, queue_wait_s, dec) and ordered is not None:
                picked = self._ask_model(trig, facts, ordered, start_at, b, dec)
                if picked and picked != (chosen.goal_id if chosen else None):
                    chosen = next(e for e in ordered if e.goal_id == picked)
            if chosen:
                goal = new_goal_record(chosen, start_at, w.rb, s)
                goal_is_new, elig_refs = True, list(chosen.refs)
                dec["_elig"] = chosen
        elif goal is not None:
            e = evaluate(goal["goal_id"], facts, pack, w.rb, s)
            elig_refs = list(e.refs) if e.eligible else []
            dec["_elig"] = e if e.eligible else None
        if goal is None and degraded:
            dec["fallback_used"].append("no_new_goal_while_degraded")

        # ---- 7. plan the intervention ----------------------------------------------------------------------------
        aging = trig.buf_s > s.fresh_trigger_s
        want_page, page, msgs, notes = True, None, [], []
        live = st["live_page"]
        if live and live["dialect"] == facts.dialect and parse(live["expires_at"]) - start_at >= timedelta(minutes=s.page_refresh_remaining_min) \
                and not goal_is_new:
            want_page = False
            notes.append(f"page already live until {live['expires_at']} ({live['action_id']})")
        if goal is None and not degraded:
            return done("NO_ACTION", "NO_ACTION", "no eligible measurable goal with supporting evidence: " +
                        "; ".join(f"{g}: {r}" for g, r in rejected[:4]), 0.8,
                        "abstaining is proportionate: a home page without a goal is just recommendation")
        gid = goal["goal_id"] if goal else None
        gctx = (goal or {}).get("context", {}) if goal else {}
        minimal = degraded and facts.degraded_reason == "cold_state"
        try:
            if want_page:
                b.tool("catalogue.candidates", s.catalogue_ms)
                b.tool("catalogue.new_releases", s.catalogue_ms)
                page = build_page(pack, facts, gid, gctx, s, start_at, minimal=minimal)
                if not page["rows"] and not page["hero_title_id"]:
                    page = None
                    notes.append("no valid page could be built (fewer than 3 valid titles per row)")
            if not degraded and not aging and goal is not None:
                m, why = plan_message(pack, facts, goal, st, s, start_at, page)
                notes.append(why)
                if m:
                    msgs.append(m)
        except BudgetExceeded as e:
            dec["fallback_used"].append("tool_budget_exceeded")
            notes.append(str(e))
        if aging:
            notes.append("trigger is aging (120-600 s): home page only, no message")
        if not page and not msgs:
            return done("NO_ACTION", "NO_ACTION", "; ".join(notes) or "nothing proportionate to do", 0.85,
                        "viewer state does not call for a change")

        # ---- 8. independent verification (+ one repair) ---------------------------------------------------------
        prop = dict(user=self.user, trigger=trig, now=pub(), goal=goal if gid else None,
                    evidence=[trig.ref] + elig_refs + (page["evidence"] if page else []), dialect=facts.dialect,
                    page=page, messages=msgs)
        res, vfacts = self._verify(prop, trig, start_at, b, degraded)
        if res["failed"]:
            bad_titles, hard = [], []
            for fl in res["failed"]:
                if fl["check"] in ("title_dialect", "availability", "injected_content", "page_structure"):
                    bad_titles += [t for t in self._ids_in(page, msgs)
                                   if (pack.catalogue.get(t) is None or pack.catalogue.get(t).dialect != facts.dialect
                                       or not pack.catalogue.available(pack.catalogue.get(t), pub())
                                       or pack.catalogue.get(t).flagged)]
                else:
                    hard.append(fl)
            dec["verifier_first_pass_failures"] = res["failed"]
            soft = [f for f in res["failed"] if f["check"] in ("consent", "quiet_hours", "frequency_cap", "copy_length",
                    "copy_forbidden_phrase", "copy_price_claim", "deeplink", "template_approved", "template_variables",
                    "template_values_from_catalogue_or_pricing")]
            if soft:                                  # a bad message is dropped, never 'repaired'
                msgs = []
                prop["messages"] = []
            if bad_titles and msgs:
                msgs = [m for m in msgs if m["title_id"] not in set(bad_titles)]
                prop["messages"] = msgs
            if bad_titles and page:
                page = sanitize_page(pack, page, bad_titles, s)
                prop["page"] = page if (page["rows"] or page["hero_title_id"]) else None
                page = prop["page"]
            res, vfacts = self._verify(dict(prop, now=pub()), trig, start_at, b, degraded)
        dec["checks"] = res
        blocking = [f for f in res["failed"]]
        if blocking:
            return done("NO_ACTION", "NO_ACTION", "verifier rejected the proposal: " +
                        "; ".join(f"{f['check']} ({f['detail']})" for f in blocking[:3]), 0.9,
                        "independent checks disagreed with the planner; nothing published")

        # ---- 9. publish -------------------------------------------------------------------------------------------
        if goal_is_new:
            self._commit_goal(goal, dec)
        acts = self._publish(trig, dec, page, msgs, pub(), goal, facts)
        dec["content"] = (["title:" + page["hero_title_id"]] if page and page["hero_title_id"] else []) + \
            [f"title:{t}" for r in (page["rows"] if page else []) for t in r["title_ids"]] + \
            [f"title:{m['title_id']}" for m in msgs]
        dec["evidence"] = prop["evidence"]
        dec["assumptions"] = self._assumptions(facts, goal, dec, page)
        conf, cr = self._confidence(dec.get("_elig"), facts, dec, degraded)
        primary = "HOME_PAGE" if page else (msgs[0]["type"].upper() if msgs else "NO_ACTION")
        dec["action_types"] = [a["type"] for a in acts]
        dec["action_ids"] = [a["action_id"] for a in acts]
        dec["notes"] = notes
        return done("PUBLISHED", primary, None, conf, cr)

    # =================================================================================================== helpers
    def _new_decision(self, trig, start_at, queue_wait_s):
        return dict(decision_id=self.w.next_decision_id(), viewer_id=self.user, arm=self.arm,
                    trigger=dict(event=trig.type, event_id=trig.id, received_at=iso(trig.rt), session_id=trig.session,
                                 event_age_s=round(trig.buf_s, 1), buffered=trig.buf_s > self.w.s.stale_trigger_s,
                                 queue_wait_s=round(queue_wait_s, 3)),
                    goal=None, action="NO_ACTION", dialect=None, content=[], evidence=[], assumptions=[], conflicts=[],
                    checks={"passed": [], "failed": [], "degraded": False}, fallback_used=[], model_trace=[],
                    unsupported_model_claims=[], rule_version=self.w.s.rule_version)

    def _skip_model(self, b, elapsed, queue_wait_s, dec):
        s, w = self.w.s, self.w
        if queue_wait_s > s.shed_queue_wait_s:
            dec["fallback_used"].append("model_skipped_overload"); return True
        if elapsed() > s.soft_deadline_s:
            dec["fallback_used"].append("model_skipped_deadline"); return True
        if not w.breaker.allow(self.last_trigger.rt + timedelta(seconds=elapsed())):
            dec["fallback_used"].append("model_skipped_circuit_open"); return True
        if b.model_calls >= s.max_model_calls:
            dec["fallback_used"].append("model_skipped_budget"); return True
        return False

    def _ask_model(self, trig, facts, ordered, start_at, b, dec):
        w, s = self.w, self.w.s
        req = dict(task="goal_advice", trigger_event_id=trig.id, viewer_id=self.user,
                   eligible_goals=[dict(goal_id=e.goal_id, strength=e.strength, reasons=e.reasons, refs=e.refs) for e in ordered])
        for attempt in (1, 2):
            at = start_at + timedelta(seconds=b.sim_ms / 1000)
            try:
                raw = w.model.complete(req, at, attempt)
                b.model(s.mock_model_latency_ms)
                out = parse_model_output(raw)
                w.breaker.success()
                gid, problem = w.verifier.validate_model_claim(out, {e.goal_id for e in ordered}, facts)
                dec["model_trace"].append(dict(attempt=attempt, outcome="accepted" if gid else "rejected",
                                               claimed_goal=out.get("goal_id"), claimed_confidence=out.get("confidence")))
                if problem:
                    dec["unsupported_model_claims"].append(dict(claim=out, problem=problem))
                    dec["fallback_used"].append("model_claim_rejected_by_verifier")
                return gid
            except ModelOutputError as e:          # the call was already counted when it returned
                dec["model_trace"].append(dict(attempt=attempt, outcome="malformed_output", detail=str(e)))
                dec["fallback_used"].append("model_output_malformed")
                continue                                   # one retry, within the call budget
            except (ModelTimeout, ModelUnavailable) as e:
                b.spend(e.cost_ms)
                w.breaker.failure(at)
                dec["model_trace"].append(dict(attempt=attempt, outcome=e.kind, detail=str(e)))
                dec["fallback_used"].append(f"model_{e.kind}")
                return None                                # no retry: protect the latency budget
            except BudgetExceeded:
                dec["fallback_used"].append("model_skipped_budget")
                return None
        dec["fallback_used"].append("model_unusable_rules_only")
        return None

    def _revalidate_goal(self, facts, events, now, dec, verify_data=True):
        st, w = self.state, self.w
        g = st["active_goal"]
        if not g:
            return
        if parse(g["deadline"]) <= now:
            self._close_goal(g, "deadline_passed", now)
            return
        if not verify_data:
            return                                         # cannot re-check eligibility without data; keep goal, no new commitments
        status, refs, detail = evaluate_success(w.pack, w.rb, g, events, now)
        if status == "succeeded":
            self._close_goal(g, "success_observed", now, refs)
            return
        e = evaluate(g["goal_id"], facts, w.pack, w.rb, w.s)
        if not e.eligible and g["goal_id"] not in ("G01", "G03"):   # G01/G03 'new user' windows lapse by design, handled by deadline
            self._close_goal(g, "no_longer_eligible: " + e.reasons[0], now)
        elif not e.eligible and g["goal_id"] in ("G01",) and facts.plays:
            self._close_goal(g, "no_longer_eligible: " + e.reasons[0], now)

    def _commit_goal(self, goal, dec):
        st = self.state
        st["active_goal"] = goal
        self.w.open_goals[goal["goal_id"]].add(self.user)
        dec["goal_committed"] = True

    def _close_goal(self, g, reason, now, refs=None):
        g["status"], g["closed_reason"], g["closed_at"] = "closed", reason, iso(now)
        if refs:
            g["closing_evidence"] = refs
        self.state["goal_history"].append(g)
        self.state["active_goal"] = None
        self.w.open_goals[g["goal_id"]].discard(self.user)

    def _shadow_goal(self, trig, start_at):
        """Measurement only: which goal would the agent have chosen? Reads the store directly; touches nothing."""
        events = self.w.pack.store.events(self.user, upto=start_at)
        f = build_facts(self.w.pack, self.user, events, trig, start_at, self.w.s)
        if f.dialect is None:
            return None
        ordered, _ = rank_eligible(f, self.w.pack, self.w.rb, self.w.s, None)
        return dict(goal_id=ordered[0].goal_id, context=dict(ordered[0].context)) if ordered else None

    def _verify(self, prop, trig, start_at, b, degraded):
        w = self.w
        vfacts, events = None, []
        try:
            events = w.read_viewer_data(self.user, trig, start_at, b, salt="verify")
            vfacts = build_facts(w.pack, self.user, events, trig, start_at, w.s)
        except StoreTimeout:
            vfacts = None
        except BudgetExceeded:
            vfacts = None
        st = self.state
        ledger = set(st["message_ledger"]) | ({str(d) for d in vfacts.message_days} if vfacts else set())
        env = dict(facts=vfacts, events=events, store=w.pack.store, arm=self.arm,
                   handled_triggers={k: v for k, v in st["processed_triggers"].items() if k != trig.id},
                   ledger_days=ledger, in_app_days={d for d, ids in st.get("in_app_ledger", {}).items()})
        try:
            b.tool("verifier.catalogue_check", w.s.catalogue_ms)
        except BudgetExceeded:
            pass
        return w.verifier.verify(prop, env), vfacts

    @staticmethod
    def _ids_in(page, msgs):
        ids = ([page["hero_title_id"]] if page and page["hero_title_id"] else []) + \
              [t for r in (page["rows"] if page else []) for t in r["title_ids"]] + [m["title_id"] for m in msgs]
        return ids

    # ------------------------------------------------------------------------------------------- publishing
    def _publish(self, trig, dec, page, msgs, at, goal, facts):
        w, st, s = self.w, self.state, self.w.s
        acts = []
        base = dict(decision_id=dec["decision_id"], user_id=self.user, trigger_event_id=trig.id)
        if page:
            a = dict(base, action_id=w.next_action_id(), type="home_page", published_at=iso(at), dialect=page["dialect"],
                     hero_title_id=page["hero_title_id"], rows=page["rows"], expires_at=iso(at + timedelta(minutes=s.page_ttl_min)),
                     goal_id=goal["goal_id"] if goal else None, status="published")
            acts.append(a)
            slugs = {w.pack.catalogue.get(t).slug for t in self._ids_in(page, [])}
            st["live_page"] = dict(action_id=a["action_id"], dialect=page["dialect"], published_at=a["published_at"],
                                   expires_at=a["expires_at"], slugs=sorted(slugs))
            for sl in slugs:
                w.live_pages[sl].add(self.user)
        for m in msgs:
            a = dict(base, action_id=w.next_action_id(), type=m["type"], published_at=iso(at), title_id=m["title_id"],
                     goal_id=m["goal_id"])
            if m["type"] == "push":
                a.update(send_at=m["send_at"], text=m["text"], deeplink=m["deeplink"], status="scheduled")
            elif m["type"] == "whatsapp":
                a.update(send_at=m["send_at"], template_id=m["template_id"], variables=m["variables"], status="scheduled")
            else:
                a.update(text=m["text"], status="published")
                st.setdefault("in_app_ledger", {}).setdefault(str(ist_day(at)), []).append(a["action_id"])
            if m["type"] in ("push", "whatsapp"):
                st["message_ledger"][str(ist_day(parse(m["send_at"])))] = a["action_id"]
                st["pending"].append(a["action_id"])
                w.pending_by_slug[w.pack.catalogue.get(m["title_id"]).slug].add(self.user)
            goal["message_planned"] = True
            acts.append(a)
        for a in acts:
            w.actions.append(a)
            st["actions"].append(a["action_id"])
        return acts

    def _assumptions(self, facts, goal, dec, page):
        a = []
        e = dec.get("_elig")
        if e:
            a += list(e.assumptions)
        if facts.sub_source == "profile":
            a.append("subscription status comes from the profile snapshot (up to a day stale); no newer subscription event seen")
        if facts.is_stale_cache:
            a.append("data store unreadable: facts are cached from an earlier decision and may be missing newer events")
        if page:
            a += page.get("assumptions", [])
        if not facts.consent_push and not facts.consent_wa:
            a.append("no push/WhatsApp consent on record, so no message channel is used")
        return a

    def _confidence(self, elig, facts, dec, degraded):
        if elig is None:
            return 0.4, "goal retained or no rule evidence recorded for this decision"
        base = {"strong": 0.8, "medium": 0.6, "weak": 0.4}.get(elig.strength, 0.5)
        why = [f"{elig.strength} evidence for {elig.goal_id}: {elig.reasons[0]}"]
        if degraded or facts.is_stale_cache:
            base -= 0.2; why.append("data store unreadable, cached facts used (-0.2)")
        if facts.sub_source == "profile" and elig.goal_id in ("G04", "G05", "G06", "G08", "G09"):
            base -= 0.05; why.append("subscription state is from a day-old snapshot (-0.05)")
        n = len([c for c in facts.conflicts if not c.startswith("app_setting_invalid")])
        if n:
            base -= 0.05; why.append(f"{n} conflicting source(s) resolved by precedence rules (-0.05)")
        if any(x.startswith("model_") for x in dec["fallback_used"]):
            why.append("model unavailable/rejected: rule priority decided")
        return round(max(0.1, min(0.95, base)), 2), "; ".join(why)

    def _finish(self, dec, b, t0, queue_wait_s, trig, status, action, reason, conf, conf_reason):
        w = self.w
        compute_ms = (perf_counter() - t0) * 1000
        b.tool("state.write", w.s.store_write_ms) if b.tool_calls < w.s.max_tool_calls else b.spend(w.s.store_write_ms)
        total_ms = compute_ms + b.sim_ms + queue_wait_s * 1000
        elig = dec.pop("_elig", None)
        g = self.state["active_goal"] if status == "PUBLISHED" else None
        if g:
            dec["goal"] = dict(id=g["goal_id"], name=g["name"], started_at=g["started_at"], deadline=g["deadline"],
                               success_signal=g["success"], rule_version=g["rule_version"], window_hours=g["window_hours"],
                               new=bool(dec.pop("goal_committed", False)))
        dec.pop("goal_committed", None)
        dec.update(status=status, action=action, confidence=conf, confidence_reason=conf_reason,
                   no_action_reason=reason, model_calls=b.model_calls, tool_calls=b.tool_calls, tool_log=b.tool_log,
                   decided_in_ms=round(total_ms, 1), compute_ms=round(compute_ms, 2), simulated_wait_ms=round(b.sim_ms, 1),
                   published_at=iso(trig.rt + timedelta(milliseconds=total_ms)))
        if action == "NO_ACTION" and not dec.get("checks"):
            dec["checks"] = {"passed": [], "failed": [], "degraded": False}
        self.state["decisions"].append(dict(decision_id=dec["decision_id"], status=status, action=action,
                                            goal=(dec["goal"] or {}).get("id") if dec.get("goal") else None,
                                            reason=reason, published_at=dec["published_at"]))
        self.state["facts_summary"] = dict(as_of=iso(trig.rt), dialect=dec["dialect"]["used"] if dec.get("dialect") else None)
        w.states.save(self.user)
        w.decisions.append(dec)
        return dec

    # ====================================================================================== events & replanning
    def observe(self, ev, now):
        """A live event arrived for this viewer. Update state, re-plan if the situation changed, cancel what no longer fits."""
        if self.arm != "agent":
            return
        st, w = self.state, self.w
        g = st["active_goal"]
        if ev.type == "notification_opt_out":
            ch = ev.props.get("channel")
            self.cancel_pending(now, f"viewer opted out of {ch}", lambda a: a["type"] == ("push" if ch == "push" else "whatsapp"))
        if ev.type == "purchase_completed" and not ev.props.get("is_trial"):
            cancelled = self.cancel_pending(now, "purchase observed: purchase-oriented message no longer makes sense",
                                            lambda a: a.get("goal_id") in PURCHASE_GOALS or a.get("template_id") == "wa_trial_ending_v1")
            old = g["goal_id"] if g else None
            if g and g["goal_id"] in PURCHASE_GOALS:
                self._close_goal(g, "success_observed: purchase", now, [ev.ref])
            self._replan(now, f"purchase_completed {ev.id}", old, cancelled)
        elif g and ev.type in ("play_started", "episode_completed", "app_open"):
            status, refs, _ = evaluate_success(w.pack, w.rb, g, w.pack.store.events(self.user, upto=now), now)
            if status == "succeeded":
                self._close_goal(g, "success_observed", now, refs)
                self._replan(now, f"goal achieved via {ev.id}", g["goal_id"], [])

    def _replan(self, now, reason, old_goal, cancelled):
        w, st = self.w, self.state
        new_goal = None
        if self.last_trigger is not None:
            events = w.pack.store.events(self.user, upto=now)
            f = build_facts(w.pack, self.user, events, self.last_trigger, now, w.s)
            if f.dialect:
                ordered, _ = rank_eligible(f, w.pack, w.rb, w.s, st["learnings"])
                if ordered:
                    goal = new_goal_record(ordered[0], now, w.rb, w.s)
                    st["active_goal"] = goal
                    w.open_goals[goal["goal_id"]].add(self.user)
                    new_goal = goal["goal_id"]
        rec = dict(viewer_id=self.user, at=iso(now), reason=reason, old_goal=old_goal, new_goal=new_goal,
                   cancelled_actions=cancelled)
        st["replans"].append(rec)
        w.replans.append(rec)

    def cancel_pending(self, now, reason, pred):
        w, st = self.w, self.state
        cancelled = []
        by_id = {a["action_id"]: a for a in w.actions if a["action_id"] in st["pending"]}
        for aid, a in by_id.items():
            if a["status"] == "scheduled" and pred(a):
                a["status"], a["cancel_reason"], a["cancelled_at"] = "cancelled", reason, iso(now)
                st["pending"].remove(aid)
                st["message_ledger"] = {k: v for k, v in st["message_ledger"].items() if v != aid}
                cancelled.append(aid)
        return cancelled

    def on_withdrawal(self, slug, now, dec_seq_hook=None):
        """A title was withdrawn: cancel scheduled messages that promote it, and replace a live page that contains it."""
        w, st, s = self.w, self.state, self.w.s
        cancelled = self.cancel_pending(now, f"title withdrawn ({slug})",
                                        lambda a: w.pack.catalogue.get(a.get("title_id")) is not None
                                        and w.pack.catalogue.get(a["title_id"]).slug == slug)
        replaced = None
        live = st["live_page"]
        if self.arm == "agent" and live and slug in live["slugs"] and parse(live["expires_at"]) > now:
            old = next(a for a in w.actions if a["action_id"] == live["action_id"])
            page = dict(dialect=old["dialect"], hero_title_id=old["hero_title_id"], rows=old["rows"], evidence=[], assumptions=[])
            bad = [t for t in self._ids_in(page, []) if w.pack.catalogue.get(t).slug == slug]
            page = sanitize_page(w.pack, page, bad, s)
            if page["rows"] or page["hero_title_id"]:
                a = dict(decision_id=f"{old['decision_id']}+withdrawal", user_id=self.user, trigger_event_id=old["trigger_event_id"],
                         action_id=w.next_action_id(), type="home_page", published_at=iso(now), dialect=old["dialect"],
                         hero_title_id=page["hero_title_id"], rows=page["rows"], expires_at=old["expires_at"],
                         goal_id=old.get("goal_id"), status="published", replaces=old["action_id"],
                         reason=f"title in {slug} withdrawn")
                w.actions.append(a)
                st["actions"].append(a["action_id"])
                st["live_page"] = dict(live, action_id=a["action_id"],
                                       slugs=sorted({w.pack.catalogue.get(t).slug for t in self._ids_in(page, [])}))
                replaced = a["action_id"]
        rec = dict(viewer_id=self.user, at=iso(now), reason=f"title withdrawn ({slug})", old_goal=None, new_goal=None,
                   cancelled_actions=cancelled, replacement_page=replaced)
        st["replans"].append(rec)
        w.replans.append(rec)
        return rec

    def release_due(self, now, final=False):
        """Re-verify scheduled messages whose send time has come (or at replay end): consent, cap, dialect, availability
        and goal state may have changed since they were scheduled."""
        w, st = self.w, self.state
        for aid in list(st["pending"]):
            a = next(x for x in w.actions if x["action_id"] == aid)
            if a["status"] != "scheduled":
                continue
            due = parse(a["send_at"]) <= now
            if not due and not final:
                continue
            trig = self.last_trigger
            events = w.pack.store.events(self.user, upto=now)
            f = build_facts(w.pack, self.user, events, trig, now, w.s) if trig else None
            ok, why = True, ""
            if f is None or f.dialect is None:
                ok, why = False, "viewer dialect unknown at send time"
            else:
                m = dict(a)
                prop = dict(user=self.user, trigger=trig, now=min(now, parse(a["send_at"])) if due else now, goal=None,
                            evidence=[], dialect=f.dialect, page=None, messages=[m])
                ledger = {k for k, v in st["message_ledger"].items() if v != aid} | {str(d) for d in f.message_days}
                env = dict(facts=f, events=events, store=w.pack.store, arm=self.arm, handled_triggers={}, ledger_days=ledger)
                res = w.verifier.verify(prop, env)
                if res["failed"]:
                    ok, why = False, "; ".join(f"{x['check']}: {x['detail']}" for x in res["failed"][:2])
                g = st["active_goal"]
                if ok and g and a.get("goal_id") == g["goal_id"]:
                    status, _, _ = evaluate_success(w.pack, w.rb, g, events, now)
                    if status == "succeeded":
                        ok, why = False, "goal already achieved before send time"
            if not ok:
                self.cancel_pending(now, "re-verification at send time failed: " + why, lambda x: x["action_id"] == aid)
            elif due:
                a["status"] = "sent"
                st["pending"].remove(aid)
