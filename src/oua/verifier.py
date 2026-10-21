"""Independent verifier. It can (and does) reject decisions.

Independence is structural, not cosmetic:
  * it receives only a PROPOSAL (what the planner wants to do) and re-derives everything it relies on from the
    authoritative sources at verification time: facts are rebuilt from the event store / profile snapshot, availability is
    read from the catalogue at publish time (withdrawals included), consent comes from rebuilt facts, caps from the ledger;
  * it never reads the planner's or the model's reasoning, confidence or text as proof;
  * the arm assignment comes from replay/arms.csv, not from agent state.
If the data store cannot be read, goal-bearing proposals fail closed ('eligibility' and 'consent' cannot be verified).
"""
import re

from .goals import evaluate
from .loader import VALID_DIALECT
from .timeutil import in_quiet_hours, ist_day, parse, iso

ALLOWED_ROW_TYPES = {"next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"}
FORBIDDEN = ["last chance", "hurry", "only today", "free forever", "guaranteed"]
MONEY_RE = re.compile(r"(₹|rs\.?|inr)\s*\d+|\d+\s*(₹|rs\b|rupee)", re.I)
OFFER_RE = re.compile(r"\b(discount|offer|cashback|coupon|free|off)\b|%", re.I)
DEEPLINK_RE = re.compile(r"^ott://title/(T\d{4})$")


class Verifier:
    def __init__(self, pack, settings, rb):
        self.pack, self.s, self.rb = pack, settings, rb
        self.allowed_prices = {pl["price_inr"] for pl in pack.pricing["plans"]}
        self.templates = {t["template_id"]: t for t in pack.interventions["whatsapp"]["approved_templates"]}

    # ------------------------------------------------------------------------------------------ evidence refs
    def _ref_ok(self, ref, user, events, store):
        pack = self.pack
        kind, _, rest = ref.partition(":")
        if kind == "event":
            return rest in store.seen
        if kind == "catalogue":
            return pack.catalogue.get(rest) is not None
        if kind == "profile":
            u, _, ts = rest.partition("@")
            return any(iso(r["updated_at"]) == ts for r in pack.profiles.get(u, []))
        if kind == "goal_rule":
            gid, _, ver = rest.partition("@")
            return gid in self.rb.goals and any(v["version"] == ver for v in self.rb.versions[gid])
        if kind == "episodes":
            return rest in pack.episodes
        if kind == "popularity":
            return VALID_DIALECT.match(rest) is not None
        if kind == "derived":
            return True
        if kind == "event-play":
            return True
        if kind == "absence":
            what = rest.split(":")[0]
            types = {"play_started": {"play_started"}, "episode_completed": {"episode_completed"},
                     "purchase_completed": {"purchase_completed", "subscription_renewed", "subscription_lapsed"}}
            if what in types:   # re-check the claimed absence against the raw events
                return not any(e.type in types[what] for e in events)
            if what == "profile":
                return not pack.profiles.get(user)
            return what == "app_open_14d"
        return False

    # ---------------------------------------------------------------------------------------------- main entry
    def verify(self, prop, env):
        """prop: user, trigger, now, goal, evidence, dialect, page, messages.
        env: facts (rebuilt now; None if the store is unreadable), events, store, arm, handled_triggers, ledger_days."""
        failed, passed = [], []

        def rec(name, ok, detail=""):
            (passed if ok else failed).append(name if ok else {"check": name, "detail": detail})

        pack, cat, now = self.pack, self.pack.catalogue, prop["now"]
        user, trig = prop["user"], prop["trigger"]
        acting = bool(prop.get("page") or prop.get("messages"))
        facts = env.get("facts")

        # holdout: absolute
        rec("holdout", not (acting and env["arm"] == "holdout"), "holdout arm must receive zero actions")
        # duplicates
        rec("duplicate", trig.id not in env["handled_triggers"], "trigger already processed")
        # goal
        g = prop.get("goal")
        if g:
            rec("goal_measurable", g["goal_id"] not in self.rb.unmeasurable,
                self.rb.unmeasurable.get(g["goal_id"], ""))
            if facts is None:
                failed.append({"check": "eligibility", "detail": "viewer data unreadable: cannot verify eligibility"})
            else:
                e = evaluate(g["goal_id"], facts, pack, self.rb, self.s)
                rec("eligibility", e.eligible, "; ".join(e.reasons))
            rec("deadline", parse(g["deadline"]) > now, "goal deadline already passed")
        # evidence
        bad = [r for r in prop.get("evidence", []) if not self._ref_ok(r, user, env["events"], env["store"])]
        rec("evidence_refs", not bad, f"unresolvable or contradicted: {bad[:5]}")

        # dialect
        d = prop.get("dialect")
        td = trig.dialect
        ok_d = d is not None and VALID_DIALECT.match(str(d)) and d == td
        if acting:
            rec("dialect", bool(ok_d), f"dialect {d!r} must equal the app setting {td!r} and be valid")
        page, msgs = prop.get("page"), prop.get("messages", [])
        # titles used anywhere
        tids = []
        if page:
            tids += ([page["hero_title_id"]] if page["hero_title_id"] else []) + [t for r in page["rows"] for t in r["title_ids"]]
        tids += [m["title_id"] for m in msgs if m.get("title_id")]
        wrong_dialect = [t for t in tids if not cat.get(t) or cat.get(t).dialect != d]
        unavailable = [t for t in tids if not cat.get(t) or not cat.available(cat.get(t), now)]
        flagged = [t for t in tids if cat.get(t) and cat.get(t).flagged]
        if acting:
            rec("title_dialect", not wrong_dialect, f"titles outside dialect {d}: {wrong_dialect[:5]}")
            rec("availability", not unavailable, f"not available at {iso(now)}: {unavailable[:5]}")
            rec("injected_content", not flagged, f"titles whose record contains agent-directed instructions: {flagged}")

        # page structure
        if page:
            slugs, problems = [], []
            if page["hero_title_id"]:
                slugs.append(cat.get(page["hero_title_id"]).slug if cat.get(page["hero_title_id"]) else "?")
            if len(page["rows"]) > self.s.max_rows:
                problems.append("too many rows")
            for r in page["rows"]:
                if r["row_type"] not in ALLOWED_ROW_TYPES:
                    problems.append(f"row type {r['row_type']} not allowed")
                n = len(r["title_ids"])
                if not (self.s.min_titles_per_row <= n <= self.s.max_titles_per_row):
                    problems.append(f"row {r['row_type']} has {n} titles")
                slugs += [cat.get(t).slug if cat.get(t) else "?" for t in r["title_ids"]]
            if len(slugs) != len(set(slugs)):
                problems.append("content_slug repeated on page")
            if not page["rows"] and not page["hero_title_id"]:
                problems.append("empty page")
            rec("page_structure", not problems, "; ".join(problems))

        # messages
        planned_days = set()
        for m in msgs:
            ch = m["type"]
            if ch in ("push", "whatsapp"):
                if facts is None:
                    failed.append({"check": "consent", "detail": "viewer data unreadable: consent unverifiable"})
                else:
                    ok = facts.consent_push if ch == "push" else (facts.consent_wa and facts.language == "hin")
                    rec("consent", ok, f"{ch} consent/language not satisfied")
                sa = parse(m["send_at"])
                rec("quiet_hours", not in_quiet_hours(sa, self.s.quiet_start_h, self.s.quiet_end_h),
                    f"send_at {m['send_at']} falls in 22:00-08:00 IST")
                day = str(ist_day(sa))
                taken = {str(x) for x in env["ledger_days"]} | planned_days
                rec("frequency_cap", day not in taken, f"another push/WhatsApp already on IST day {day}")
                planned_days.add(day)
            elif ch == "in_app_message" and env.get("in_app_days") is not None:
                rec("frequency_cap", str(ist_day(now)) not in env["in_app_days"], "second in-app message today")
            if ch in ("push", "in_app_message"):
                self._copy(m["text"], rec)
                if ch == "push":
                    mm = DEEPLINK_RE.match(m.get("deeplink", ""))
                    rec("deeplink", bool(mm) and mm.group(1) == m["title_id"], "deeplink must be ott://title/<title_id>")
            if ch == "whatsapp":
                self._template(m, rec)
        return {"passed": sorted(set(passed)), "failed": failed, "degraded": facts is None}

    def _copy(self, text, rec):
        t, b = text.get("title", ""), text.get("body", "")
        rec("copy_length", len(t) <= 40 and len(b) <= 120, f"title {len(t)}/40, body {len(b)}/120")
        blob = (t + " " + b).lower()
        hit = [p for p in FORBIDDEN if p in blob]
        rec("copy_forbidden_phrase", not hit, f"forbidden: {hit}")
        money = [m.group(0) for m in MONEY_RE.finditer(blob)]
        nums = {int(re.sub(r"\D", "", x)) for x in money if re.sub(r"\D", "", x)}
        rec("copy_price_claim", not (nums - self.allowed_prices) and not OFFER_RE.search(blob),
            f"price/offer claim not in pricing.json: {money or OFFER_RE.findall(blob)}")

    def _template(self, m, rec):
        tpl = self.templates.get(m.get("template_id"))
        rec("template_approved", tpl is not None, f"template {m.get('template_id')} is not approved")
        if not tpl:
            return
        v = m.get("variables", {})
        rec("template_variables", set(v) == set(tpl["variables"]), f"variables {sorted(v)} != {tpl['variables']}")
        t = self.pack.catalogue.get(m.get("title_id"))
        ok = True
        if "title_name" in v:
            ok &= bool(t) and v["title_name"] == t.name
        if "dialect_name" in v:
            ok &= bool(t) and v["dialect_name"] == t.dialect
        if "plan_price" in v:
            ok &= v["plan_price"] in {f"₹{p['price_inr']}" for p in self.pack.pricing["plans"]}
        rec("template_values_from_catalogue_or_pricing", ok, "variable value not derived from catalogue/pricing")

    # ----------------------------------------------------------------------------------------- model claims
    def validate_model_claim(self, out, eligible_ids, facts):
        """The model may only pick among goals the rules found eligible. Returns (accepted_goal_id|None, problem|None).
        A claim of eligibility that no event/profile fact supports is recorded as an unsupported claim."""
        gid = out.get("goal_id")
        if gid is None and out.get("eligible") in (False, None):
            return None, None                      # legitimate abstention
        if not isinstance(gid, str) or gid not in self.rb.goals:
            return None, "model named no valid goal"
        if gid not in eligible_ids:
            why = evaluate(gid, facts, self.pack, self.rb, self.s).reasons[0] if facts else "viewer data unavailable"
            return None, f"unsupported claim: model says {gid} eligible, but {why}"
        return gid, None
