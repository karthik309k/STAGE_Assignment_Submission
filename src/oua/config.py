"""All tunables in one place. Nothing here is specific to a replay day."""
from dataclasses import dataclass, asdict
import json


@dataclass
class Settings:
    # Budgets (brief: configurable max 8 model calls and 20 tool calls per decision)
    max_model_calls: int = 8
    max_tool_calls: int = 20
    # Latency
    latency_budget_s: float = 60.0         # target: publish within 60 s of trigger
    hard_fail_s: float = 300.0             # > 5 min counts as a failure
    soft_deadline_s: float = 20.0          # past this (virtual) elapsed time, skip optional work
    model_timeout_s: float = 2.0
    store_read_timeout_s: float = 1.5
    workers: int = 4
    shed_queue_wait_s: float = 5.0         # backlog longer than this -> skip optional model calls
    drop_after_wait_s: float = 45.0        # a trigger that cannot start by this point is shed (recorded, no action)
    # Simulated service times of dependencies in the mock/replay world (ms). Real compute is measured separately.
    mock_model_latency_ms: float = 300.0
    store_read_ms: float = 20.0
    store_write_ms: float = 10.0
    catalogue_ms: float = 5.0
    # Trigger freshness
    fresh_trigger_s: float = 120.0         # <= this: full decision (page + optional message)
    stale_trigger_s: float = 600.0         # > this: not a live session -> no action (matches how the app behaves)
    # Home page
    page_ttl_min: int = 120
    page_refresh_remaining_min: int = 30   # do not republish while a page for the same dialect still has >= this left
    max_rows: int = 6
    min_titles_per_row: int = 3
    max_titles_per_row: int = 10
    # Messaging
    message_min_delay_min: int = 120       # user is in the app now; nudge later, not immediately
    quiet_start_h: int = 22
    quiet_end_h: int = 8
    # Circuit breaker for the model provider
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 60.0
    # Goals
    new_user_days: int = 7
    absence_days: int = 14
    recent_lapse_days: int = 30            # interpretation of "recently lapsed" (G06)
    resume_gap_days: int = 3
    new_release_days: int = 30
    rule_version: str = "rules-v1"
    model_mode: str = "mock"               # mock | anthropic (optional, off by default)

    @classmethod
    def load(cls, path=None):
        s = cls()
        if path:
            with open(path) as f:
                for k, v in json.load(f).items():
                    if not hasattr(s, k):
                        raise KeyError(f"unknown setting {k}")
                    setattr(s, k, v)
        return s

    def to_dict(self):
        return asdict(self)
