# SPDX-License-Identifier: MIT
"""Configuration loading and validation (config.toml, Lane, Config)."""

from __future__ import annotations

import os
import re
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    sys.exit("buildqd needs Python 3.11 or newer (for tomllib).")

from . import commands, common, tools as tool_registry
from .commands import NOT_SCRIPT_PATTERN, NOT_SCRIPTS

CI_LONG_WAITS = 12  # times starve_seconds a build may queue before it goes ahead of CI jobs' builds past ci_deadline

DEFAULTS = {
    "max_jobs": 4,
    "cpu_target": 0.9,
    "mem_floor": "10%",
    "disk_floor": "2G",
    # Where image builds write: Docker's root, containerd's image store, rootless Docker's, and
    # Podman's rootful and rootless stores (those that exist).
    "container_storage": ["/var/lib/docker", "/var/lib/containerd", "~/.local/share/docker",
                          "/var/lib/containers/storage", "~/.local/share/containers/storage"],
    "cpu_psi_block": 50.0,
    "mem_psi_block": 10.0,
    "ramp_seconds": 90,
    "starve_seconds": 600,
    "ci_first": False,
    "make_room_for_ci": "0",
    "ci_deadline": "0",
    "make_room_behind_agents": True,
    "make_room_spare_after": "0",
    "script_labels": [],
    "key_env": [],
    "restartable_scripts": [],
    "unwatched_seconds": 30,
    "abandoned_seconds": 600,
    "history_runs": 30,
    "history_days": 30,
    "history_keys": 2000,
    "mem_margin": 1.15,
    "max_log": "64M",
    "slice": "buildq.slice",
    "slice_guard": True,
    "cgroup_root": "",
    "runner": "auto",
    "default_lane": "default",
    "keep_finished": "7d",
    "max_finished": 1000,
    "on_finish": "",
    "shed_memory": True,
    "reuse_window": "2h",
    "github_status": False,
    "gh": "",
    "metrics": True,
    "release_after_peak": "shadow",
    "scale_mem_with_cores": "always",
    "env": {},
    "lanes": {"default": {"cores": 4, "properties": {"MemoryHigh": "40%", "MemoryMax": "50%"}}},
    "lane_rules": [],
    "flock_slots": [],
}
PROPERTY_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Lane:
    KEYS = {"cores", "min_cores", "max_cores", "mem", "min_mem", "properties", "may_pass_held", "jobserver",
            "exclusive", "timeout", "priority", "extra_jobs", "test_threads", "waits_for_cpu",
            "release_after_peak", "workspace_group"}

    def __init__(self, name: str, raw, mem_total: int, ncpu: int):
        if not isinstance(raw, dict):
            raise ValueError(f"lanes.{name} must be a table")
        unknown = set(raw) - self.KEYS
        if unknown:
            raise ValueError(f"lanes.{name}: unknown keys {sorted(unknown)}")
        self.name = name
        self.cores = common.number(raw.get("cores", 4), f"lanes.{name}.cores", 1, 4096, integer=True)
        self.min_cores = common.number(raw.get("min_cores", 1), f"lanes.{name}.min_cores", 1, 4096, integer=True)
        self.max_cores = common.number(raw.get("max_cores", max(self.cores, 1)), f"lanes.{name}.max_cores", 1, 4096,
                                integer=True)
        self.max_cores = min(self.max_cores, ncpu)
        self.cores = min(max(self.cores, self.min_cores), self.max_cores)
        if self.min_cores > self.max_cores:
            raise ValueError(f"lanes.{name}: min_cores is more than max_cores")
        if "mem" in raw:
            self.mem = common.parse_size(raw["mem"], mem_total)
        else:
            self.mem = min(4 * common.GIB, max(1, mem_total // 4))
        self.min_mem = common.parse_size(raw.get("min_mem", 0), mem_total)
        if not 0 < self.mem <= mem_total:
            raise ValueError(f"lanes.{name}.mem must be more than 0 and at most the machine's memory")
        if self.min_mem > mem_total:
            raise ValueError(f"lanes.{name}.min_mem is more than the machine's memory")
        properties = raw.get("properties") or {}
        if not isinstance(properties, dict):
            raise ValueError(f"lanes.{name}.properties must be a table")
        self.properties = {}
        for key, value in properties.items():
            if not PROPERTY_NAME.match(str(key)) or isinstance(value, (dict, list)) or "\n" in str(value):
                raise ValueError(f"lanes.{name}.properties: bad property {key!r}")
            self.properties[str(key)] = str(value).lower() if isinstance(value, bool) else str(value)
        self.may_pass_held = common.boolean(raw.get("may_pass_held", False), f"lanes.{name}.may_pass_held")
        # false: its builds start even when the CPU is at cpu_target, on the cores they need, and
        # the rebalancer takes the difference back from resizable builds. Memory still decides.
        self.waits_for_cpu = common.boolean(raw.get("waits_for_cpu", True), f"lanes.{name}.waits_for_cpu")
        # Off for builds whose memory is used outside their cgroup (docker build: dockerd does the work).
        self.release_after_peak = common.boolean(raw.get("release_after_peak", True), f"lanes.{name}.release_after_peak")
        mode = raw.get("jobserver", "tools")
        modes = {True: "all", "all": "all", "tools": "tools", False: "off", "off": "off"}
        if mode not in modes:
            raise ValueError(f"lanes.{name}.jobserver must be true, false or \"tools\"")
        self.jobserver = modes[mode]  # "tools": only for cargo, make and ninja, which are known to speak it
        self.exclusive = common.boolean(raw.get("exclusive", True), f"lanes.{name}.exclusive")
        # Exclusive lanes with the same group run one build per workspace between them (a cargo
        # check and a cargo test sharing a target folder would only wait on cargo's lock).
        group = raw.get("workspace_group", name)
        if not isinstance(group, str) or not group:
            raise ValueError(f"lanes.{name}.workspace_group must be a name")
        self.workspace_group = group
        self.group_given = "workspace_group" in raw and group != name  # (naming itself is the default)
        # RUST_TEST_THREADS and NEXTEST_TEST_THREADS win over a project's own settings (a suite
        # pinned to one thread for a shared database, say), so they are set only where asked.
        self.test_threads = common.boolean(raw.get("test_threads", False), f"lanes.{name}.test_threads")
        self.timeout = common.parse_duration(raw["timeout"]) if raw.get("timeout") else None
        self.priority = common.number(raw.get("priority", 0), f"lanes.{name}.priority", -1000, 1000, integer=True)
        self.extra_jobs = common.number(raw.get("extra_jobs", 0), f"lanes.{name}.extra_jobs", 0, 1000, integer=True)
        cap = self.properties.get("MemoryMax")
        try:
            self.mem_cap = common.parse_size(cap, mem_total) if cap and cap != "infinity" else None
        except ValueError:
            self.mem_cap = None

    def summary(self) -> dict:
        return {"cores": self.cores, "min_cores": self.min_cores, "max_cores": self.max_cores, "mem": self.mem,
                "min_mem": self.min_mem, "properties": self.properties, "may_pass_held": self.may_pass_held,
                "jobserver": self.jobserver, "exclusive": self.exclusive, "timeout": self.timeout,
                "priority": self.priority, "extra_jobs": self.extra_jobs, "waits_for_cpu": self.waits_for_cpu,
                "release_after_peak": self.release_after_peak, "workspace_group": self.workspace_group}


class Config:
    def __init__(self, raw: dict, mem_total: int, ncpu: int):
        if not isinstance(raw, dict):
            raise ValueError("the configuration must be a table")
        unknown = set(raw) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown keys {sorted(unknown)}")
        d = {**DEFAULTS, **raw}
        self.max_jobs = common.number(d["max_jobs"], "max_jobs", 1, 10000, integer=True)
        self.cpu_target = common.number(d["cpu_target"], "cpu_target", 0.05, 4)
        self.mem_floor = common.parse_size(d["mem_floor"], mem_total)
        if not 0 <= self.mem_floor < mem_total:
            raise ValueError("mem_floor must be less than the machine's memory")
        # A size, or a percentage of each build's own filesystem ("20%"), worked out where it runs.
        storage = d["container_storage"]
        if not isinstance(storage, list) or not all(isinstance(p, str) and p for p in storage):
            raise ValueError("container_storage must be a list of directories")
        self.container_storage = [os.path.expanduser(p) for p in storage]
        floor = d["disk_floor"]
        if isinstance(floor, str) and floor.strip().endswith("%"):
            try:
                share = float(floor.strip()[:-1])
            except ValueError:
                raise ValueError(f"disk_floor: not a percentage: {floor!r}") from None
            share = common.number(share, "disk_floor", 0, 99)
            self.disk_floor, self.disk_floor_share = 0, share / 100
        else:
            self.disk_floor, self.disk_floor_share = common.parse_size(floor), 0.0
        self.cpu_psi_block = common.number(d["cpu_psi_block"], "cpu_psi_block", 0, 1000)
        self.mem_psi_block = common.number(d["mem_psi_block"], "mem_psi_block", 0, 1000)
        self.ramp_seconds = common.number(d["ramp_seconds"], "ramp_seconds", 0, 3600)
        self.starve_seconds = common.number(d["starve_seconds"], "starve_seconds", 1, 7 * 86400)
        self.ci_first = common.boolean(d["ci_first"], "ci_first")
        room = d["make_room_for_ci"]
        self.make_room_for_ci = 0.0 if room in (0, "0", "", None, False) else common.parse_duration(room)
        due = d["ci_deadline"]
        self.ci_deadline = 0.0 if due in (0, "0", "", None, False) else common.parse_duration(due)
        if self.ci_deadline and not self.ci_first:
            raise ValueError("ci_deadline needs ci_first = true: it says when a CI job's build stops yielding to "
                             "builds that have waited longer")
        if self.ci_deadline and self.ci_deadline < self.starve_seconds:
            raise ValueError("ci_deadline must be at least starve_seconds: a CI job's build past it has waited out "
                             "its turn too, so its room and its workspace bind every lane")
        if self.ci_deadline and self.ci_deadline >= CI_LONG_WAITS * self.starve_seconds:
            raise ValueError(f"ci_deadline must be under {CI_LONG_WAITS} times starve_seconds: builds that have "
                             "queued that long go before CI jobs' builds again, which would undo it")
        if self.make_room_for_ci and not self.ci_first:
            raise ValueError("make_room_for_ci needs ci_first = true: without it a CI job's build holds no room, "
                             "so the memory freed for it could go to another build")
        # (#38) Whether a CI build waiting partly behind room held for agents' builds that have waited
        # longer may stop builds; and how long a build may have run before it is never stopped for one.
        self.make_room_behind_agents = common.boolean(d["make_room_behind_agents"], "make_room_behind_agents")
        spare = d["make_room_spare_after"]
        self.make_room_spare_after = 0.0 if spare in (0, "0", "", None, False) else common.parse_duration(spare)
        labels = d["script_labels"]
        if not isinstance(labels, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_.:+-]+", x)
                                                    for x in labels):
            raise ValueError("script_labels must be a list of script file names or package scripts, such as "
                             "[\"build.sh\", \"verify:push\"]")
        self.script_labels = frozenset(labels)
        key_env = d["key_env"]
        if not isinstance(key_env, list) or not all(isinstance(x, str) and ENV_NAME.fullmatch(x) for x in key_env):
            raise ValueError("key_env must be a list of environment variable names, such as [\"TEST_SUITE\"]")
        self.key_env = tuple(sorted(set(key_env)))
        scripts = d["restartable_scripts"]
        if not isinstance(scripts, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_.+-]+", x)
                                                     for x in scripts):
            raise ValueError("restartable_scripts must be a list of script file names, such as "
                             "[\"run-api-tests.sh\"]")
        disallowed = [x for x in scripts
                      if x in NOT_SCRIPTS or NOT_SCRIPT_PATTERN.fullmatch(x) or tool_registry.is_not_script(x)]
        if disallowed:
            raise ValueError(f"restartable_scripts names scripts, not tools: {', '.join(sorted(disallowed))} "
                             "would make anything run with them restartable")
        self.restartable_scripts = frozenset(scripts)
        self.unwatched_seconds = common.number(d["unwatched_seconds"], "unwatched_seconds", 1, 86400)
        self.abandoned_seconds = common.number(d["abandoned_seconds"], "abandoned_seconds", 0, 30 * 86400)
        self.history_runs = common.number(d["history_runs"], "history_runs", 1, 1000, integer=True)
        self.history_days = common.number(d["history_days"], "history_days", 1, 3650)
        self.history_keys = common.number(d["history_keys"], "history_keys", 10, 1_000_000, integer=True)
        self.mem_margin = common.number(d["mem_margin"], "mem_margin", 1, 10)
        self.max_log = common.parse_size(d["max_log"])
        self.slice = str(d["slice"])
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.slice", self.slice):
            raise ValueError(f"slice must be a systemd slice name ending in .slice: {self.slice!r}")
        self.slice_guard = common.boolean(d["slice_guard"], "slice_guard")
        cg_root = str(d["cgroup_root"] or "").strip()
        if cg_root:
            cg_norm = os.path.normpath(cg_root)
            if not os.path.isabs(cg_root) or not (cg_norm == "/sys/fs/cgroup" or cg_norm.startswith("/sys/fs/cgroup/")):
                raise ValueError(f"cgroup_root must be a directory under /sys/fs/cgroup: {cg_root!r}")
            cg_root = cg_norm
        self.cgroup_root = cg_root
        self.runner = str(d["runner"])
        if self.runner not in ("auto", "systemd", "cgroup", "direct"):
            raise ValueError("runner must be auto, systemd, cgroup or direct")
        self.keep_finished = common.parse_duration(d["keep_finished"])
        self.max_finished = common.number(d["max_finished"], "max_finished", 0, 1_000_000, integer=True)
        self.on_finish = str(d["on_finish"] or "")
        self.shed_memory = common.boolean(d["shed_memory"], "shed_memory")
        self.reuse_window = common.parse_duration(d["reuse_window"]) if d["reuse_window"] not in (0, "0", "", None) else 0.0
        self.github_status = common.boolean(d["github_status"], "github_status")
        # Only its form is checked here: a gh removed later (a version manager's upgrade) must not
        # make the whole configuration, lanes and all, fail to load. Whether it runs is found out
        # when a status is sent, and logged once.
        gh = os.path.expanduser(str(d["gh"] or ""))
        if gh and not os.path.isabs(gh):
            raise ValueError(f"gh must be a full path (~ is expanded): {gh!r}")
        self.gh = gh
        self.metrics = common.boolean(d["metrics"], "metrics")
        rap = d["release_after_peak"]
        self.release_after_peak = "shadow" if rap == "shadow" else common.boolean(rap, "release_after_peak")
        scale = d["scale_mem_with_cores"]
        scale = {True: "always", False: "never"}.get(scale, scale) if isinstance(scale, bool) else scale
        if scale not in ("always", "never"):
            raise ValueError("scale_mem_with_cores must be always or never")
        self.scale_mem_with_cores = scale
        if not isinstance(d["lanes"], dict) or not d["lanes"]:
            raise ValueError("lanes must define at least one lane")
        self.lanes = {str(n): Lane(str(n), lane, mem_total, ncpu) for n, lane in d["lanes"].items()}
        for lane in self.lanes.values():  # a group that names a lane must be that lane's group
            named = self.lanes.get(lane.workspace_group)
            if lane.group_given and named is not None and named is not lane and named.workspace_group != lane.workspace_group:
                raise ValueError(f"lanes.{lane.name}.workspace_group {lane.workspace_group!r}: lane {named.name}'s "
                                 f"group is {named.workspace_group!r}: use that")
        for lane in self.lanes.values():
            if not lane.group_given:
                continue
            others = [o for o in self.lanes.values() if o is not lane and o.workspace_group == lane.workspace_group]
            if not others:
                raise ValueError(f"lanes.{lane.name}.workspace_group {lane.workspace_group!r} is shared with no other "
                                 "lane (to join a lane's group, name that lane or its group)")
            if any(not o.exclusive for o in [lane, *others]):
                raise ValueError(f"lanes.{lane.name}.workspace_group {lane.workspace_group!r}: every lane in a group "
                                 "must be exclusive")
        self.default_lane = str(d["default_lane"])
        if self.default_lane not in self.lanes:
            raise ValueError(f"default_lane {self.default_lane!r} is not in lanes {sorted(self.lanes)}")
        if not isinstance(d["env"], dict):
            raise ValueError("env must be a table")
        self.env = {}
        for key, value in d["env"].items():
            if not ENV_NAME.match(str(key)) or isinstance(value, (dict, list)):
                raise ValueError(f"env: bad variable {key!r}")
            if key in DEFAULTS:
                raise ValueError(f"env.{key} is a top-level setting written after [env]; move it above the "
                                 "first [table] of the file")
            self.env[str(key)] = str(value)
        if not isinstance(d["lane_rules"], list):
            raise ValueError("lane_rules must be a list of tables")
        self.lane_rules = []
        for entry in d["lane_rules"]:
            if not isinstance(entry, dict) or "match" not in entry or "lane" not in entry:
                raise ValueError("each lane_rules entry needs a match and a lane")
            if set(entry) - {"match", "lane"}:
                raise ValueError(f"lane_rules: unknown keys {sorted(set(entry) - {'match', 'lane'})} "
                                 "(a top-level key written after a [[lane_rules]] entry lands in it)")
            if str(entry["lane"]) not in self.lanes:
                raise ValueError(f"lane_rules: lane {entry['lane']!r} is not in lanes")
            try:
                self.lane_rules.append((re.compile(str(entry["match"])), str(entry["lane"])))
            except re.error as e:
                raise ValueError(f"lane_rules: bad pattern {entry['match']!r}: {e}") from e
        if not isinstance(d["flock_slots"], list):
            raise ValueError("flock_slots must be a list of tables")
        self.flock_slots = []
        for entry in d["flock_slots"]:
            if not isinstance(entry, dict) or "glob" not in entry:
                raise ValueError("each flock_slots entry needs a glob")
            if set(entry) - {"glob", "lane"}:
                raise ValueError(f"flock_slots: unknown keys {sorted(set(entry) - {'glob', 'lane'})}")
            lane = str(entry.get("lane", self.default_lane))
            if lane not in self.lanes:
                raise ValueError(f"flock_slots: lane {lane!r} is not in lanes")
            self.flock_slots.append((os.path.expanduser(str(entry["glob"])), lane))

    def summary(self) -> dict:
        return {
            "max_jobs": self.max_jobs, "cpu_target": self.cpu_target, "mem_floor": self.mem_floor,
            "disk_floor": f"{self.disk_floor_share * 100:g}%" if self.disk_floor_share else self.disk_floor,
            "cpu_psi_block": self.cpu_psi_block, "mem_psi_block": self.mem_psi_block,
            "ramp_seconds": self.ramp_seconds, "starve_seconds": self.starve_seconds, "ci_first": self.ci_first,
            "make_room_for_ci": self.make_room_for_ci, "make_room_behind_agents": self.make_room_behind_agents,
            "ci_deadline": self.ci_deadline,
            "scale_mem_with_cores": self.scale_mem_with_cores,
            "make_room_spare_after": self.make_room_spare_after, "script_labels": sorted(self.script_labels),
            "key_env": list(self.key_env),
            "restartable_scripts": sorted(self.restartable_scripts),
            "slice": self.slice,
            "slice_guard": self.slice_guard,
            "cgroup_root": self.cgroup_root,
            "runner": self.runner, "default_lane": self.default_lane, "keep_finished": self.keep_finished,
            "max_log": self.max_log, "shed_memory": self.shed_memory, "reuse_window": self.reuse_window,
            "github_status": self.github_status, "gh": self.gh, "metrics": self.metrics,
            "release_after_peak": self.release_after_peak,
            "lanes": {name: lane.summary() for name, lane in self.lanes.items()},
            "lane_rules": [[rule.pattern, lane] for rule, lane in self.lane_rules],
            "env": self.env,
        }

    def lane_for(self, argv: list[str]) -> str:
        """The lane a command goes to when its caller names none: the first rule that matches
        its key words (`cargo test -p api`, `pnpm --filter web build`, ...), else default_lane."""
        words = " ".join(commands.command_words(argv, self.script_labels))
        for rule, lane in self.lane_rules:
            if rule.search(words):
                return lane
        return self.default_lane


def load_config_file(path: str, mem_total: int, ncpu: int) -> Config:
    raw = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    return Config(raw, mem_total, ncpu)

