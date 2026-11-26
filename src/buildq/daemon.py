# SPDX-License-Identifier: MIT
"""buildqd daemon core, ticket lifecycle, Unix socket server, and CLI entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import collections
import fcntl
import glob
import json
import os
import queue
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
import types
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout

from . import commands, common, config, machine, metrics, supervisor
from .commands import GIT_ENV
from .common import DEVNULL, FINISHED, PENDING, UID, VERSION
from .config import Config, Lane
from .github import GitHubMixin
from .history import History
from .machine import Machine, T3_THREAD_VAR, user_manager_cgroup
from .metrics import Metrics, WAIT_COLUMN, WAIT_COLUMNS
from .runners import CgroupRunner, DirectRunner, Jobserver, SystemdRunner
from .scheduler import SchedulerMixin
from .ticket import Conn, Ticket

class RunnerUnavailableError(RuntimeError):
    """Raised when an explicitly requested runner (e.g. runner = 'cgroup') cannot start."""


class Daemon(GitHubMixin, SchedulerMixin):
    VACATED_SECONDS = 600.0  # how long a cancelled `run` build's place is kept for its retry

    def __init__(self, config_path: str, state_dir: str, machine: Machine | None = None, runner=None,
                 queries=None):
        self.config_path = config_path
        # systemctl queries, which take seconds when the user manager is slow, run here, off the
        # event loop: a stalled systemd mustn't stall every client (#2). See query().
        self.queries = queries or ThreadPoolExecutor(max_workers=4, thread_name_prefix="buildq-query")
        self.pending_queries: dict[tuple, Future] = {}
        self.storage_unreadable: set[str] = set()  # container storage said to be unreadable, once each
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.last_good = os.path.join(state_dir, "config.last-good.toml")
        self.machine = machine or Machine()
        self.tickets_dir = os.path.join(state_dir, "tickets")
        os.makedirs(self.tickets_dir, exist_ok=True)
        self.cg_base = user_manager_cgroup()
        self.override = runner  # tests pass a runner of their own
        self.runners = {"systemd": SystemdRunner(self.cg_base), "direct": DirectRunner()}
        if runner is not None:
            self.runners[runner.name] = runner
        self._systemd_ok = None
        self._cgroup_probed: tuple[str, str | None] | None = None
        self.cfg = None
        self.cfg = self.load_config()
        self.runner = self.runner_for_config(self.cfg)
        self.history = History(os.path.join(state_dir, "history.jsonl"), self.cfg.history_keys)
        self.metrics = Metrics(os.path.join(state_dir, "metrics.db"))
        self.tickets: dict[str, Ticket] = {}
        self.watchers: dict[str, set[Conn]] = collections.defaultdict(set)
        self.helpers: list[tuple[subprocess.Popen, float]] = []
        self.budget: dict = {}
        self.stopping = False
        self.last_prune = -1e9
        self.last_rebalance = -1e9
        # The place in the queue of a `buildq run` build cancelled because its client went (a tool
        # timeout), by (directory, command, lane, caller): (seconds it waited, kept until). The retry
        # takes it (#7). Kept in memory only: a daemon restart forgets them (the retry goes to the back).
        self.vacated: dict[tuple, tuple[float, float]] = {}
        self.thread_scan: tuple[float, dict | None] | None = None  # (when, live_threads()) for drop_abandoned
        self.guard_available: int | None = None  # MemAvailable when the slice was last guarded
        self.submissions: dict[str, str] = {}  # the client's submission id -> its ticket
        self.slice_limit = None  # the slice's configured MemoryMax, read once systemd has the slice
        self.guard_failed = False
        self.cg_root = "/sys/fs/cgroup"
        self.memory_tight = False
        self.memory_calm_since = None
        self.last_memory_log = 0.0
        self.memory_lines: queue.Queue = queue.Queue()
        self.walk_wanted = threading.Event()
        self.walking = False
        self.memory_stopping = False
        self.memory_threads: dict[str, threading.Thread] = {}
        self.memory_thread_lock = threading.Lock()
        self.memory_thread_failed = False
        self.last_freeze = self.last_kill = -1e9
        self.room_made: dict[str, float] = {}  # a CI job's build -> when builds were last stopped for it
        self.partly_made: set[str] = set()  # CI jobs' builds that builds were stopped for though not enough
        self.started_at = time.time()
        self.avail_seen: collections.deque = collections.deque()  # (time, memory available) of the last 10 s
        self.submit_outcome: dict = {}  # how the last submission was answered: joined, reused, passed before
        self.paused = common.read_json(os.path.join(state_dir, "paused.json"))  # {"reason", "since"} or None
        if not isinstance(self.paused, dict):
            self.paused = None
        self.status_posts: list = []
        self.status_procs: list[tuple[subprocess.Popen, dict, float]] = []
        self.gh: list | None = None  # None: found as each status is sent (tests put a stand-in here)
        self.gh_unrunnable = False  # said so in the log already, until gh runs again
        self.source_command = [*common.client_command(), "source"]
        self.source_checks: list[tuple[subprocess.Popen, str, str, float, int]] = []
        self.source_wanted: collections.deque = collections.deque()  # (ticket, phase, attempt) waiting a turn
        self.status_posted: dict[str, dict] = {}  # "repo@sha@context" -> {state, order} last posted
        self.status_seen: dict[str, list] = {}  # "repo@sha@context" -> order of the newest verdict seen
        self.status_expired: list[dict] = []  # recently expired missing-commit posts (revivable after push)
        self.status_stats: dict = {"delivered": 0, "expired": 0, "failed": 0, "last_error": None, "last_posted": None}
        self.tick_ms: collections.deque = collections.deque(maxlen=300)
        self.tick_max_ms: float = 0.0
        self.tick_last_slow: dict | None = None
        statuses = common.read_json(os.path.join(state_dir, "github-statuses.json"))
        if isinstance(statuses, list):
            self.status_posts = statuses
        elif isinstance(statuses, dict):
            queued = statuses.get("queue")
            self.status_posts = queued if isinstance(queued, list) else []
            seen = statuses.get("seen")
            self.status_seen = {str(k): list(v) for k, v in seen.items() if isinstance(v, list)} \
                if isinstance(seen, dict) else {}
            posted = statuses.get("posted")
            self.status_posted = {}
            for where, value in (posted.items() if isinstance(posted, dict) else ()):
                if isinstance(value, str):
                    value = {"state": value, "order": [0, ""]}
                if isinstance(value, dict) and isinstance(value.get("state"), str):
                    self.status_posted[str(where)] = {"state": value["state"], "order": list(value.get("order") or [0, ""])}
            expired = statuses.get("expired")
            if isinstance(expired, list):
                self.status_expired = [p for p in expired if commands.valid_post(p)][-100:]
            stats = statuses.get("stats")
            if isinstance(stats, dict):
                for key in ("delivered", "expired", "failed"):
                    val = common.sane_count(stats.get(key))
                    if val is not None:
                        self.status_stats[key] = int(val)
                if isinstance(stats.get("last_error"), str):
                    self.status_stats["last_error"] = stats["last_error"][:300]
                if isinstance(stats.get("last_posted"), (int, float)) and not isinstance(stats.get("last_posted"), bool):
                    self.status_stats["last_posted"] = stats["last_posted"]
        newest: dict[str, dict] = {}
        for post in (p for p in self.status_posts if commands.valid_post(p)):  # what was in flight is just queued now
            where = post.get("where") or f"{post['repo']}@{post['sha']}@{post['context']}"
            post["where"] = where
            post["order"] = list(post.get("order") or [post["first"], ""])
            if where not in newest or post["order"] > newest[where]["order"]:
                newest[where] = post
        self.status_posts = list(newest.values())

    # ---- configuration

    def attempt(self, t: Ticket) -> tuple:
        """A ticket's attempt: queries are keyed by it, so an answer never outlives its attempt."""
        return (t.id, t.requeues, t.unit)

    @staticmethod
    def snapshot(t: Ticket):
        """What a runner's query reads from a ticket, copied: the loop goes on changing the ticket
        while the query runs on another thread."""
        return types.SimpleNamespace(id=t.id, unit=t.unit, cg=t.cg, dir=t.dir)

    def drop_queries(self, t: Ticket) -> None:
        """Forget a ticket's queries (its attempt has ended): their answers belong to it alone."""
        for key in [k for k in self.pending_queries if len(k) > 1 and isinstance(k[1], tuple) and k[1][0] == t.id]:
            self.pending_queries.pop(key).cancel()

    def unit_state_of(self, t: Ticket):
        """The runner's view of a ticket's service: through query() for systemd (a systemctl call that
        can be slow); the direct runner's is a file read, done here on the ticket itself."""
        runner = self.runner_of(t)
        if runner.name != "systemd":
            return runner.unit_state(t)
        return self.query(("unit-state", self.attempt(t)), runner.unit_state, self.snapshot(t))

    def result_of(self, t: Ticket):
        """systemd's verdict on a build's service (through query()), or the runner's own."""
        runner = self.runner_of(t)
        if runner.name != "systemd":
            return runner.result(t)
        return self.query(("result", self.attempt(t)), runner.result, self.snapshot(t))

    def query(self, key: tuple, fn, *args):
        """fn(*args), run off the event loop: its answer, or PENDING while it isn't in yet, so the
        caller asks again on a later tick. A quick answer (a healthy systemctl takes milliseconds)
        is waited for, up to 50 ms, so it is used at once."""
        future = self.pending_queries.get(key)
        if future is None:
            future = self.pending_queries[key] = self.queries.submit(fn, *args)
            wait = 0.05  # a quick answer is used at once
        else:
            wait = 0.0  # asked before: look, don't wait again (a stalled systemd mustn't cost every tick)
        try:
            answer = future.result(timeout=wait)
        except FutureTimeout:
            return PENDING
        finally:
            if future.done():
                self.pending_queries.pop(key, None)
        return answer

    def _cgroup_runner(self, configured_root: str) -> CgroupRunner | None:
        key = os.environ.get("BUILDQ_CGROUP_ROOT", "").strip() or configured_root
        if self._cgroup_probed is None or self._cgroup_probed[0] != key:
            self._cgroup_probed = (key, CgroupRunner.discover(configured_root))
        root = self._cgroup_probed[1]
        if not root:
            return None
        existing = self.runners.get("cgroup")
        if isinstance(existing, CgroupRunner) and existing.root == root and existing._ready:
            return existing
        runner = CgroupRunner(root, self.machine.mem_total, state_dir=self.state_dir)
        if not runner._ready:
            self._cgroup_probed = (key, None)
            return None
        self.runners["cgroup"] = runner
        return runner

    def runner_for_config(self, cfg: Config):
        """The runner new builds use. A build already running keeps the runner it started under."""
        if self.override is not None:
            return self.override
        wanted = os.environ.get("BUILDQ_RUNNER") or cfg.runner
        if wanted not in ("auto", "systemd", "cgroup", "direct"):
            common.log(f"BUILDQ_RUNNER={wanted!r} isn't auto, systemd, cgroup or direct; using {cfg.runner}")
            wanted = cfg.runner
        if os.environ.get("INVOCATION_ID") and wanted != "systemd":
            # Running as a systemd service: the direct runner would put every build inside the
            # daemon's own small cgroup. A slow probe under load mustn't pick it.
            if wanted == "cgroup":
                raise RunnerUnavailableError(
                    "runner = \"cgroup\" can't be used while buildqd runs as a systemd service "
                    "(use runner = \"systemd\" or \"auto\")"
                )
            if wanted != "auto":
                common.log(f"runner = {wanted!r} can't be used while buildqd runs as a systemd service; using systemd")
            wanted = "systemd"
        explicit_cg_root = bool(os.environ.get("BUILDQ_CGROUP_ROOT", "").strip() or cfg.cgroup_root.strip())
        if wanted == "auto":
            if self._systemd_ok is None:
                self._systemd_ok = SystemdRunner.available()
            if self._systemd_ok:
                wanted = "systemd"
            elif self._cgroup_runner(cfg.cgroup_root) is not None:
                wanted = "cgroup"
            else:
                if explicit_cg_root:
                    common.log(f"cgroup_root {os.environ.get('BUILDQ_CGROUP_ROOT') or cfg.cgroup_root!r} "
                               "isn't usable for cgroup runner; falling back to direct runner", level=4)
                wanted = "direct"
        if wanted == "cgroup":
            cg_runner = self._cgroup_runner(cfg.cgroup_root)
            if cg_runner is not None:
                return cg_runner
            raise RunnerUnavailableError(
                "runner = \"cgroup\" needs a writable delegated cgroup v2 directory with cpu and memory "
                "controllers, and buildqd must run inside that delegated subtree (run init/cgroup-setup.sh "
                "and start buildqd via an init service, set cgroup_root, or use runner = \"auto\")"
            )
        return self.runners[wanted]

    def runner_of(self, t: "Ticket"):
        found = self.runners.get(t.runner)
        if found is not None:
            return found
        # Backend-preserving recovery: if a restarted daemon switched to `direct` while a `cgroup`
        # build is still active or pending cleanup, return a read/control handle for its cgroup.
        if t.runner == "cgroup" and t.cg and t.cg.startswith("/sys/fs/cgroup/"):
            handle = CgroupRunner.__new__(CgroupRunner)
            handle.root = os.path.dirname(os.path.dirname(t.cg))
            handle.builds_dir = os.path.dirname(t.cg)
            handle.mem_total = self.machine.mem_total
            handle.state_dir = self.state_dir
            handle.initial_memory_max = None
            handle._ready = False
            return handle
        return self.runner

    def load_config(self) -> Config:
        """The configuration file if it loads and the runner accepts its lanes; else the last one
        that did; else the defaults. An explicit unavailable runner fails startup cleanly rather
        than silently dropping back to another backend."""
        m = self.machine
        try:
            cfg = config.load_config_file(self.config_path, m.mem_total, m.ncpu)
            runner = self.runner_for_config(cfg)
            for lane in cfg.lanes.values():
                problem = runner.check_lane(lane)
                if problem:
                    raise ValueError(f"{runner.name} refuses lanes.{lane.name}.properties: {problem}")
            if os.path.exists(self.config_path):
                shutil.copyfile(self.config_path, self.last_good)
            return cfg
        except RunnerUnavailableError:
            if self.cfg is None:
                raise  # fail startup explicitly rather than silently switching runners
            problem = f"config {self.config_path}: requested runner is unavailable"
        except Exception as e:
            if self.cfg is None and os.environ.get("BUILDQ_RUNNER") == "cgroup":
                raise RunnerUnavailableError(f"config {self.config_path}: {e}") from e
            problem = f"config {self.config_path}: {e}"
        if self.cfg is not None:
            common.log(f"{problem}; keeping the configuration in force")
            return self.cfg
        try:
            cfg = config.load_config_file(self.last_good, m.mem_total, m.ncpu)
            common.log(f"{problem}; using the last configuration that loaded ({self.last_good})")
            return cfg
        except Exception:
            common.log(f"{problem}; no earlier configuration loaded, so using the defaults")
            return Config({}, m.mem_total, m.ncpu)

    async def reload_async(self) -> None:
        """Reload without blocking the loop: checking lane properties starts processes."""
        cfg = await asyncio.get_running_loop().run_in_executor(None, self.load_config)
        self.apply(cfg)

    def reload(self) -> None:
        self.apply(self.load_config())

    def apply(self, cfg: "Config") -> None:
        self.cfg = cfg
        self.slice_limit = None  # read it again: the slice may have changed
        for key in [k for k in self.pending_queries if k[0] == "slice-max"]:
            self.pending_queries.pop(key).cancel()  # (and not from an answer asked for before)
        self.runner = self.runner_for_config(self.cfg)
        self.history.max_keys = self.cfg.history_keys
        common.log(f"configuration in force: {json.dumps(self.cfg.summary())}")
        self.schedule(time.time())

    def lane(self, name: str) -> Lane:
        return self.cfg.lanes.get(name) or self.cfg.lanes[self.cfg.default_lane]

    def active(self) -> list[Ticket]:
        """Tickets that hold a turn: starting or running, including builds found in flock slots."""
        return [t for t in self.tickets.values() if t.state in ("starting", "running")]

    def resolve(self, ref) -> tuple[Ticket | None, str | None]:
        ref = str(ref or "").strip().lower()
        if ref in self.tickets:
            return self.tickets[ref], None
        if len(ref) < 4:
            return None, "give at least the first 4 characters of the ticket"
        matches = [t for tid, t in self.tickets.items() if tid.startswith(ref)]
        if len(matches) == 1:
            return matches[0], None
        if matches:
            return None, f"{ref} matches {len(matches)} tickets; give more of it"
        return None, f"no ticket {ref}"

    def position(self, t: Ticket) -> int | None:
        return self.queue().index(t) if t.state == "queued" else None

    def save(self, t: Ticket) -> bool:
        """Write the ticket to disk; False if that failed (a full disk, say), which is logged."""
        if not t.dir:
            return True
        try:
            common.write_json(os.path.join(t.dir, "ticket.json"), t.record())
            return True
        except OSError as e:
            common.log(f"can't save ticket {t.id}: {e}")
            return False

    def broadcast(self, t: Ticket, now: float) -> None:
        conns = self.watchers.get(t.id)
        if not conns:
            return
        message = {"type": "ticket", **t.row(now, self.position(t))}
        for conn in list(conns):
            if not conn.send(message):
                conns.discard(conn)
        if t.state in FINISHED:
            self.watchers.pop(t.id, None)

    # ---- submitting, cancelling

    def submit(self, msg: dict, now: float, peer: int | None = None) -> tuple[Ticket | None, str | None]:
        if self.stopping:
            return None, "the daemon is stopping; try again in a moment"
        argv = msg.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            return None, "no command given"
        lane_name = str(msg.get("lane") or self.cfg.lane_for(argv))
        if lane_name not in self.cfg.lanes:
            return None, f"unknown lane {lane_name!r}; the lanes are {', '.join(sorted(self.cfg.lanes))}"
        lane = self.cfg.lanes[lane_name]
        cwd = msg.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None, "the working directory must be an absolute path"
        env = msg.get("env")
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            return None, "the environment must be a map of strings"
        self.submit_outcome = {}
        submission = msg.get("submission")
        if isinstance(submission, str) and submission in self.submissions:
            earlier = self.tickets.get(self.submissions[submission])
            if earlier is not None:  # the client didn't get our answer and asked again: the same answer
                how = earlier.answered.get(submission)
                self.submit_outcome = {how: True} if how in ("joined", "reused") else {}
                return earlier, None
        tid = str(uuid.uuid4())
        t = Ticket(tid, argv, cwd, lane_name,
                   commands.command_key(argv, cwd, lane_name, self.cfg.script_labels, env=env, key_env=self.cfg.key_env),
                   now, os.path.join(self.tickets_dir, tid))
        try:
            t.priority = common.number(msg.get("priority") or 0, "priority", -1000, 1000, integer=True)
            if msg.get("timeout") is not None:
                t.timeout = common.number(float(msg["timeout"]), "timeout", 1, 30 * 86400)
            else:
                t.timeout = lane.timeout
            if msg.get("cores") is not None:
                t.cores_given = common.number(float(msg["cores"]), "cores", 1, self.machine.ncpu)
            if msg.get("mem") is not None:
                t.mem_given = common.parse_size(msg["mem"], self.machine.mem_total)
        except (TypeError, ValueError, OverflowError) as e:
            return None, f"bad option: {e}"
        t.attached = bool(msg.get("attach"))
        t.restartable = msg.get("restartable") is True
        agent = None  # the agent session that dropped it off (#71)
        above = machine.agents_above(peer) if peer and not t.attached else []
        if above:
            thread = env.get(T3_THREAD_VAR, "").strip()[:200]
            agent = {"pids": above, "thread": thread or None}
        umask = msg.get("umask")
        if isinstance(umask, int) and not isinstance(umask, bool) and 0 <= umask <= 0o777:
            t.umask = umask
        limits = msg.get("rlimits")
        if isinstance(limits, dict):
            t.rlimits = {str(k): v for k, v in limits.items() if k in ("NOFILE", "STACK", "CORE") and isinstance(v, int)}
        name, notify = msg.get("name"), msg.get("notify")
        t.name = str(name)[:200] if name else None
        t.notify = str(notify) if notify else None
        t.caller = commands.sane_caller(msg.get("caller"))
        t.from_ci = bool(env.get("GITHUB_ACTIONS") and env.get("RUNNER_NAME"))  # (the shim's test, too)
        # A place left by the same build of this caller's, cancelled when its `buildq run` went: taken
        # here, so a retry that joins another build uses it up too.
        place = self.vacated.pop((cwd, tuple(argv), lane_name, t.caller), None) if t.caller else None
        source = commands.sane_source(msg.get("source"))
        effective_env = {**self.cfg.env, **env} if self.cfg.env else env
        if source and source.get("repo") and commands.paths_inside(argv, cwd, source["repo"], effective_env):
            settings = [t.timeout, t.umask, t.rlimits, t.cores_given, t.mem_given, self.cfg.env]
            t.source, t.dedupe = source, commands.dedupe_key(source, argv, cwd, lane_name, env, settings)
        elif source:
            t.source = source  # shown, but a command that reaches outside the tree matches nothing
        if t.dedupe and msg.get("again") is not True:
            earlier = self.same_build(t, msg, now)
            if earlier is not None:
                who = submission if isinstance(submission, str) and len(submission) <= 100 else f"anon-{tid}"
                how = "joined" if self.submit_outcome.get("joined") else "reused"
                earlier.answered[who] = how
                self.submissions[who] = earlier.id  # a resend gets the same answer
                if how == "joined":
                    earlier.interests[who] = "run" if t.attached else "submit"
                    if t.attached:
                        earlier.followed[who] = now
                    elif agent:
                        earlier.agents[who] = agent
                    earlier.wanted_at = now
                self.save(earlier)
                return earlier, None
        t.workspace = commands.workspace_of(cwd, env, argv)
        if isinstance(submission, str) and len(submission) <= 100:
            t.submission = submission
            self.submissions[submission] = tid
        t.interests = {t.submission or f"anon-{tid}": "run" if t.attached else "submit"}
        if t.attached:
            t.followed = {t.submission or f"anon-{tid}": now}
        elif agent:
            t.agents = {t.submission or f"anon-{tid}": agent}
        t.git_env = {k: v for k, v in env.items() if GIT_ENV.fullmatch(k)}
        t.cold = commands.is_cold(argv, cwd, env)
        t.max_log = self.cfg.max_log
        job_env = dict(env)
        for key, value in self.cfg.env.items():
            job_env.setdefault(key, value.replace("{id}", tid))
        job_env["BUILDQ_JOB"] = tid
        job_env["BUILDQ_TICKET"] = tid
        t.disk_paths = commands.disk_paths_of(cwd, job_env, argv)  # (configured defaults included: a TMPDIR elsewhere counts)
        t.write_dirs = commands.write_dirs_of(cwd, job_env, argv)
        try:
            os.makedirs(t.dir)
            common.write_json(os.path.join(t.dir, "env.json"), job_env)
            open(os.path.join(t.dir, "output.log"), "ab").close()
        except OSError as e:
            shutil.rmtree(t.dir, ignore_errors=True)
            return None, f"can't create the ticket: {e}"
        t.env = job_env
        t.queued_at = t.wait_mark = now
        if place and place[1] >= now and place[0] > 0:
            t.submitted = now - place[0]  # the retry of a run whose client went: as long a wait behind it
            common.log(f"{tid} takes the place of the same build, cancelled when its `buildq run` went "
                f"(it had waited {place[0]:.0f}s)")
        self.estimate(t, now)
        self.tickets[tid] = t
        self.save(t)
        common.log(f"queued {tid} [{lane_name}]: want {t.want} cores (at least {t.need}), {common.fmt_size(t.est_mem)} "
            f"({t.est_src}); {t.label!r} in {cwd}")
        self.schedule(now)
        return t, None

    def waiting(self, t: Ticket, now: float) -> list[str]:
        """The submissions still waiting for a build: dropped-off ones, and those of `buildq run`s
        following it, or whose client followed it within unwatched_seconds (it may be reconnecting,
        after a daemon restart say: the time counts from the daemon's start at the earliest). A run
        killed outright waits no more after that."""
        following = {getattr(c, "submission", None) for c in self.watchers.get(t.id, ())}

        def recent(who):
            return now - max(t.followed.get(who, 0.0), self.started_at) <= self.cfg.unwatched_seconds
        return [who for who, kind in t.interests.items() if kind == "submit" or who in following or recent(who)]

    def same_build(self, t: Ticket, msg: dict, now: float) -> Ticket | None:
        """An earlier ticket that answers this submission: the same build queued or running (it
        joins it), or, for a plain check or with --reuse, one that passed on the same source within
        reuse_window, with its source checked unchanged at its start and end."""
        same = [x for x in self.tickets.values() if x.dedupe == t.dedupe and not x.source_changed]
        live = [x for x in same if x.state not in FINISHED and not x.cancel_requested and not x.shed_error]
        if live:
            x = live[0]
            if not t.attached:
                x.attached = False  # a dropped-off submission depends on it now: no client owns it
            x.joiners += 1
            x.priority = max(x.priority, t.priority)
            if t.notify:
                x.extra_notify.append(t.notify)
            self.save(x)
            self.submit_outcome = {"joined": True}
            common.log(f"{x.id} is already {x.state} for the same command on the same source; a new submission joins it")
            return x
        passed = [x for x in same if x.state == "succeeded" and x.source_verified and x.ended
                  and now - x.ended <= self.cfg.reuse_window]
        if not passed:
            return None
        last = max(passed, key=lambda x: x.ended)
        reuse = msg.get("reuse")
        if reuse is True or (reuse is None and commands.reusable_check(t.argv)):
            self.submit_outcome = {"reused": True}
            common.log(f"{last.id} passed the same command on the same source {now - last.ended:.0f}s ago; "
                "its result answers a new submission")
            return last
        self.submit_outcome = {"passed_before": {"id": last.id, "short": last.id[:8], "ended": last.ended}}
        return None

    def cancel(self, t: Ticket, now: float, why: str | None = None) -> None:
        """Cancel a build. `why` is shown with a queued build's ticket when the daemon cancelled it
        by itself."""
        if t.state in FINISHED or t.flock:
            return
        t.cancel_requested = True
        if t.state == "queued":
            self.finalize(t, now, None, error=why)
            return
        self.stop(t, now, force=t.frozen)  # a frozen build can't act on SIGTERM; thawing it would let it grow
        self.save(t)
        self.broadcast(t, now)
        common.log(f"cancelling {t.id}")

    def stop(self, t: Ticket, now: float, force: bool = False) -> None:
        t.stop_sent = now
        t.force_sent = t.force_sent or force
        try:
            proc = self.runner_of(t).stop(t, force)
            if proc is not None:
                self.helpers.append((proc, now + 60))
        except OSError as e:
            common.log(f"can't stop {t.id}: {e}")

    # ---- pausing the queue

    def pause(self, reason: str | None, now: float) -> None:
        """Start nothing new until resumed (before a reboot, say); running builds carry on."""
        self.paused = {"reason": reason, "since": now}
        common.write_json(os.path.join(self.state_dir, "paused.json"), self.paused)
        common.log(f"paused the queue{f' ({reason})' if reason else ''}: running builds carry on, nothing new starts",
            level=4)
        self.schedule(now)

    def resume(self, now: float) -> None:
        if self.paused is None:
            return
        self.paused = None
        try:
            os.remove(os.path.join(self.state_dir, "paused.json"))
        except OSError:
            pass
        common.log("resumed the queue")
        self.schedule(now)

    def requeue(self, t: Ticket, now: float) -> None:
        """A build stopped to free memory is gone: queue it again, at the front, or fail it."""
        self.drop_queries(t)  # its attempt's answers are no use to the next one, or to no one
        t.wanted_at = now  # (#71: queued again, it has the whole of abandoned_seconds)
        result = common.read_json(os.path.join(t.dir, "result.json"))  # its kills, before the file goes
        if isinstance(result, dict):
            t.kills_seen = max(t.kills_seen, int(common.sane_count(result.get("oom_kills")) or 0))
            t.kills_own_cap = t.kills_own_cap or supervisor.oom_of(result)
        if t.cancel_requested:
            self.finalize(t, now, None)  # cancelled while it was being stopped: it stays cancelled
            return
        if t.shed_error or t.env is None:
            self.finalize(t, now, None, error=t.shed_error or "stopped to free memory; its environment is gone "
                                                              "since the daemon restarted: run it again")
            return
        try:  # unload the old service; the next attempt has a name of its own
            proc = self.runner_of(t).cleanup(t)
            if proc is not None:
                self.helpers.append((proc, now + 60))
        except OSError:
            pass
        self.close_jobserver(t, remove=True)
        try:
            for name in ("result.json", "supervisor.json", "child.json", "make_fixed.json"):
                try:
                    os.remove(os.path.join(t.dir, name))
                except FileNotFoundError:
                    pass
            common.write_json(os.path.join(t.dir, "env.json"), t.env)
            ci = self.tickets.get(t.yields_to) if t.yields_to else None
            cause = ("because the machine ran short of memory" if not t.yields_to else
                     f"to make room for a CI job's build ({ci.label[:60]}, {ci.id[:8]})" if ci is not None
                     else "to make room for a CI job's build")
            marker = (f"\nbuildq: stopped at {time.strftime('%H:%M:%S')} {cause}; "
                      "it starts again from the beginning when there is room.\n").encode()
            offsets = {}
            for name in ("output.log", "stderr.log", "stdout.log"):
                with open(os.path.join(t.dir, name), "a+b") as f:
                    if name != "stdout.log":  # stdout may be parsed: keep the note out of it
                        f.write(marker)
                    elif f.tell():
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b"\n":
                            f.write(b"\n")  # end a line the stop cut, so the next attempt's first line is whole
                    offsets[name] = f.tell()
        except OSError as e:
            self.finalize(t, now, None, error=f"stopped to free memory, and couldn't be queued again: {e}")
            return
        t.attempt_offsets = offsets
        if t.frozen and t.frozen_at:
            t.frozen_attempt += max(0.0, now - t.frozen_at)
        attempt = max(0.0, now - (t.started or now) - t.frozen_attempt)
        self.close_release_audit(t, attempt)
        t.attempts_wall += attempt
        t.frozen_total += t.frozen_attempt
        t.frozen_attempt = 0.0
        t.attempts_cpu += max(0, t.cpu_usec - (t.cpu_usec_base or 0)) / 1e6
        t.attempts_mem_wait += t.mem_wait_some
        t.attempts_mem_high += t.mem_high
        t.attempts_peak = max(t.attempts_peak, t.mem_peak)
        t.force_sent = t.freeze_failed = t.thaw_failed = False
        t.state, t.started, t.running_since, t.result_seen, t.stop_sent = "queued", None, None, None, None
        t.unit = t.cg = t.launcher = t.last_grant_t = t.cpu_usec_base = t.freeze_asked = t.exec_argv = None
        t.nice = None
        t.cap_killed = False
        t._pids = []
        t._own_stopped = t._all_stopped = None
        t.started_max_cores = t.max_granted  # (the row describes the last attempt that started)
        t.ended_rule = self.cfg.scale_mem_with_cores
        t.granted = t.max_granted = t.tokens_out = 0
        t.measured = t.elastic = t.frozen = t.requeue_pending = False
        t.frozen_at = None
        t.samples.clear()
        t.cores_now, t.mem_now, t.cpu_usec, t.grant_area = 0.0, 0, 0, 0.0
        t.mem_peak, t.peak_cores, t.peak_mem_cores = 0, 0.0, 0
        t.peak_at, t.tail, t.trace, t.trace_merges, t.released = 0.0, 0, [], 1, False
        t.release_at, t.release_hold, t.after_release_peak, t.release_revoked = None, 0, 0, False
        t.settled_since, t.swap_now, t.observed_gap, t.release_end, t.peak_cpu = None, 0, False, None, 0.0
        t.peak_swap = t.tail_used = 0
        t.release_est = t.release_oom_base = t.kills_seen = t.oom_kills = 0
        t.release_breach = t.kills_own_cap = t.release_open_at_gap = False
        t.sampled_own_oom = None
        t.mem_wait_some = t.mem_wait_full = 0.0
        t.mem_high = 0
        t.source_verified = None  # the next attempt is checked afresh
        t.source_checks = {}
        t.requeues += 1
        for_ci = bool(t.yields_to)  # stopped for a CI job's build (make_room_for_ci)
        if for_ci:
            t.ci_stops += 1
            if not self.yielding(t, now):  # it has started, left the queue or waited YIELD_MAX: nothing to yield to
                t.yields_to = t.yield_until = None
        t.queued_at = t.wait_mark = now
        if self.yielding(t, now):
            t.reason_cat, t.reason = "held", ("stopped to make room for a CI job's build; it goes first once "
                                              "that has started")
        elif for_ci:
            t.reason_cat, t.reason = "memory", ("stopped to make room for a CI job's build, which no longer needs "
                                                "it; it goes first once there is room")
        else:
            t.reason_cat, t.reason = "memory", "stopped to free memory; it goes first once there is room"
        self.save(t)
        self.broadcast(t, now)
        what = "make room for a CI job's build" if for_ci else "free memory"
        common.log(f"queued {t.id} again: stopped to {what} ({t.requeues} time{'s' if t.requeues != 1 else ''})", level=4)

    # ---- checking that a build built the source its record says

    SOURCE_CHECKS_AT_ONCE = 3

    def check_source(self, t: Ticket, phase: str, now: float) -> None:
        """Fingerprint the build's tree again, in a process of its own (`buildq source DIR`, with the
        submitter's git settings), as it starts and as it ends: the build uses what is on disk then,
        which may no longer be what was recorded when it was submitted. Only a build whose tree
        matched both times can answer a later submission, or tell GitHub it passed. At most three
        checks run at once; the rest wait their turn."""
        self.source_wanted.append((t.id, phase, t.requeues))
        self.start_source_checks(now)

    def start_source_checks(self, now: float) -> None:
        while self.source_wanted and len(self.source_checks) < self.SOURCE_CHECKS_AT_ONCE:
            tid, phase, attempt = self.source_wanted.popleft()
            t = self.tickets.get(tid)
            if t is None or t.requeues != attempt:
                continue
            module_mode = len(self.source_command) >= 3 and self.source_command[1:3] == ["-m", "buildq.client"]
            pypath = common.module_pythonpath(os.environ.get("PYTHONPATH"), always=True) if module_mode else None
            if t.git_env:
                env = {"LC_ALL": "C", **t.git_env}
                if pypath:
                    env["PYTHONPATH"] = pypath
                    env["PYTHONSAFEPATH"] = "1"
            elif pypath:
                env = {**os.environ, "PYTHONPATH": pypath, "PYTHONSAFEPATH": "1"}
            else:
                env = None
            try:
                proc = subprocess.Popen([*self.source_command, t.cwd], stdin=DEVNULL, stdout=subprocess.PIPE,
                                        stderr=DEVNULL, start_new_session=True, env=env)
            except OSError as e:
                common.log(f"can't check {t.id}'s source: {e}")
                t.source_checks[phase] = False
                continue
            self.source_checks.append((proc, tid, phase, now + 30, attempt))

    def collect_source_checks(self, now: float) -> None:
        for entry in list(self.source_checks):
            proc, tid, phase, deadline, attempt = entry
            if proc.poll() is None:
                if now < deadline:
                    continue
                try:  # the helper and the git under it
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
            self.source_checks.remove(entry)
            try:
                seen = json.loads(proc.stdout.read() or b"null") if proc.returncode == 0 else None
            except ValueError:
                seen = None
            proc.stdout.close()
            t = self.tickets.get(tid)
            if t is None or not t.source or t.requeues != attempt:
                continue  # gone, or a check of an attempt since stopped: it says nothing of this one
            same = isinstance(seen, dict) and seen.get("commit") == t.source["commit"] \
                and seen.get("fingerprint") == t.source["fingerprint"]
            t.source_checks[phase] = same
            if isinstance(seen, dict) and not same:
                t.source_changed = True
                common.log(f"{t.id}'s source changed {'before it started' if phase == 'start' else 'while it ran'}: "
                    "it answers no later submission")
            if "start" in t.source_checks and "end" in t.source_checks and t.state in FINISHED:
                t.source_verified = bool(t.source_checks["start"] and t.source_checks["end"]) and not t.source_changed
                if t.source_verified:
                    self.note_status(t, now)
            self.save(t)
        self.start_source_checks(now)

    # ---- watching builds

    def check(self, now: float) -> None:
        active = self.active()
        direct_jobs = {t.id: (t.pid, t.pid_start) for t in active
                       if not t.flock and t.state == "running" and not t.cg}
        proc_snap = machine.proc_trees_snapshot(direct_jobs) if direct_jobs else {}
        for t in active:
            if t.flock:
                t.measure(now)
                self.outgrown(t, now)
                continue
            if t.state == "starting" and not self.check_starting(t, now):
                continue
            t.measure(now, proc_sample=proc_snap.get(t.id))
            if t.release_dirty:  # a take-back is saved at once, so a crash can't bring the decision back
                t.release_dirty = False
                self.save(t)
            if t.requeue_pending:
                if self.runner_of(t).alive(t):
                    if t.stop_sent is None or now - t.stop_sent >= 30:
                        self.stop(t, now, force=True)
                    continue
                result = common.read_json(os.path.join(t.dir, "result.json"))
                if isinstance(result, dict) and isinstance(result.get("exit"), int) and result["exit"] < 128:
                    t.requeue_pending, t.shed_error = False, None  # it finished on its own first
                    self.finalize(t, now, result.get("exit"), oom=supervisor.oom_of(result),
                                  timed_out=bool(result.get("timed_out")), result=result)
                else:
                    self.requeue(t, now)
                continue
            # Non-cgroup runner MemoryMax watchdog: kill a build whose complete measured process-tree
            # memory exceeds its lane's MemoryMax, marking it killed for memory so history learns its cap.
            if t.cap_killed and self.runner_of(t).alive(t) and (t.stop_sent is None or now - t.stop_sent >= 2):
                # Retry force teardown if an earlier stop left live descendants (e.g. concurrent fork).
                self.stop(t, now, force=True)
                continue
            if (not self._runner_has_cgroup(self.runner_of(t)) and t.measured and t._mem_sample_ok
                    and not t.cancel_requested and not t.cap_killed):
                lane = self.lane(t.lane)
                if lane.mem_cap and t.mem_now > lane.mem_cap:
                    t.cap_killed = True
                    t.kills_own_cap = True
                    t.oom_kills += 1
                    t.error = f"exceeded its lane's MemoryMax ({common.fmt_size(lane.mem_cap)})"
                    common.log(f"{t.id} exceeded its lane's MemoryMax ({common.fmt_size(t.mem_now)} > "
                               f"{common.fmt_size(lane.mem_cap)}); stopping it", level=4)
                    self.stop(t, now, force=True)
                    self.save(t)
                    continue
            self.outgrown(t, now)
            if t.last_grant_t is not None and not t.frozen:
                t.grant_area += t.granted * max(0.0, now - t.last_grant_t)
            t.last_grant_t = now
            result = common.read_json(os.path.join(t.dir, "result.json"))
            if isinstance(result, dict):
                # Give systemd/runner a moment to stop anything the build left behind, so the turn ends
                # with the build's last process.
                t.result_seen = t.result_seen or now
                if self.runner_of(t).alive(t):
                    # Processes the build left behind still hold memory and its workspace: the
                    # turn lasts until they are gone. Past 15 s, they are killed.
                    if now - t.result_seen >= 15 and (t.stop_sent is None or now - t.stop_sent >= 30):
                        common.log(f"{t.id} ended, but processes it left are still running; killing them")
                        self.stop(t, now, force=True)
                    continue
                self.finalize(t, now, result.get("exit"), oom=t.cap_killed or supervisor.oom_of(result),
                              timed_out=bool(result.get("timed_out")), error=t.error if t.cap_killed else None,
                              result=result, learn_oom=True if t.cap_killed else None)
                continue
            if t.cancel_requested and t.stop_sent and now - t.stop_sent > 75:
                self.stop(t, now, force=True)
            if not self.runner_of(t).alive(t) and now - (t.running_since or now) > 3:
                result = common.read_json(os.path.join(t.dir, "result.json"))
                if isinstance(result, dict):
                    self.finalize(t, now, result.get("exit"), oom=t.cap_killed or supervisor.oom_of(result),
                                  timed_out=bool(result.get("timed_out")), error=t.error if t.cap_killed else None,
                                  result=result, learn_oom=True if t.cap_killed else None)
                else:
                    # Killed as a whole, supervisor and all: cgroup OOM, watchdog cap, systemd-oomd, or SIGKILL.
                    verdict = self.result_of(t)
                    if verdict is PENDING:
                        continue  # finalized on a later tick, with systemd's verdict
                    own_cap_now = bool(t.cg) and machine.cgroup_events(t.cg).get("oom", 0) > 0
                    slice_oom = verdict == "slice-oom-kill" or (
                        verdict == "oom-kill" and t.sampled_own_oom is False and not own_cap_now)
                    oom = (verdict == "oom-kill" and not slice_oom) or t.cap_killed
                    managed_oom = verdict == "managed-oom-kill"
                    error = None if t.cancel_requested else (
                        t.error if t.cap_killed and t.error else
                        "it was stopped for memory (systemd-oomd)" if managed_oom else
                        "it was stopped for memory (slice memory limit)" if slice_oom else
                        "it was stopped for memory (systemd-oomd or the kernel)" if oom else
                        f"the build ended without reporting an exit status ({verdict or 'killed?'})")
                    self.finalize(t, now, None, lost=True, oom=oom or managed_oom or slice_oom, learn_oom=oom, error=error)

    def attempt_seconds(self, t: Ticket, now: float | None) -> float:
        """Seconds into this attempt, pauses left out: the clock attempt_wall and the profile use."""
        now = time.time() if now is None else now
        return round(max(0.0, now - t.started - self.paused_for(t, now)), 1)

    def outgrown(self, t: Ticket, now: float | None = None) -> None:
        """A build that passes its estimate was underestimated (a full rebuild where history saw
        incremental ones, say). From then on, plan it at its lane's enforced MemoryMax, or at half as
        much again as it holds, so the room it may still grow into stays held."""
        if t.measured and t.outgrown_at is None and t.started and t.start_plan and t.mem_now > t.start_plan:
            # (against the plan it started with: a growth in cores may have raised est_mem since)
            t.outgrown_at = self.attempt_seconds(t, now)
        if not t.measured or t.mem_now <= t.est_mem:
            return
        if t.breached_at is None and t.started:  # (past the plan it holds now: a breach under any setting)
            t.breached_at = self.attempt_seconds(t, now)
            t.breaches += 1
            breached = True
        else:
            breached = False
        lane = self.lane(t.lane)
        cap = lane.mem_cap if self._runner_enforces_mem_cap(self.runner_of(t)) else None
        raised = cap if cap and cap > t.mem_now else int(t.mem_now * 1.5)
        if raised > t.est_mem:
            common.log(f"{t.id} passed its estimate ({common.fmt_size(t.est_mem)}); planning it at {common.fmt_size(raised)}")
            t.est_mem = raised
        if breached:
            self.save(t)  # (when, and the raised plan: a restart before the next save would lose both; #63)

    def check_starting(self, t: Ticket, now: float) -> bool:
        """Move a starting build on; True once it runs."""
        if t.launcher is None:  # found starting after a restart
            runner = self.runner_of(t)
            if runner.alive(t) or os.path.exists(os.path.join(t.dir, "result.json")):
                t.state, t.running_since = "running", now
                self.save(t)
                return True
            if now < (t.recheck_at or 0):
                return False
            state = self.unit_state_of(t)
            if state is PENDING:
                return False  # asked; the answer comes on a later tick
            if state in ("pending", "unknown"):
                # systemd has it again (or can't say): a reset-failed asked for meanwhile is stale
                stale = self.pending_queries.pop(("reset-failed", self.attempt(t)), None)
                if stale is not None:
                    stale.cancel()
                t.recheck_at = now + 10  # systemd has it, or can't say yet: look again
                return False
            if os.path.exists(os.path.join(t.dir, "env.json")) and t.cancel_requested:
                self.finalize(t, now, None)  # it never started, and it was cancelled
            elif os.path.exists(os.path.join(t.dir, "env.json")):
                # The runner has neither the service nor a job to start it, and no supervisor read
                # its environment: it never started. Queue it again, clearing any failed unit/cgroup
                # of that name first, so the next launch can use it.
                if runner.name == "systemd":
                    if self.query(("reset-failed", self.attempt(t)), machine.reset_failed_unit, t.unit) is PENDING:
                        return False  # the build stays starting until the name is free
                else:
                    try:
                        runner.cleanup(t)
                    except OSError:
                        pass
                self.close_jobserver(t, remove=True)
                self.drop_queries(t)  # (this attempt is over)
                t.state, t.started, t.unit, t.cg, t.exec_argv, t.granted, t.elastic = "queued", None, None, None, None, 0, False
                self.save(t)
                common.log(f"requeued {t.id}: it hadn't started when the daemon stopped")
            else:
                self.finalize(t, now, None, lost=True,
                              error="the daemon stopped while the build was starting, and it is gone")
            return False
        rc = t.launcher.poll()
        if rc is None:
            if now - (t.started or now) < 60:
                return False
            t.launcher.kill()  # systemd-run hung; find out what became of the service
            t.launcher.wait()
            t.launcher = None
            t.recheck_at = now
            return False
        if rc != 0:
            state = self.unit_state_of(t)
            if state is PENDING:
                return False  # the launcher's exit code keeps; look again on a later tick
            if state == "pending":
                rc = 0  # the service exists: a launch from before a daemon restart got there first
            elif state == "unknown":
                t.launcher, t.recheck_at = None, now + 10  # systemd isn't answering; ask again later
                return False
        if rc != 0:
            try:
                with open(os.path.join(t.dir, "launch.err"), errors="replace") as f:
                    detail = f.read().strip()[-500:]
            except OSError:
                detail = ""
            self.finalize(t, now, None, error=f"systemd-run couldn't start the build (exit {rc}): {detail}")
            return False
        t.launcher = None
        t.state, t.running_since = "running", now
        if t.cancel_requested:
            self.stop(t, now)
        self.save(t)
        self.broadcast(t, now)
        return True

    def close_jobserver(self, t: Ticket, remove: bool = False) -> None:
        if t.js is not None:
            t.js.close()
            t.js = None
        if remove and t.jobserver:
            try:
                os.remove(t.jobserver)
            except OSError:
                pass
            t.jobserver, t.tokens_out = None, 0

    def finalize(self, t: Ticket, now: float, exit_code, oom: bool = False, timed_out: bool = False,
                 error: str | None = None, lost: bool = False, result: dict | None = None,
                 learn_oom: bool | None = None) -> None:
        if t.state in FINISHED:
            return
        self.drop_queries(t)  # its attempt's answers are no use to anyone now
        t.env = None
        if isinstance(result, dict):  # the supervisor's reading, taken as the build ended
            t.take_waits(result.get("mem_wait_some"), result.get("mem_wait_full"), result.get("mem_high"))
            t.kills_seen = max(t.kills_seen, int(common.sane_count(result.get("oom_kills")) or 0))
            t.kills_own_cap = t.kills_own_cap or supervisor.oom_of(result)
        exit_code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
        lane = self.lane(t.lane)
        t.exit = exit_code
        result_hit_own_cap = result.get("hit_own_cap") if isinstance(result, dict) and "hit_own_cap" in result else None
        sampled_own_oom = (bool(t.oom_kills)
                           and result_hit_own_cap is not False
                           and learn_oom is not False
                           and t.sampled_own_oom is not False)
        own_oom = bool((oom if learn_oom is None else learn_oom) or sampled_own_oom) or (
            exit_code == 137 and bool(lane.mem_cap) and t.mem_peak >= 0.9 * lane.mem_cap)
        t.oom = bool(oom) or own_oom
        t.timed_out = timed_out
        t.error = error or (f"timed out after {t.timeout or 0:.0f}s" if timed_out else None)
        t.ended = now
        if t.cancel_requested:
            t.state = "cancelled"
        elif exit_code is None:
            t.state = "lost" if lost or not error else "failed"
        else:
            t.state = "succeeded" if exit_code == 0 else "failed"
        if t.launcher is not None:
            t.launcher.poll()
            t.launcher = None
        self.close_jobserver(t)
        if t.frozen and t.frozen_at:  # ended while paused
            t.frozen_attempt += max(0.0, now - t.frozen_at)
            t.frozen, t.frozen_at = False, None
        if not t.started and t.queued_at:
            t.wait_total += max(0.0, now - t.queued_at)  # it ends without having run: all of it was waiting
        # Time spent paused is neither how long it takes nor time its cores were used.
        wall = max(now - (t.started or now) - (t.frozen_attempt if t.started else 0.0), 0.0)
        self.close_release_audit(t, wall)
        t.frozen_total += t.frozen_attempt
        t.frozen_attempt = 0.0
        used = t.cpu_usec - (t.cpu_usec_base or 0)
        if isinstance(result, dict):
            res_cpu = result.get("cpu_s")
            if isinstance(res_cpu, (int, float)) and not isinstance(res_cpu, bool) and res_cpu >= 0:
                res_usec = int(float(res_cpu) * 1_000_000)
                if res_usec > used:
                    used = res_usec
                    # Keep t.cpu_usec in sync so outcomes.jsonl and metrics.db see the exact same
                    # authoritative terminal CPU, even on fast cgroup builds that ended before the
                    # 1 Hz sampler ran, without setting t.measured on CPU-only runs (so an unsampled
                    # memory peak is never mistaken for an observed 0 bytes).
                    t.cpu_usec = (t.cpu_usec_base or 0) + used
        t.avg_cores = used / 1e6 / wall if wall > 0 and (t.measured or used > 0) else 0.0
        t.peak_cores = max(t.peak_cores, t.avg_cores)
        if t.flock:
            self.tickets.pop(t.id, None)
            common.log(f"no longer counting {t.id} ({error or 'lock released'})")
            return
        for secret_file in ("env.json", "parallel.json"):
            try:
                os.remove(os.path.join(t.dir, secret_file))
            except OSError:
                pass
        # A run that a signal ended (a cancel, a timeout) shows how far it got, not what it needs,
        # so it teaches nothing; a run the kernel killed for its own memory cap teaches that it
        # needs more (a slice/ancestor managed-oom kill doesn't inflate this command's own estimate).
        if own_oom and not t.measured:  # killed before it could be sampled: it needed at least its cap
            t.mem_peak = max(t.mem_peak, lane.mem_cap or t.est_mem)
            t.peak_cores = max(t.peak_cores, float(t.max_granted or 1))
        if exit_code is not None and exit_code < 128 and not t.measured and not t.oom:
            t.mem_peak = t.mem_peak or 0  # too quick to sample: it needed little, and that is worth knowing
        learn = own_oom or (exit_code is not None and exit_code < 128 and (t.measured or wall < 2))
        if learn and not t.cancel_requested and not timed_out:
            given = round(t.grant_area / wall, 1) if wall > 0 and t.grant_area else t.max_granted
            try:
                self.history.add({
                    "key": t.key, "lane": t.lane, "t": round(time.time()), "wall": round(wall, 1), "exit": exit_code,
                    "peak_mem": t.mem_peak, "mem_measured": bool(t.measured or own_oom),
                    "peak_cores": round(t.peak_cores, 2), "avg_cores": round(t.avg_cores, 2),
                    "granted": given, "mem_cores": t.peak_mem_cores or t.max_granted, "elastic": t.elastic,
                    "cold": t.cold, "oom": own_oom, "mem_wait_some": round(t.mem_wait_some, 1),
                    "mem_wait_full": round(t.mem_wait_full, 1), "mem_high": t.mem_high,
                    "waited": round(t.wait_total, 1), "caller": t.caller,
                    "requeues": t.requeues,
                    "peak_at": round(t.peak_at, 1) if t.measured and not t.observed_gap else None,
                    "tail": t.tail if t.measured and not t.observed_gap else None,
                    "peak_swap": t.peak_swap if t.measured and not t.observed_gap else None,
                    "tail_used": t.tail_used if t.measured and not t.observed_gap else None,
                    "peak_cpu": round(t.peak_cpu, 1) if t.measured and not t.observed_gap else None,
                })
            except OSError as e:
                common.log(f"can't record {t.id} in history: {e}")
        try:
            proc = self.runner_of(t).cleanup(t)
            if proc is not None:
                self.helpers.append((proc, now + 60))
        except OSError:
            pass
        self.save(t)
        self.broadcast(t, now)
        self.run_hooks(t, now)
        if t.dedupe and t.state in ("succeeded", "failed") and not t.source_changed:
            self.check_source(t, "end", now)
        self.record_outcome(t, wall)
        try:
            self.record_build(t, wall)
        except Exception as e:  # the analytics must never cost a build its ending
            self.metrics.trouble(f"can't record {t.id} for the analytics: {e!r}")
        if t.state == "succeeded" and commands.command_words(t.argv)[:2] == ["git", "push"] and t.source:
            self.retry_statuses(now, repo=t.source.get("remote"))
        waits = ""
        if t.mem_wait_some >= 1 or t.mem_high:
            waits = (f"; waited on memory {t.mem_wait_some:.0f}s ({t.mem_wait_full:.0f}s with all of it waiting)"
                     f"{', and went over its MemoryHigh' if t.mem_high else ''}")
        common.log(f"{t.state} {t.id} (exit {t.exit}{', killed for memory' if t.oom else ''}"
            f"{'; ' + t.error if t.error else ''}) after {wall:.0f}s: peak {common.fmt_size(t.mem_peak)}, "
            f"{t.peak_cores:.1f} cores at most, {t.avg_cores:.1f} on average, {t.max_granted} given{waits}")

    def record_outcome(self, t: Ticket, wall: float) -> None:
        """Every finished build, for `buildq report` (the history keeps only the runs that teach)."""
        if t.flock:
            return
        path = os.path.join(self.state_dir, "outcomes.jsonl")
        # What every attempt took, the ones stopped to free memory included.
        record = {"t": round(time.time()), "key": t.key, "lane": t.lane, "caller": t.caller, "state": t.state,
                  "exit": t.exit, "oom": t.oom, "timed_out": t.timed_out,
                  "stopped_for_memory": bool(t.shed_error) or t.requeues > t.ci_stops, "requeues": t.requeues,
                  "ci_stops": t.ci_stops,
                  "wall": round(wall + t.attempts_wall, 1), "waited": round(t.wait_total, 1),
                  "peak_mem": max(t.mem_peak, t.attempts_peak),
                  "mem_wait_some": round(t.mem_wait_some + t.attempts_mem_wait, 1), "mem_high": t.mem_high,
                  "avg_cores": round(t.avg_cores, 2), "paused": round(t.frozen_total, 1),
                  "cpu_seconds": round(t.attempts_cpu + max(0, t.cpu_usec - (t.cpu_usec_base or 0)) / 1e6, 1),
                  "mem_high_all": t.mem_high + t.attempts_mem_high}
        try:
            if os.path.getsize(path) > 8 << 20:
                os.replace(path, path + ".1")
        except OSError:
            pass
        try:
            with open(path, "a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as e:
            common.log(f"can't record {t.id}'s outcome: {e}")

    def record_build(self, t: Ticket, wall: float) -> None:
        """A finished build's row in the analytics file: what it waited for, what was planned for
        it, and what it used, with the shape of its memory over its last attempt."""
        if t.flock or not self.cfg.metrics:
            return
        whole = t.measured and not t.observed_gap  # watched from start to end
        profile = metrics.memory_profile(t.trace, wall) if whole else None
        self.metrics.build({
            "id": t.id, "ended": round(t.ended or time.time()), "submitted": round(t.submitted), "key": t.key,
            "label": t.label[:200], "name": t.name, "lane": t.lane, "caller": t.caller,
            "repo": (t.source or {}).get("repo") or t.workspace, "state": t.state, "exit": t.exit,
            "from_ci": int(bool(t.from_ci)), "wall": round(wall + t.attempts_wall, 1), "waited": round(t.wait_total, 1),
            "attempt_wall": round(wall, 1), "workspace": t.workspace,
            "start_plan": t.start_plan or None, "start_cores": t.start_cores or None, "outgrown_at": t.outgrown_at,
            "plan_base": t.start_base or None, "plan_cores": t.start_base_cores or None, "cold": int(bool(t.cold)),
            "breached_at": t.breached_at, "breaches": t.breaches, "start_rule": t.start_rule,
            "end_rule": (t.ended_rule or self.cfg.scale_mem_with_cores) if t.start_rule else None,
            "max_cores": t.max_granted or t.started_max_cores or None,
            "rigid": int(bool(t.start_rigid)) if t.start_rule else None,
            "peak_swap": t.peak_swap if whole else None, "tail_used": t.tail_used if whole else None,
            "waited_by": json.dumps(t.waited_by, separators=(",", ":"), sort_keys=True),
            "est_mem": t.est_mem, "est_src": t.est_src, "mem_peak": max(t.mem_peak, t.attempts_peak),
            "peak_at": round(t.peak_at, 1) if whole else None, "tail": t.tail if whole else None,
            "profile": json.dumps(profile) if profile else None, "released": int(t.released),
            "release_at": t.release_at, "release_hold": t.release_hold if t.release_at is not None else None,
            "after_release_peak": t.after_release_peak if t.release_at is not None else None,
            "release_revoked": int(t.release_revoked), "release_end": t.release_end,
            "past_peak_attempts": t.past_peak_attempts, "past_peak_over": t.past_peak_over,
            "past_peak_most_over": t.past_peak_most_over,
            "past_peak_freed": round(t.past_peak_freed / (1 << 30) / 3600, 3), "past_peak_unknown": t.past_peak_unknown,
            "past_peak_released": t.past_peak_released,
            "observed_gap": int(t.observed_gap),
            "cores_granted": round(t.grant_area / wall, 2) if wall > 0 and t.grant_area else t.max_granted,
            "avg_cores": round(t.avg_cores, 2), "peak_cores": round(t.peak_cores, 2),
            "cpu_seconds": round(t.attempts_cpu + max(0, t.cpu_usec - (t.cpu_usec_base or 0)) / 1e6, 1),
            "requeues": t.requeues, "ci_stops": t.ci_stops,
            "paused": round(t.frozen_total + t.frozen_attempt, 1), "oom": int(bool(t.oom)),
            "mem_high": t.mem_high + t.attempts_mem_high, "mem_wait": round(t.mem_wait_some + t.attempts_mem_wait, 1),
        })

    def observe_metrics(self, now: float) -> None:
        """This tick's numbers, added to the minute the analytics file will get."""
        if not self.cfg.metrics:
            return
        m, cfg, budget = self.machine, self.cfg, self.budget
        running = [t for t in self.active() if not t.flock]
        queued = [t for t in self.tickets.values() if t.state == "queued"]
        waits = collections.Counter(WAIT_COLUMN.get(t.reason_cat, "wait_other") for t in queued)
        values = {
            "cores": m.ncpu, "cpu_target": cfg.cpu_target * m.ncpu, "cpu_busy": m.busy_now,
            "mem_total": m.mem_total, "mem_floor": cfg.mem_floor, "mem_available": m.mem_available,
            "swap_used": m.swap_used, "psi_cpu": m.psi_cpu, "psi_mem": m.psi_mem,
            "running": len(self.active()), "queued": len(queued),
            "reserved": sum(t.est_mem for t in running), "builds_mem_avg": sum(t.mem_now for t in running),
            "growth": budget.get("growth_reserved", 0),
            "free_mem": budget.get("free_mem", 0), "free_cpu": budget.get("free_cores", 0),
        }
        values.update({name: waits.get(name, 0) for name in WAIT_COLUMNS})
        builds_cg = self._runner_builds_cgroup(self.runner)
        builds_fallback = (sum(t.mem_now for t in running), sum(t.cores_now for t in running))
        self.metrics.observe(now, values, builds_cg, builds_fallback)

    def run_hooks(self, t: Ticket, now: float) -> None:
        for command in (t.notify, *t.extra_notify, self.cfg.on_finish):
            if not command:
                continue
            env = dict(os.environ, BUILDQ_TICKET=t.id, BUILDQ_STATE=t.state,
                       BUILDQ_EXIT="" if t.exit is None else str(t.exit), BUILDQ_OOM="1" if t.oom else "0",
                       BUILDQ_LABEL=t.label, BUILDQ_NAME=t.name or "", BUILDQ_CWD=t.cwd, BUILDQ_LOG=t.log or "")
            try:
                proc = subprocess.Popen(["/bin/sh", "-c", command], env=env, cwd=os.path.expanduser("~"),
                                        stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)
                self.helpers.append((proc, now + 120))
            except OSError as e:
                common.log(f"can't run the finish hook for {t.id}: {e}")

    def reap_helpers(self, now: float) -> None:
        keep = []
        for proc, deadline in self.helpers:
            if proc.poll() is None:
                if now > deadline:
                    proc.kill()
                keep.append((proc, deadline))
        self.helpers = keep

    # ---- builds started outside the queue, holding flock slots of an older semaphore script

    def scan_flock_slots(self, now: float) -> None:
        seen = set()
        if self.cfg.flock_slots:
            locked = machine.locked_inodes()
            for pattern, lane in self.cfg.flock_slots:
                for path in sorted(glob.glob(pattern)):
                    path = os.path.realpath(path)
                    try:
                        inode = os.stat(path).st_ino
                    except OSError:
                        continue
                    if inode not in locked:
                        continue
                    tid = "flock:" + path
                    t = self.tickets.get(tid)
                    if t and machine.holds_flock(t.flock[1], path, inode):
                        seen.add(tid)
                        continue
                    if t:
                        self.finalize(t, now, None, error="the lock changed hands")
                    holder = machine.find_flock_holder(path, inode)
                    if holder is not None:  # a lock that none of our processes holds isn't one of ours
                        seen.add(tid)
                        self.adopt_flock(tid, path, holder, lane, now)
        for t in [t for t in self.tickets.values() if t.flock and t.id not in seen]:
            self.finalize(t, now, None, error="lock released")

    def adopt_flock(self, tid: str, path: str, pid: int, lane: str, now: float) -> None:
        argv, cwd, cg = machine.proc_cmdline(pid), "/", None
        own = machine.proc_cgroup(pid)
        children = machine.proc_children(pid)
        for child in children:
            child_cg = machine.proc_cgroup(child)
            if child_cg and child_cg != own and child_cg.endswith(".scope"):
                argv, cg = machine.proc_cmdline(child) or argv, child_cg
                break
        else:
            if children:
                argv = machine.proc_cmdline(children[0]) or argv
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass
        argv = argv or [f"(holder of {os.path.basename(path)})"]
        t = Ticket(tid, argv, cwd, lane, commands.command_key(argv, cwd, lane, self.cfg.script_labels), now, None)
        t.state, t.started, t.running_since, t.runner = "running", now, now, "outside"
        t.flock = (path, pid)
        t.cg = cg
        t.oom_base = None
        self.estimate(t, now)
        t.granted = t.want
        t.est_mem = self.mem_for(t, t.granted)
        self.tickets[tid] = t
        common.log(f"counting {tid} (held by pid {pid}, started outside the queue) as a running build: {t.label}")

    # ---- surviving a restart

    def load_tickets(self, now: float) -> None:
        for path in glob.glob(os.path.join(self.tickets_dir, "*", "ticket.json")):
            directory = os.path.dirname(path)
            try:
                t = Ticket.from_record(common.read_json(path), directory)
            except (TypeError, KeyError, ValueError):
                common.log(f"skipping unreadable ticket {directory}")
                continue
            if t.id != os.path.basename(directory):
                continue
            self.tickets[t.id] = t
            if t.submission:
                self.submissions[t.submission] = t.id
            if not isinstance(t.answered, dict):
                t.answered = {}
            if not isinstance(t.interests, dict):
                t.interests = {}
            if not isinstance(t.followed, dict):
                t.followed = {}
            t.agents = machine.sane_agents(t.agents)
            if not t.interests and t.state not in FINISHED:  # saved before buildq kept interests
                t.interests = {t.submission or f"anon-{t.id}": "run" if t.attached else "submit"}
            for other in t.answered:
                self.submissions[str(other)] = t.id
            if t.state in FINISHED:
                continue
            if t.state in ("starting", "running") and t.group_lane is None:
                # Saved before groups were recorded: it started in this lane, under this lane's group
                # (or, the lane being gone, the lane's own name, which is what the old daemon held).
                t.group_lane = t.lane
                if t.workspace_group is None:
                    lane = self.cfg.lanes.get(t.lane)
                    t.workspace_group = (lane.workspace_group if lane.exclusive else "") if lane else t.lane
                self.save(t)  # at once: a crash before the next save mustn't lose it
            if t.lane not in self.cfg.lanes:
                t.lane = self.cfg.default_lane
            if t.state == "queued":
                if os.path.exists(os.path.join(directory, "env.json")):
                    env = common.read_json(os.path.join(directory, "env.json"))
                    t.env = env if isinstance(env, dict) else None
                    t.reason_cat = "restart"  # the time the daemon was away is waited for that
                    common.log(f"kept {t.id} in the queue across the restart")
                else:
                    self.finalize(t, now, None, error="its environment file is gone")
                continue
            if t.jobserver and os.path.exists(t.jobserver):
                try:
                    t.js = Jobserver(t.jobserver, create=False, out=t.tokens_out)
                    t.js.used = t.elastic
                except OSError:
                    t.js = None
            t.cpu_usec_base = None  # count CPU from here; the cgroup's counter started before
            t.observed_gap = True  # it ran unwatched for a while: its peak and after may have been missed
            t.release_stood_at_load = t.release_at is not None and not t.release_revoked
            t.release_open_at_gap = t.release_open_at_gap or t.release_stood_at_load
            runner = self.runner_of(t)
            if isinstance(t.freeze_asked, list) and len(t.freeze_asked) >= 2 and getattr(runner, "can_freeze", False):
                wanted = bool(t.freeze_asked[0])
                t.freeze_asked = (wanted, now)  # sent again, with a full minute: its sender went with the old daemon
                try:
                    proc = runner.freeze(t, wanted)
                    if proc is not None:
                        self.helpers.append((proc, now + 30))
                except OSError:
                    pass
            else:
                t.freeze_asked = None
            if t.freeze_asked is None:
                own, took, parent_frozen = self._runner_freeze_state(runner, t)
                if own is not None:
                    if not own and t.frozen:  # resumed while the daemon was away: the pause ends now
                        if t.frozen_at:
                            t.frozen_attempt += max(0.0, now - t.frozen_at)
                        t.frozen, t.frozen_at, t.thaw_failed = False, None, False
                    elif own and not t.frozen and not parent_frozen:
                        t.frozen, t.frozen_at = True, (now if took else None)
                    elif own and t.frozen and not t.frozen_at and took:
                        t.frozen_at = now  # (a pause counts only once in effect)
            if t.state == "starting":
                t.recheck_at = now + 10
            else:
                t.running_since = now
            if t.cancel_requested:
                self.stop(t, now, force=t.frozen)  # a cancel in progress when the daemon stopped goes on
            common.log(f"picked up {t.id} [{t.lane}] ({t.state}) from before the restart: {t.label}")
        # Prune stale empty build cgroups ONLY after every saved ticket is loaded so neither
        # terminal OOM counters nor pre-join active build cgroups are deleted prematurely.
        cg_runner = self.runners.get("cgroup")
        if cg_runner is not None and hasattr(cg_runner, "prune_stale"):
            active_cgs = {t.cg for t in self.tickets.values() if t.state in ("starting", "running") and t.cg}
            cg_runner.prune_stale(active_cgs)

    def prune(self, now: float) -> None:
        finished = sorted((t for t in self.tickets.values() if t.state in FINISHED and t.dir),
                          key=lambda t: t.ended or 0)
        excess = len(finished) - self.cfg.max_finished
        for i, t in enumerate(finished):
            if i < excess or (t.ended or 0) < now - self.cfg.keep_finished:
                shutil.rmtree(t.dir, ignore_errors=True)
                self.tickets.pop(t.id, None)
                if t.submission:
                    self.submissions.pop(t.submission, None)
                for other in t.answered:
                    self.submissions.pop(other, None)

    # ---- status

    def overview(self, now: float) -> dict:
        queue = self.queue()
        running = sorted(self.active(), key=lambda t: t.started or 0)
        recent = sorted((t for t in self.tickets.values() if t.state in FINISHED), key=lambda t: t.ended or 0)[-10:]
        out = {
            "version": VERSION, "now": now, "runner": self.runner.name,
            "builds_cg": self._runner_builds_cgroup(self.runner),
            "machine": self.machine.summary(),
            "budget": self.budget, "config": self.cfg.summary(), "paused": self.paused,
            "running": [t.row(now) for t in running],
            "queued": [t.row(now, i) for i, t in enumerate(queue)],
            "recent": [t.row(now) for t in reversed(recent)],
        }
        if self.cfg.github_status:
            out["github_status"] = self.github_status_summary()
        if self.tick_ms:
            samples = sorted(self.tick_ms)
            tick_info: dict = {
                "last_ms": round(self.tick_ms[-1], 1),
                "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 1),
                "max_ms": round(self.tick_max_ms, 1),
            }
            if self.tick_last_slow is not None:
                tick_info["last_slow"] = dict(self.tick_last_slow)
            out["tick"] = tick_info
        return out

    def listing(self, now: float, everything: bool) -> list[dict]:
        queue = self.queue()
        rows = []
        for t in sorted(self.tickets.values(), key=lambda t: t.submitted, reverse=True):
            if not everything and t.state in FINISHED and (t.ended or 0) < now - 86400:
                continue
            rows.append(t.row(now, queue.index(t) if t.state == "queued" else None))
        return rows

    # ---- the loop

    def tick(self, now: float) -> None:
        t0 = time.monotonic()
        prev = t0
        phases: list[tuple[str, float]] = []

        def mark(name: str) -> float:
            nonlocal prev
            t = time.monotonic()
            phases.append((name, (t - prev) * 1000.0))
            prev = t
            return t

        self.machine.sample(t0)
        mark("sample")
        self.check(now)
        mark("check")
        self.shed_memory(now)
        mark("shed")
        self.drop_unwatched(now)
        mark("unwatched")
        self.drop_abandoned(now)
        mark("abandoned")
        self.scan_flock_slots(now)
        mark("flock")
        self.schedule(now)
        mark("schedule")
        self.make_room_for_ci(now)
        mark("ci_room")
        try:
            self.observe_metrics(now)
        except Exception as e:  # nor the loop its tick
            self.metrics.trouble(f"can't add up this tick for the analytics: {e!r}")
        tick = mark("metrics")
        if tick - self.last_rebalance >= self.REBALANCE_SECONDS:
            self.last_rebalance = tick
            self.rebalance(now)
            mark("rebalance")
            self.guard_slice()
            mark("guard_slice")
        elif self.memory_moving():
            self.guard_slice()  # every tick while memory is short or falling fast (#5)
            mark("guard_slice")
        self.watch_memory(tick)
        mark("watch_memory")
        if self.source_checks or self.source_wanted:
            self.collect_source_checks(now)
            mark("source_checks")
        if self.status_posts or self.status_procs:
            self.send_status_posts(now)
            mark("status_posts")
        self.send_waits(now)
        mark("send_waits")
        self.reap_helpers(now)
        mark("reap_helpers")
        if tick - self.last_prune >= 600:
            self.last_prune = tick
            self.prune(now)
            mark("prune")
        dur_ms = (prev - t0) * 1000.0
        self.tick_ms.append(dur_ms)
        if dur_ms > self.tick_max_ms:
            self.tick_max_ms = dur_ms
        if dur_ms >= 500.0:
            top = sorted(phases, key=lambda p: p[1], reverse=True)[:3]
            shown = [(name, ms) for name, ms in top if ms >= 1.0]
            self.tick_last_slow = {
                "ms": round(dur_ms, 1),
                "at": round(now, 1),
                "phases": {name: round(ms, 1) for name, ms in shown},
            }
            detail = ", ".join(f"{name} {ms:.0f}ms" for name, ms in shown)
            suffix = f" ({detail})" if detail else ""
            common.log(f"slow daemon tick: {dur_ms:.0f}ms (max {self.tick_max_ms:.0f}ms){suffix}", level=4)

    def vacate(self, t: Ticket, now: float) -> None:
        """A queued `buildq run` build whose client went (a harness's tool timeout, by SIGKILL, or
        SIGTERM or SIGHUP) leaves its place for the retry, which a harness usually makes: the same
        command, in the same directory and lane, from the same caller, within VACATED_SECONDS. The
        place is the time it waited, so the retry doesn't go to the back of the queue, where under load
        it could cycle without ever running (#7). A build that was running leaves none: it had its
        turn, and one that can't finish within the tool's time limit mustn't come back first for ever."""
        if t.state != "queued" or not t.attached or not t.caller or t.requeues:
            # (with no caller to tell whose retry it is, anyone could take it; and a build queued again
            # after a memory stop had its turn: its "wait" would count the time it ran)
            return
        key = (t.cwd, tuple(t.argv), t.lane, t.caller)
        waited = max(0.0, now - t.submitted)
        earlier = self.vacated.get(key)
        if earlier and earlier[1] >= now:
            waited = max(waited, earlier[0])  # the longest wait of the two
        self.vacated[key] = (waited, now + self.VACATED_SECONDS)

    def drop_unwatched(self, now: float) -> None:
        """A build that `buildq run` submitted is meant to be followed. Once no one has followed it
        for unwatched_seconds (its client was killed, by an editor or a tool timeout), cancel it rather than let
        it hold a turn for output nobody reads. Builds dropped off with `buildq submit` stay."""
        for t in list(self.tickets.values()):
            if t.state not in FINISHED and t.interests:
                for conn in self.watchers.get(t.id, ()):
                    if getattr(conn, "submission", None) in t.interests:
                        t.followed[conn.submission] = now
                dirty = False
                gone = [who for who, kind in t.interests.items() if kind == "run" and who not in self.waiting(t, now)]
                if gone and len(gone) < len(t.interests):  # (the last one's going is the unwatched rule below)
                    for who in gone:
                        del t.interests[who]
                        t.followed.pop(who, None)
                    dirty = True
                want_attached = "submit" not in t.interests.values()
                if t.attached != want_attached:
                    t.attached = want_attached
                    dirty = True
                if dirty:
                    self.save(t)
            if not t.attached or t.state in FINISHED or t.cancel_requested or t.flock:
                continue
            if self.watchers.get(t.id):
                t.last_watched = now
            elif now - max([t.last_watched, *t.followed.values()]) > self.cfg.unwatched_seconds:
                common.log(f"cancelling {t.id}: the `buildq run` that submitted it has gone")
                self.vacate(t, now)
                self.cancel(t, now)
        for place, (_, until) in list(self.vacated.items()):
            if until < now:
                del self.vacated[place]

    def live_thread(self, thread: str, now: float, fresh: bool) -> list[int] | None | bool:
        """A running agent process of this T3 thread as [pid, start ticks], None if there is none,
        or True if that can't be told. From a scan of /proc kept for 30 s, or taken now if `fresh`."""
        if self.thread_scan is None or now - self.thread_scan[0] >= 30 or (fresh and self.thread_scan[0] != now):
            try:
                self.thread_scan = (now, machine.live_threads())
            except OSError as e:
                common.log(f"can't list processes to look for agent sessions: {e}")
                self.thread_scan = (now, None)
        scan = self.thread_scan[1]
        if scan is None:
            return True
        return next((list(p) for p in scan.get(thread, ()) if not machine.proc_gone(p[0], p[1])), None)

    def agent_state(self, t: Ticket, now: float, running: dict, fresh: bool = False) -> str | None:
        """Whether the agent sessions that dropped a build off still run: "alive" while one of them
        does, "gone" once every one has ended, None when it isn't theirs alone to want: a `buildq
        run` waits for it too, or a submission from no known agent (CI, a terminal).

        A session runs while its agent process does, or an agent that process ran under, or a later
        agent process of the same T3 thread (a restart, a handoff to another account), which then
        stands for it. A process that can't be read isn't called gone. `running` remembers each
        process looked at, for one pass over the tickets."""
        waiting = self.waiting(t, now)
        if not waiting or any(t.interests.get(who) != "submit" or who not in t.agents for who in waiting):
            return None
        for who in waiting:
            record = t.agents[who]
            for pid, start in record["pids"]:
                if (pid, start) not in running:
                    running[pid, start] = not machine.proc_gone(pid, start)
                if running[pid, start]:
                    return "alive"
            later = self.live_thread(record["thread"], now, fresh) if record["thread"] else None
            if later:
                if later is not True:
                    record["pids"] = [later]
                    self.save(t)
                return "alive"
        return "gone"

    def drop_abandoned(self, now: float) -> None:
        """A build dropped off with `buildq submit` is waited on in rounds (`buildq wait --timeout 9m`),
        so going unwatched for a while means nothing. But once the agent session that dropped it off
        has ended, and for abandoned_seconds on end that session stayed gone and nobody waited on the
        ticket, it would only run for nobody: cancel it while it's queued (#71). The time counts from
        the daemon's start at the earliest, and from when it was queued again for a build that ran
        and was stopped. A build that is running carries on."""
        limit = self.cfg.abandoned_seconds
        running: dict = {}
        for t in list(self.tickets.values()):
            if not limit or t.state != "queued" or t.cancel_requested or t.flock or t.attached or not t.agents:
                t.agent_now = None
                if t.state not in FINISHED:
                    t.wanted_at = now  # (the time starts when the rule first applies to it)
                continue
            t.agent_now = self.agent_state(t, now, running)
            if t.agent_now == "gone" and not self.watchers.get(t.id) and now - t.wanted_at > limit:
                t.agent_now = self.agent_state(t, now, running, fresh=True)  # a look of its own, not the kept one
                if t.agent_now == "gone":
                    common.log(f"cancelling {t.id}: the agent session that dropped it off ended, and nobody has "
                        f"waited on it for {common.fmt_duration(now - t.wanted_at)}")
                    self.cancel(t, now, why=f"the agent session that dropped it off had ended, and nobody waited "
                                            f"on it for {common.fmt_duration(limit)}; submit it again if it's still wanted")
                    continue
            if t.agent_now != "gone" or self.watchers.get(t.id):
                t.wanted_at = now

    def send_waits(self, now: float) -> None:
        for position, t in enumerate(self.queue()):
            if t.reason_cat != t.sent_cat or now - t.sent_at >= 60:
                t.sent_cat, t.sent_at = t.reason_cat, now
                for conn in list(self.watchers.get(t.id, ())):
                    conn.send({"type": "wait", "id": t.id, "ahead": position, "reason": t.reason,
                               "category": t.reason_cat, "waited": round(now - t.submitted)})

    async def ticker(self) -> None:
        while not self.stopping:
            try:
                self.tick(time.time())
            except Exception:
                common.log("tick failed:\n" + traceback.format_exc())
            await asyncio.sleep(1.0)

    def handle_message(self, msg: dict, conn: Conn, now: float) -> None:
        op = msg.get("op")
        if op == "submit":
            t, error = self.submit(msg, now, peer=getattr(conn, "peer_pid", None))
            if t is None:
                conn.send({"type": "error", "error": error})
                return
            if msg.get("watch") and t.state not in FINISHED:
                self.watchers[t.id].add(conn)
                conn.submission = msg.get("submission")
                t.sent_cat = None  # send the first reason on the next tick
            conn.send({"type": "ticket", **t.row(now, self.position(t)), **self.submit_outcome})
        elif op in ("watch", "cancel"):
            t, error = self.resolve(msg.get("ticket"))
            if t is None:
                conn.send({"type": "error", "error": error})
                return
            if op == "cancel":
                if t.flock:
                    conn.send({"type": "error", "error": f"{t.id} was started outside the queue; buildq can't cancel it"})
                    return
                who = msg.get("submission") or msg.get("owner")
                who = who if isinstance(who, str) and len(who) <= 100 else None
                if not msg.get("all") and t.state not in FINISHED:
                    if who is not None and t.interests and who not in t.interests:
                        conn.send({"type": "ticket", **t.row(now, self.position(t)), "kept": True})
                        return  # not one of its submissions (or one that went already): nothing to stop
                    if who is not None and who in t.interests:
                        # A `run` that made or joined it was interrupted: that submission stops
                        # waiting. The build stops only when it was the last one waiting.
                        del t.interests[who]
                        t.followed.pop(who, None)
                        t.agents.pop(who, None)
                        if self.waiting(t, now):
                            t.attached = "submit" not in t.interests.values()
                            self.save(t)
                            common.log(f"{t.id}: a submission that waited for it went; {len(self.waiting(t, now))} still wait")
                            conn.send({"type": "ticket", **t.row(now, self.position(t)), "kept": True})
                            return
                    elif who is None and len(self.waiting(t, now)) > 1:
                        conn.send({"type": "error", "error": (
                            f"{t.id[:8]} is shared: {len(self.waiting(t, now))} submissions of the same build wait for it. "
                            "`buildq cancel --all` stops it for all of them")})
                        return
                if msg.get("vacate") is True:  # the client was stopped (SIGTERM, SIGHUP), not interrupted
                    self.vacate(t, now)
                self.cancel(t, now)
                self.schedule(now)
            elif t.state not in FINISHED:
                self.watchers[t.id].add(conn)
                t.wanted_at = now  # (a wait shorter than a tick counts too)
                if isinstance(msg.get("submission"), str):
                    conn.submission = msg["submission"]  # a `run` following again after a reconnect
                    if self.submissions.get(conn.submission) == t.id and conn.submission not in t.interests \
                            and (conn.submission == t.submission or t.answered.get(conn.submission) == "joined"):
                        t.interests[conn.submission] = "run"  # it had been given up for gone: it waits again
                    if conn.submission in t.interests:
                        t.followed[conn.submission] = now
                        self.save(t)
                t.sent_cat = None
            conn.send({"type": "ticket", **t.row(now, self.position(t))})
        elif op == "status":
            refs = msg.get("tickets")
            if refs:
                rows, errors = [], []
                for ref in refs if isinstance(refs, list) else [refs]:
                    t, error = self.resolve(ref)
                    if t is None:
                        errors.append(error)
                    else:
                        rows.append(t.row(now, self.position(t)))
                conn.send({"type": "tickets", "tickets": rows, "errors": errors})
            else:
                conn.send({"type": "status", **self.overview(now)})
        elif op == "list":
            conn.send({"type": "tickets", "tickets": self.listing(now, bool(msg.get("all"))), "errors": []})
        elif op == "pause":
            reason = msg.get("reason")
            self.pause(str(reason)[:300] if reason else None, now)
            conn.send({"type": "paused", "paused": self.paused})
        elif op == "resume":
            self.resume(now)
            conn.send({"type": "paused", "paused": None})
        elif op == "retry_statuses":
            repo = msg.get("repo")
            repo = str(repo) if isinstance(repo, str) and repo else None
            counts = self.retry_statuses(now, repo=repo)
            if self.status_posts:
                self.send_status_posts(now)
            conn.send({"type": "statuses", **counts})
        elif op == "ping":
            conn.send({"type": "pong", "version": VERSION, "runner": self.runner.name})
        else:
            conn.send({"type": "error", "error": f"unexpected op {op!r}"})

    async def handle(self, reader, writer) -> None:
        conn = Conn(writer)
        sock = writer.get_extra_info("socket")
        cred = common.peer_cred(sock) if sock is not None else None
        if cred is not None and cred[1] != UID:
            writer.close()  # only this user's processes may use the queue
            return
        conn.peer_pid = cred[0] if cred else None
        try:
            while not self.stopping:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError("not an object")
                except ValueError:
                    conn.send({"type": "error", "error": "each message is one JSON object on one line"})
                    continue
                try:
                    self.handle_message(msg, conn, time.time())
                except Exception:
                    common.log("request failed:\n" + traceback.format_exc())
                    conn.send({"type": "error", "error": "the daemon failed to handle that request; see its log"})
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            common.log("connection handler failed:\n" + traceback.format_exc())
        finally:
            conn.closed = True
            for conns in self.watchers.values():
                conns.discard(conn)
            try:
                writer.close()
            except Exception:
                pass


# --------------------------------------------------------------------------------------------
# Serving


def acquire_instance_lock(state_dir: str) -> int:
    """Acquire an exclusive non-blocking flock on `<state_dir>/buildqd.lock` before state recovery
    or socket binding so concurrent daemon starts never race on tickets, cgroups, or sockets."""
    common.ensure_private_dir(state_dir)
    dfd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open("buildqd.lock", flags, 0o600, dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != UID:
            os.close(fd)
            sys.exit(f"buildqd: {state_dir}/buildqd.lock is not a private regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        sys.exit(f"buildqd: another daemon already holds {state_dir}/buildqd.lock")
    except OSError as e:
        os.close(fd)
        sys.exit(f"buildqd: can't lock {state_dir}/buildqd.lock: {e}")
    return fd


def listening_socket(path: str, state_dir: str | None = None):
    """The socket systemd passed us (socket activation), or one we bind at `path`."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and int(os.environ.get("LISTEN_FDS") or 0) >= 1:
        for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
            os.environ.pop(name, None)
        sock = socket.socket(fileno=3)
        sock.setblocking(False)
        return sock, False
    parent = os.path.normpath(os.path.dirname(os.path.abspath(path)) or ".")
    if parent in common.fallback_runtime_dirs(state_dir):
        common.ensure_private_dir(parent)
    else:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as e:
            sys.exit(f"buildqd: socket directory {parent} isn't a real directory: {e}")
        try:
            pst = os.fstat(dfd)
            if not stat.S_ISDIR(pst.st_mode):
                sys.exit(f"buildqd: socket directory {parent} is not a directory")
            if pst.st_uid != UID and not (pst.st_mode & stat.S_ISVTX):
                sys.exit(f"buildqd: socket directory {parent} belongs to uid {pst.st_uid}, not {UID}")
        finally:
            os.close(dfd)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISSOCK(st.st_mode):
            sys.exit(f"buildqd: refusing to replace non-socket {path}")
        if st.st_uid != UID:
            sys.exit(f"buildqd: refusing to replace socket {path} owned by uid {st.st_uid}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(path)
            sys.exit(f"buildqd: something is already listening on {path}")
        except ConnectionRefusedError:
            pass
        except FileNotFoundError:
            st = None
        finally:
            probe.close()
        if st is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)  # mode 0600: the daemon runs with umask 077
    sock.listen(128)
    sock.setblocking(False)
    return sock, True


async def serve(args) -> None:
    lock_fd = acquire_instance_lock(args.state_dir)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    holder: dict = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.ensure_future(holder["daemon"].reload_async())
                            if "daemon" in holder else None)
    try:
        daemon = Daemon(args.config, args.state_dir)
    except RunnerUnavailableError as e:
        common.log(f"buildqd: {e}", level=3)
        os.close(lock_fd)
        sys.exit(f"buildqd: {e}")
    holder["daemon"] = daemon
    await asyncio.sleep(0.5)
    daemon.machine.sample(time.monotonic())  # a first CPU reading before the first decision
    now = time.time()
    daemon.load_tickets(now)
    daemon.prune(now)
    daemon.scan_flock_slots(now)
    sock, owned = listening_socket(args.socket, args.state_dir)
    pid_file = os.path.expanduser(args.pid_file) if getattr(args, "pid_file", None) else None
    my_ident = machine.proc_identity(os.getpid())
    my_start = my_ident[0] if my_ident else None
    my_boot = machine.boot_id()
    started_by = os.environ.get("BUILDQ_DAEMON_STARTED_BY") or "external"
    pid_in_state = False
    if pid_file:
        pid_parent = os.path.normpath(os.path.dirname(os.path.abspath(pid_file)) or ".")
        pid_in_state = pid_parent == os.path.normpath(os.path.abspath(args.state_dir))
        rec = json.dumps({"pid": os.getpid(), "start": my_start, "boot_id": my_boot,
                          "socket": os.path.abspath(args.socket), "started_by": started_by}) + "\n"
        common.write_private_file_atomic(pid_file, rec, enforce_parent_mode=pid_in_state)
    # Python 3.13+ removes a Unix server's socket file when the server closes. A socket systemd
    # passed us belongs to systemd and must outlive this process; ours we remove ourselves.
    keep = {"cleanup_socket": False} if sys.version_info >= (3, 13) else {}
    server = await asyncio.start_unix_server(daemon.handle, sock=sock, limit=16 << 20, **keep)
    ticker = asyncio.create_task(daemon.ticker())
    m = daemon.machine
    common.log(f"buildqd {VERSION} on {args.socket}: {m.ncpu} cores, {common.fmt_size(m.mem_total)} memory, "
        f"{daemon.runner.name} runner; configuration {json.dumps(daemon.cfg.summary())}")
    try:
        await stop.wait()
        daemon.stopping = True  # from here on nothing new starts
        daemon.thaw_all(time.time())
        daemon.save_queued(time.time())
        daemon.guard_slice(stopping=True)
        ticker.cancel()
        daemon.stop_memory_watch()
        daemon.queries.shutdown(wait=False, cancel_futures=True)
        server.close()
        if owned:
            try:
                os.unlink(args.socket)
            except OSError:
                pass
        if pid_file:
            try:
                cur = common.read_json(pid_file)
                if isinstance(cur, dict) and cur.get("pid") == os.getpid() and cur.get("start") == my_start:
                    os.unlink(pid_file)
            except OSError:
                pass
        common.log(f"stopping; {len(daemon.active())} running build(s) carry on, and the next daemon picks them up")
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="buildqd", description="The buildq daemon: a load-aware build queue.")
    parser.add_argument("--socket", default=None, help=f"Unix socket to listen on (default: {common.default_socket()})")
    parser.add_argument("--state-dir", default=common.default_state_dir(), help="tickets and history (default: %(default)s)")
    parser.add_argument("--config", default=common.default_config(), help="TOML configuration file (default: %(default)s)")
    parser.add_argument("--pid-file", help="write daemon PID to this file while running (for non-systemd init/daemon CLI)")
    parser.add_argument("--log-file", help="append timestamped logs to this file with rotation (for non-systemd runs)")
    parser.add_argument("--check-config", action="store_true", help="load the configuration, print it, and exit")
    parser.add_argument("--supervise", metavar="TICKET_DIR", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"buildqd {VERSION}")
    args = parser.parse_args(argv)
    if args.socket is None:
        args.socket = common.default_socket(state_dir=args.state_dir)
    os.umask(0o077)  # tickets, logs, history and the socket are private to this user
    if args.supervise:
        sys.exit(supervisor.supervise(args.supervise))
    if args.log_file and not args.check_config:
        common.configure_log_file(args.log_file, dup_stderr=True)
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    if args.check_config:
        m = Machine()
        cfg = config.load_config_file(args.config, m.mem_total, m.ncpu)
        wanted = os.environ.get("BUILDQ_RUNNER") or cfg.runner
        if wanted in ("auto", "systemd") and SystemdRunner.available():
            for lane in cfg.lanes.values():
                problem = SystemdRunner.check_lane(lane)
                if problem:
                    sys.exit(f"systemd refuses lanes.{lane.name}.properties: {problem}")
        else:
            # Read-only validation (no cgroup writes, no migration-ancestry check so `install.sh`
            # run from an interactive shell can validate `runner = "cgroup"` cleanly).
            if wanted == "cgroup" and not CgroupRunner.discover(cfg.cgroup_root, read_only=True, check_migration=False):
                sys.exit("cgroup runner needs a writable cgroup v2 directory with cpu and memory controllers "
                         "(set cgroup_root, run init/cgroup-setup.sh, or use runner = \"auto\")")
            for lane in cfg.lanes.values():
                problem = CgroupRunner.check_lane(lane, m.mem_total)
                if problem:
                    sys.exit(f"runner refuses lanes.{lane.name}.properties: {problem}")
        if cfg.gh and not (os.path.isfile(cfg.gh) and os.access(cfg.gh, os.X_OK)):
            # (a warning, not a refusal: the configuration loads, and statuses are tried when sent)
            print(f"buildqd: warning: gh = {cfg.gh!r} isn't an executable file; commit statuses can't be "
                  "sent until it is", file=sys.stderr)
        print(json.dumps(cfg.summary(), indent=2))
        return
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
