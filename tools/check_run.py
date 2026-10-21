"""Independent audit of a finished run. Does NOT import the agent or verifier: it re-checks sample_run/actions.jsonl and
decisions.jsonl directly against the data pack, so a bug in the agent cannot hide itself.

    python tools/check_run.py --pack <pack dir> --run sample_run
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
P = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
FORBIDDEN = ["last chance", "hurry", "only today", "free forever", "guaranteed"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--run", default="sample_run")
    a = ap.parse_args()
    pj = lambda *x: os.path.join(a.pack, *x)
    arms = {r["user_id"]: r["arm"] for r in csv.DictReader(open(pj("replay", "arms.csv"), encoding="utf-8"))}
    cat = {r["title_id"]: r for r in csv.DictReader(open(pj("data", "catalogue.csv"), encoding="utf-8"))}
    templates = {t["template_id"] for t in json.load(open(pj("config", "interventions.json")))["whatsapp"]["approved_templates"]}
    controls = [json.loads(l) for l in open(pj("replay", "control_events.jsonl"), encoding="utf-8") if l.strip()]
    withdrawn = {c["title_id"]: P(c["at"]) for c in controls if c["type"] == "title_withdrawn"}
    slug_of = lambda t: cat[t]["content_slug"]
    withdrawn_slugs = {slug_of(t): at for t, at in withdrawn.items()}
    actions = [json.loads(l) for l in open(os.path.join(a.run, "actions.jsonl"), encoding="utf-8")]
    decisions = [json.loads(l) for l in open(os.path.join(a.run, "decisions.jsonl"), encoding="utf-8")]

    fails = defaultdict(list)
    def bad(check, msg):
        fails[check].append(msg)

    day_count = Counter()
    for x in actions:
        u, t = x["user_id"], x["type"]
        if arms.get(u) != "agent":
            bad("holdout_zero_actions", f"{x['action_id']} -> {u} ({arms.get(u)})")
        if t in ("push", "whatsapp"):
            sa = P(x["send_at"]).astimezone(IST)
            if sa.hour >= 22 or sa.hour < 8:
                bad("quiet_hours_22_08_IST", f"{x['action_id']} send_at {x['send_at']} = {sa:%H:%M} IST")
            day_count[(u, sa.date())] += 1
        if t == "push":
            tx = x["text"]
            if len(tx["title"]) > 40 or len(tx["body"]) > 120:
                bad("push_length", x["action_id"])
            if any(p in (tx["title"] + " " + tx["body"]).lower() for p in FORBIDDEN):
                bad("forbidden_phrase", x["action_id"])
            m = re.fullmatch(r"ott://title/(T\d{4})", x.get("deeplink", ""))
            if not m or m.group(1) != x["title_id"] or m.group(1) not in cat:
                bad("deeplink_format", x["action_id"])
            if cat[x["title_id"]]["dialect"] is None:
                bad("push_title_dialect", x["action_id"])
        if t == "whatsapp" and x["template_id"] not in templates:
            bad("whatsapp_template_approved", x["action_id"])
        if t == "home_page":
            pub = P(x["published_at"])
            ids = ([x["hero_title_id"]] if x["hero_title_id"] else []) + [i for r in x["rows"] for i in r["title_ids"]]
            slugs = [slug_of(i) for i in ids]
            if len(slugs) != len(set(slugs)):
                bad("page_unique_content_slug", x["action_id"])
            if len(x["rows"]) > 6:
                bad("page_max_rows", x["action_id"])
            for r in x["rows"]:
                if not 3 <= len(r["title_ids"]) <= 10:
                    bad("page_row_3_to_10_titles", f"{x['action_id']} {r['row_type']} has {len(r['title_ids'])}")
                if r["row_type"] not in {"next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"}:
                    bad("page_row_type_allowed", f"{x['action_id']} {r['row_type']}")
            for i in ids:
                c = cat[i]
                if c["dialect"] != x["dialect"]:
                    bad("page_titles_in_page_dialect", f"{x['action_id']} {i} is {c['dialect']}, page {x['dialect']}")
                if P(c["available_from"]) > pub or (c["available_until"] and P(c["available_until"]) <= pub):
                    bad("page_titles_available_at_publish", f"{x['action_id']} {i}")
                if slug_of(i) in withdrawn_slugs and pub >= withdrawn_slugs[slug_of(i)]:
                    bad("withdrawn_title_not_used_after_withdrawal", f"{x['action_id']} {i} published {x['published_at']}")
    for (u, d), n in day_count.items():
        if n > 1:
            bad("max_one_push_or_whatsapp_per_IST_day", f"{u} has {n} on {d}")

    slow = [d for d in decisions if d["status"] == "PUBLISHED" and d["decided_in_ms"] > 60000]
    for d in slow:
        bad("published_within_60s", d["decision_id"])
    for d in decisions:
        for k in ("goal", "action", "confidence", "confidence_reason", "evidence", "assumptions", "checks", "decided_in_ms"):
            if k not in d:
                bad("decision_has_required_fields", f"{d['decision_id']} missing {k}")
        if d["action"] == "NO_ACTION" and not d.get("no_action_reason"):
            bad("no_action_has_reason", d["decision_id"])
        if d["status"] == "PUBLISHED" and (not d["evidence"] or not d["goal"]):
            bad("published_has_goal_and_evidence", d["decision_id"])

    checks = ["holdout_zero_actions", "quiet_hours_22_08_IST", "max_one_push_or_whatsapp_per_IST_day", "push_length", "forbidden_phrase",
              "deeplink_format", "whatsapp_template_approved", "page_unique_content_slug", "page_max_rows", "page_row_3_to_10_titles",
              "page_row_type_allowed", "page_titles_in_page_dialect", "page_titles_available_at_publish",
              "withdrawn_title_not_used_after_withdrawal", "published_within_60s", "decision_has_required_fields",
              "no_action_has_reason", "published_has_goal_and_evidence"]
    print(f"actions checked: {len(actions)} ({dict(Counter(x['type'] for x in actions))}); decisions: {len(decisions)}; "
          f"withdrawals in replay: {len(withdrawn)}")
    for c in checks:
        print(f"  {'FAIL' if fails[c] else 'PASS'}  {c}" + (f"  ({len(fails[c])}) e.g. {fails[c][0]}" if fails[c] else ""))
    sys.exit(1 if any(fails.values()) else 0)


if __name__ == "__main__":
    main()
