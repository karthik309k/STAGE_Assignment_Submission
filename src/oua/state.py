"""Durable per-viewer state: one JSON file per viewer, written atomically after every decision.

The state is the agent's memory: active goal, goal history, every decision and action it took, the page currently live,
scheduled messages, idempotency ledgers (processed triggers / handled sessions), outcomes and what it learned."""
import json
import os
import tempfile
import time


def new_state(user, arm):
    return dict(
        viewer_id=user, arm=arm, active_goal=None, goal_history=[],
        processed_triggers={}, handled_sessions={}, decisions=[], actions=[],
        live_page=None, pending=[], message_ledger={}, facts_summary=None,
        replans=[], outcomes=[], learnings={"goal_failures": {}, "goal_successes": {}, "notes": []},
        flags=[],
    )


class StateStore:
    def __init__(self, directory=None):
        self.dir = directory
        self.cache = {}
        if directory:
            os.makedirs(directory, exist_ok=True)

    def path(self, user):
        return os.path.join(self.dir, f"{user}.json")

    def load(self, user, arm="agent"):
        if user in self.cache:
            return self.cache[user]
        st = None
        if self.dir and os.path.exists(self.path(user)):
            with open(self.path(user)) as f:
                st = json.load(f)
        if st is None:
            st = new_state(user, arm)
        self.cache[user] = st
        return st

    def save(self, user):
        if not self.dir:
            return
        st = self.cache[user]
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, indent=1, default=str)
        target = self.path(user)
        # Atomic rename (a crash never leaves a half-written state file). On Windows an editor file-watcher or antivirus can
        # hold a freshly created file for a few milliseconds and os.replace raises PermissionError: retry briefly, and as a
        # last resort write the target directly (not atomic, but never loses the state or crashes the replay).
        for attempt in range(10):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        with open(target, "w") as f:
            json.dump(st, f, indent=1, default=str)
        try:
            os.remove(tmp)
        except OSError:
            pass

    def save_all(self):
        for u in list(self.cache):
            self.save(u)
