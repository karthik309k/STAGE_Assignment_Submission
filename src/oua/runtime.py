"""Runtime plumbing shared by agents: per-decision budgets, fault-aware data access, and the World (shared services)."""
import hashlib
from collections import defaultdict
from datetime import timedelta

from .goals import RuleBook
from .model import CircuitBreaker, Dependencies, MockModel, AnthropicModel
from .state import StateStore
from .verifier import Verifier


def h01(*parts):
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class BudgetExceeded(Exception):
    pass


class StoreTimeout(Exception):
    pass


class Budget:
    """Counts model/tool calls and accumulates SIMULATED dependency latency (ms). Real compute is timed separately."""

    def __init__(self, settings):
        self.s = settings
        self.model_calls = 0
        self.tool_calls = 0
        self.sim_ms = 0.0
        self.tool_log = []

    def tool(self, name, ms):
        if self.tool_calls >= self.s.max_tool_calls:
            raise BudgetExceeded(f"tool budget {self.s.max_tool_calls} exhausted at {name}")
        self.tool_calls += 1
        self.tool_log.append(name)
        self.sim_ms += ms

    def model(self, ms):
        if self.model_calls >= self.s.max_model_calls:
            raise BudgetExceeded(f"model budget {self.s.max_model_calls} exhausted")
        self.model_calls += 1
        self.sim_ms += ms

    def spend(self, ms):
        self.sim_ms += ms


class World:
    def __init__(self, pack, settings, state_dir=None, model=None):
        self.pack, self.s = pack, settings
        self.rb = RuleBook(pack)
        self.verifier = Verifier(pack, settings, self.rb)
        self.deps = Dependencies()
        self.states = StateStore(state_dir)
        self.agents = {}
        self.cached_facts = {}                      # last good facts per viewer (used when the data store is down)
        self.breaker = CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown_s)
        if model is not None:
            self.model = model
        elif settings.model_mode == "anthropic":
            self.model = AnthropicModel(settings)
        else:
            self.model = MockModel(pack.faults, self.deps, settings)
        self.decision_seq = 0
        self.action_seq = 0
        self.open_goals = defaultdict(set)          # goal_id -> viewers with an open goal (so a rule fix touches only these)
        self.live_pages = defaultdict(set)          # content_slug -> viewers whose live page contains it
        self.pending_by_slug = defaultdict(set)     # content_slug -> viewers with a scheduled message about it
        self.decisions = []
        self.actions = []                           # every action ever created (status tracked on the record)
        self.replans = []
        self.logs = []                              # operational log (injected instructions, outages, ...)

    def next_decision_id(self):
        self.decision_seq += 1
        return f"D-{self.decision_seq:05d}"

    def next_action_id(self):
        self.action_seq += 1
        return f"A-{self.action_seq:05d}"

    def read_viewer_data(self, user, trig, at, budget, salt="plan"):
        """The 'user data store' tool. Honours dependency_degraded windows: 20% of reads time out, others are slow."""
        s = self.s
        if self.deps.state("user_data_store", at) == "degraded":
            u = h01("store", trig.id, salt)
            if u < 0.20:
                budget.tool("viewer_data.read(timeout)", s.store_read_timeout_s * 1000)
                raise StoreTimeout(f"read timed out after {s.store_read_timeout_s}s")
            budget.tool("viewer_data.read(slow)", 200 + 1000 * h01("lat", trig.id, salt))
        else:
            budget.tool("viewer_data.read", s.store_read_ms)
        return self.pack.store.events(user, upto=at)
