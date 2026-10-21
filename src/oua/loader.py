"""Pack loader: parses, deduplicates and indexes the data pack. Nothing here decides anything about a viewer."""
import csv
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .timeutil import parse, ist_day

VALID_DIALECT = re.compile(r"^d[1-6]$")
# Patterns that mark text aimed at the agent rather than at viewers. Such text is logged and never acted upon.
INJECTION_RE = re.compile(
    r"(system note|assistant instruction|for ai agents|ignore (all |your )?(previous|prior)|"
    r"message (every|all) (user|viewer)|send .{0,40}(to every|to all)|mark every user|skip all (checks|rules))",
    re.I)
BUFFER_THRESHOLD_S = 120  # sent_at - occurred_at above this = offline-buffered; occurrence time is re-estimated


@dataclass(slots=True)
class Ev:
    id: str
    user: str
    type: str
    rt: object            # received_at (server clock, trusted)
    occ: object           # occurred_at (phone clock, untrusted)
    sent: object          # sent_at (phone clock, untrusted)
    buf_s: float          # sent - occurred: pure buffering delay, immune to a constant clock offset
    t_est: object         # best estimate of when it really happened
    props: dict
    dialect: object
    lang: object
    session: object
    deliver_at: object = None

    @property
    def ref(self):
        return "event:" + self.id


def make_ev(raw):
    rt, occ, sent = parse(raw["received_at"]), parse(raw["occurred_at"]), parse(raw["sent_at"])
    buf = max(0.0, (sent - occ).total_seconds())
    t_est = rt - timedelta(seconds=buf) if buf > BUFFER_THRESHOLD_S else rt
    return Ev(raw["event_id"], raw["user_id"], raw["event"], rt, occ, sent, buf, t_est,
              raw.get("properties") or {}, raw.get("app_dialect"), raw.get("app_language"),
              raw.get("session_id"), parse(raw.get("deliver_at")) if raw.get("deliver_at") else None)


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


class EventStore:
    """Per-user event lists ordered by received_at, deduplicated by event_id (first delivery wins)."""

    def __init__(self):
        self.by_user = defaultdict(list)
        self.seen = set()
        self.duplicates = 0

    def ingest(self, ev):
        if ev.id in self.seen:
            self.duplicates += 1
            return False
        self.seen.add(ev.id)
        lst = self.by_user[ev.user]
        lst.append(ev)
        if len(lst) > 1 and lst[-2].rt > ev.rt:
            lst.sort(key=lambda e: e.rt)
        return True

    def events(self, user, upto=None):
        lst = self.by_user.get(user, [])
        if upto is None:
            return list(lst)
        return [e for e in lst if e.rt <= upto]


@dataclass
class Title:
    id: str
    slug: str
    name: str
    dialect: str
    language: str
    ctype: str
    genres: tuple
    release: object
    avail_from: object
    avail_until: object
    premium: bool
    flagged: bool = False   # description/name contained instruction-like text (ignored, logged)


class Catalogue:
    def __init__(self, rows):
        self.titles = {}
        self.by_slug = defaultdict(list)
        self.flagged = []
        self.withdrawn = {}   # slug -> withdrawal time (conservative: licence covers every language variant)
        for r in rows:
            flagged = bool(INJECTION_RE.search(r["description"] or "") or INJECTION_RE.search(r["title"] or ""))
            t = Title(r["title_id"], r["content_slug"], r["title"], r["dialect"], r["language"], r["content_type"],
                      tuple(g for g in r["genres"].split("|") if g), parse(r["release_date"] + "T00:00:00Z"),
                      parse(r["available_from"]), parse(r["available_until"]) if r["available_until"] else None,
                      r["is_premium"] == "true", flagged)
            self.titles[t.id] = t
            self.by_slug[t.slug].append(t)
            if flagged:
                self.flagged.append(t.id)

    def get(self, title_id):
        return self.titles.get(title_id)

    def withdraw(self, title_id, at):
        t = self.titles.get(title_id)
        if t:
            self.withdrawn[t.slug] = at
        return t

    def is_withdrawn(self, t, at):
        w = self.withdrawn.get(t.slug)
        return w is not None and w <= at

    def available(self, t, at):
        return (t is not None and t.avail_from <= at and (t.avail_until is None or at < t.avail_until)
                and not self.is_withdrawn(t, at))

    def pick_variant(self, slug, dialect, language, at):
        """One title_id per content_slug: in `dialect`, available at `at`, preferring `language`."""
        cands = [t for t in self.by_slug.get(slug, []) if t.dialect == dialect and self.available(t, at)]
        if not cands:
            return None
        cands.sort(key=lambda t: (t.language != language, t.id))
        return cands[0]


class Pack:
    def __init__(self, root):
        self.root = root
        p = lambda *a: os.path.join(root, *a)
        self.issues = Counter()
        with open(p("config", "goals.json")) as f:
            self.goals_cfg = json.load(f)
        with open(p("config", "interventions.json")) as f:
            self.interventions = json.load(f)
        with open(p("config", "pricing.json")) as f:
            self.pricing = json.load(f)
        self.plans = {pl["plan_id"]: pl for pl in self.pricing["plans"]}
        with open(p("data", "catalogue.csv"), encoding="utf-8") as f:
            self.catalogue = Catalogue(list(csv.DictReader(f)))
        self.episodes = defaultdict(list)
        with open(p("data", "episodes.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.episodes[r["content_slug"]].append((int(r["season"]), int(r["episode"]), r["episode_id"], int(r["minutes"])))
        for v in self.episodes.values():
            v.sort()
        self.profiles = defaultdict(list)
        with open(p("data", "profiles_snapshot.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.profiles[r["user_id"]].append(dict(
                    user=r["user_id"], signup_at=parse(r["signup_at"]), status=r["subscription_status"],
                    plan_started_at=parse(r["plan_started_at"]), plan_ends_at=parse(r["plan_ends_at"]),
                    dialect=r["preferred_dialect"], language=r["preferred_language"],
                    push=r["notifications_opt_in"] == "true", wa=r["whatsapp_opt_in"] == "true",
                    updated_at=parse(r["updated_at"]), snapshot_at=parse(r["snapshot_at"])))
        self.arms = {}
        with open(p("replay", "arms.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.arms[r["user_id"]] = r["arm"]
        # history
        self.store = EventStore()
        self.type_counts = Counter()
        for raw in read_jsonl(p("data", "events.jsonl")):
            ev = make_ev(raw)
            self.type_counts[ev.type] += 1
            if not self.store.ingest(ev):
                self.issues["history_duplicate_event_lines"] += 1
            if ev.buf_s > BUFFER_THRESHOLD_S:
                self.issues["history_offline_buffered_events"] += 1
            if ev.dialect is not None and not VALID_DIALECT.match(str(ev.dialect)):
                self.issues["history_invalid_app_dialect"] += 1
        self.history_start = min(e.rt for lst in self.store.by_user.values() for e in lst)
        self.history_end = max(e.rt for lst in self.store.by_user.values() for e in lst)
        # replay inputs
        self.triggers = [make_ev(r) for r in read_jsonl(p("replay", "triggers.jsonl"))]
        self.live = [make_ev(r) for r in read_jsonl(p("replay", "live_events.jsonl"))]
        self.controls = list(read_jsonl(p("replay", "control_events.jsonl")))
        self.faults = {f["trigger_event_id"]: f for f in read_jsonl(p("replay", "model_faults.jsonl"))}
        self.known_event_types = set(self.type_counts) | {e.type for e in self.live} | {e.type for e in self.triggers}
        # replay day (IST) = IST date of the median trigger; avoids hard-coding a date
        if self.triggers:
            days = sorted(ist_day(t.rt) for t in self.triggers)
            self.replay_day = days[len(days) // 2]
        else:
            self.replay_day = None
        self._popularity = None
        self._audit_static()

    # ------------------------------------------------------------------ popularity
    def popularity(self, dialect):
        if self._popularity is None:
            cut = self.history_end - timedelta(days=14)
            pop = defaultdict(Counter)
            for lst in self.store.by_user.values():
                for e in lst:
                    if e.type == "play_started" and e.rt >= cut:
                        t = self.catalogue.get(e.props.get("title_id"))
                        if t:
                            pop[t.dialect][t.slug] += 1
            self._popularity = pop
        return self._popularity.get(dialect, Counter())

    def slugs_in_dialect(self, dialect):
        if not hasattr(self, "_slugs_by_dialect"):
            m = defaultdict(set)
            for t in self.catalogue.titles.values():
                m[t.dialect].add(t.slug)
            self._slugs_by_dialect = {k: sorted(v) for k, v in m.items()}
        return self._slugs_by_dialect.get(dialect, [])

    # ------------------------------------------------------------------ audit of static data issues
    def _audit_static(self):
        c = self.catalogue
        self.issues["catalogue_titles"] = len(c.titles)
        self.issues["catalogue_slugs_with_language_variants"] = sum(1 for v in c.by_slug.values() if len(v) > 1)
        self.issues["catalogue_titles_with_injected_instructions"] = len(c.flagged)
        self.issues["profile_rows"] = sum(len(v) for v in self.profiles.values())
        self.issues["profile_users_with_two_rows"] = sum(1 for v in self.profiles.values() if len(v) > 1)
        self.issues["triggers_total_lines"] = len(self.triggers)
        ids = Counter(t.id for t in self.triggers)
        self.issues["trigger_duplicate_lines"] = sum(v - 1 for v in ids.values() if v > 1)
        self.issues["trigger_offline_buffered_over_600s"] = sum(1 for t in self.triggers if t.buf_s > 600)
        self.issues["trigger_invalid_app_dialect"] = sum(
            1 for t in self.triggers if t.dialect is None or not VALID_DIALECT.match(str(t.dialect)))
        trig_users = {t.user for t in self.triggers}
        self.issues["trigger_users"] = len(trig_users)
        self.issues["trigger_users_without_profile_row"] = sum(1 for u in trig_users if u not in self.profiles)
        self.issues["deeplinks_with_invalid_dialect"] = sum(
            1 for t in self.triggers if t.props.get("deeplink") and not re.search(r"/d[1-6]/", t.props["deeplink"]))
