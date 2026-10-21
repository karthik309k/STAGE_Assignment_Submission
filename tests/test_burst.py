"""What happens when 1,000 viewers open the app in the same minute? Run the real replay harness on a synthetic burst."""
import json
from datetime import timedelta

from conftest import NOW, raw_event
from oua.config import Settings
from oua.replay import run_replay


def _burst(builder, n, stale_every=0):
    for i in range(n):
        u = f"B{i:04d}"
        builder.viewer(u)
        at = NOW + timedelta(milliseconds=(i * 60000) // n)          # n opens spread over one minute
        buf = 86400 if stale_every and i % stale_every == 0 else 0
        builder.triggers.append(raw_event(u, "app_open", at, buf=buf, deliver=True))


def test_thousand_opens_in_one_minute_all_decided_inside_budget(builder, tmp_path):
    _burst(builder, 1000)
    pack = builder.build()
    out = tmp_path / "run"
    s = run_replay(pack, Settings(), str(out), verbose=False)
    lat = s["latency"]
    assert lat["decisions"] == 1000
    assert lat["over_60s"] == 0 and lat["over_300s"] == 0
    assert lat["model_calls_max"] <= 8 and lat["tool_calls_max"] <= 20


def test_overload_sheds_optional_model_calls_before_dropping_anyone(builder, tmp_path):
    _burst(builder, 1000)
    pack = builder.build()
    s = run_replay(pack, Settings(), str(tmp_path / "run"), verbose=False)
    fb = s["latency"]["fallbacks"]
    assert fb.get("model_skipped_overload", 0) > 0          # optional work is shed first
    assert s["latency"]["shed"] == 0                         # nobody is dropped at this load


def test_stale_triggers_wait_behind_fresh_ones_under_load(builder, tmp_path):
    _burst(builder, 1000, stale_every=3)
    pack = builder.build()
    run_replay(pack, Settings(), str(tmp_path / "run"), verbose=False)
    dec = [json.loads(l) for l in open(tmp_path / "run" / "decisions.jsonl")]
    fresh = [d["trigger"]["queue_wait_s"] for d in dec if not d["trigger"]["buffered"]]
    stale = [d["trigger"]["queue_wait_s"] for d in dec if d["trigger"]["buffered"]]
    assert max(fresh) <= max(stale)
    assert all(d["status"] == "PUBLISHED" for d in dec if not d["trigger"]["buffered"])


def test_extreme_overload_sheds_and_records_it_instead_of_publishing_late(builder, tmp_path):
    _burst(builder, 1000)
    pack = builder.build()
    s = run_replay(pack, Settings(workers=1, mock_model_latency_ms=3000, store_read_ms=400), str(tmp_path / "run"), verbose=False)
    dec = [json.loads(l) for l in open(tmp_path / "run" / "decisions.jsonl")]
    shed = [d for d in dec if d["status"] == "SHED_OVERLOAD"]
    assert shed and all(d["no_action_reason"].startswith("load shed") for d in shed)
    pubs = [d for d in dec if d["status"] == "PUBLISHED"]
    assert all(d["decided_in_ms"] <= 60000 for d in pubs)
