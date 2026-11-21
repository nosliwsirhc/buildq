# SPDX-License-Identifier: MIT
"""Tool and compiler registry: keys, parallelism env, argv rewrites, restart/reuse, and disk dirs."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
import os
import re
from types import MappingProxyType

SCRIPT_WORD = re.compile(r"[a-z][a-z_-]{0,23}")
CREDENTIAL_OR_ASSIGN = re.compile(r"://|^[^@\s/]+:[^@\s]*@|^[^-][^=]*=")
CARGO_VALUE_OPTIONS = frozenset({
    "--manifest-path", "--target-dir", "-j", "--jobs", "--color", "--config",
    "-Z", "--profile", "--target", "-F", "--features", "--bin", "--test",
    "--example", "--bench", "-p", "--package", "--exclude", "--message-format",
})
JS_VALUE_OPTIONS = frozenset({"-C", "--dir", "--prefix", "--filter", "-F", "--workspace", "--cwd"})
VERIFY_CARGO = frozenset({"check", "c", "clippy", "test", "t", "nextest"})
RESTART_CARGO = frozenset({"build", "b", "check", "c", "clippy", "test", "t", "doc", "d", "nextest", "bench"})


def redact_args(args: list[str]) -> list[str]:
    """Replace URLs, user:pass@, and plain assignments so credentials never enter keys or subcommands."""
    return ["…" if CREDENTIAL_OR_ASSIGN.search(a) else a for a in args]


def positional_items(args: list[str], value_options: Collection[str] = frozenset()) -> list[tuple[int, str]]:
    """(index, arg) of non-option args; bin/buildq-shim keeps a standalone zero-import copy checked by unit tests."""
    out, skip = [], False
    for i, arg in enumerate(args):
        if skip:
            skip = False
        elif arg.startswith("-"):
            skip = arg in value_options
        else:
            out.append((i, arg))
    return out


def positionals(args: list[str], value_options: Collection[str] = frozenset()) -> list[str]:
    return [arg for _, arg in positional_items(args, value_options)]


def option_values(args: list[str], names: Collection[str]) -> list[str]:
    values = []
    for i, arg in enumerate(args):
        if arg in names:
            if i + 1 < len(args):
                values.append(args[i + 1])
            continue
        for name in names:
            if name.startswith("--") and arg.startswith(name + "="):
                values.append(arg[len(name) + 1:])
            elif len(name) == 2 and not arg.startswith("--") and arg.startswith(name) and len(arg) > 2:
                values.append(arg[2:])
    return values


def asks_for_jobs(args: list[str]) -> bool:
    return any(a == "-j" or re.fullmatch(r"-j\d+", a) or a == "--jobs" or a.startswith("--jobs=") for a in args)


def without_jobs(argv: list[str]) -> list[str]:
    """argv with make's or ninja's -j/--jobs options removed, so the build's jobserver decides."""
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in ("-j", "--jobs"):
            skip = i + 1 < len(argv) and argv[i + 1].isdigit()
            continue
        if re.fullmatch(r"-j\d+", a) or a.startswith("--jobs="):
            continue
        out.append(a)
    return out


def jobserver_flags(want: int, jobserver: str) -> str:
    """GNU make 4.4+ / cargo / ninja FIFO jobserver flags."""
    return f"-j{want} --jobserver-auth=fifo:{jobserver}"


def no_emit(args: list[str]) -> bool:
    """Whether tsc's --noEmit is in effect at the end of its arguments (`--noEmit false` turns it off)."""
    on, i = False, 0
    while i < len(args):
        arg = args[i]
        if arg == "--noEmit":
            on = True
            if i + 1 < len(args) and args[i + 1] in ("true", "false"):
                on, i = args[i + 1] == "true", i + 1
        elif arg.startswith("--noEmit="):
            on = arg.split("=", 1)[1] == "true"
        i += 1
    return on


@dataclass(frozen=True)
class ToolSpec:
    """Declarative specification of how buildq classifies, sizes, and isolates a compiler or tool."""

    names: tuple[str, ...]
    # Optional regex matched as a fallback in lookup() for versioned tool binaries (e.g. gcc-16).
    name_pattern: re.Pattern[str] | None = None
    honors_grant: bool = False
    jobserver: bool = False
    makeflags_jobserver: bool = False
    container_storage: bool = False
    # True when these names must be rejected in `restartable_scripts` config validation.
    not_script: bool = True
    # Env var names whose values this tool explicitly merges with `caller_env`; exempted from
    # scheduler.start()'s `given` filter only when this spec's callback actually emits the key.
    merge_env_vars: tuple[str, ...] = ()
    command_words: Callable[[str, list[str], Collection[str]], list[str]] | None = None
    restart_safe: Callable[[str, list[str]], bool] | None = None
    reusable_check: Callable[[str, list[str]], bool] | None = None
    # Exported to every build (so wrapper scripts invoking this tool inherit core limits).
    global_env: Callable[[int, int, str | None, bool, dict | None], dict[str, str]] | None = None
    # Exported only when this tool is the direct command (e.g. MAKEFLAGS for make -j / ninja).
    direct_env: Callable[[str, list[str], int, int, str | None, bool, dict | None], dict[str, str]] | None = None
    # Optional args rewrite when this tool is the direct command; receives only the tool's own
    # args (without executable or wrappers) and returns replacement args or None.
    rewrite_argv: Callable[[str, list[str], int, str | None, bool, dict | None], list[str] | None] | None = None
    # Extra output directories to watch in `buildq top` when this tool runs (receives tool args).
    write_dirs: Callable[[str, list[str], str, dict], list[str]] | None = None
    # Whether this invocation looks like a cold build from scratch (receives tool args).
    is_cold: Callable[[str, list[str], str, dict], bool] | None = None


def build_registry(specs: Collection[ToolSpec]) -> Mapping[str, ToolSpec]:
    registry: dict[str, ToolSpec] = {}
    for spec in specs:
        for name in spec.names:
            if name in registry:
                raise ValueError(f"tool {name!r} is already registered")
            registry[name] = spec
    return MappingProxyType(registry)


# ---- Per-tool implementations ---------------------------------------------------------------


def _cargo_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    words = [tool]
    if rest and rest[0].startswith("+"):
        rest = rest[1:]
    words += positionals(rest, CARGO_VALUE_OPTIONS)[:1]
    words += [f"-p {p}" for p in sorted(set(option_values(rest, {"-p", "--package"})))]
    words += [f"--profile {p}" for p in option_values(rest, {"--profile"})]
    words += [f for f in ("--release", "--workspace", "--all-targets", "--no-run", "--lib", "--doc")
              if f in rest]
    return words


def _cargo_subcommand(args: list[str]) -> str:
    before = args[:args.index("--")] if "--" in args else args
    words = _cargo_words("cargo", redact_args(before), ())
    return words[1] if len(words) > 1 else ""


def _cargo_restart_safe(_tool: str, args: list[str]) -> bool:
    return _cargo_subcommand(args) in RESTART_CARGO


def _cargo_reusable_check(_tool: str, args: list[str]) -> bool:
    sub = _cargo_subcommand(args)
    return sub in VERIFY_CARGO and (sub != "nextest" or "run" in args)


def _cargo_global_env(granted: int, want: int, jobserver: str | None, test_threads: bool,
                      _caller_env: dict | None) -> dict[str, str]:
    g = str(granted)
    values: dict[str, str] = {}
    if test_threads:
        values["RUST_TEST_THREADS"] = g
        values["NEXTEST_TEST_THREADS"] = g
    if jobserver:
        # cargo reads CARGO_MAKEFLAGS first and hands its jobserver to build scripts.
        values["CARGO_MAKEFLAGS"] = jobserver_flags(want, jobserver)
    else:
        values["CARGO_BUILD_JOBS"] = g  # no tokens to bound it, so cargo is told the number
    return values


def _cargo_write_dirs(_tool: str, _args: list[str], cwd: str, env: dict) -> list[str]:
    if env.get("CARGO_TARGET_DIR"):
        return []  # already tracked globally in commands.write_dirs_of
    here = os.path.abspath(cwd)
    for _ in range(12):
        if os.path.exists(os.path.join(here, "Cargo.lock")):
            return [os.path.join(here, "target")]
        if os.path.dirname(here) == here:
            break
        here = os.path.dirname(here)
    return []


def _cargo_is_cold(_tool: str, args: list[str], cwd: str, env: dict) -> bool:
    """Whether a cargo command will build from scratch: its target directory has no build of that
    profile yet. History of warm builds says little about a cold one."""
    target = env.get("CARGO_TARGET_DIR")
    if target:
        target = os.path.join(cwd, target)
    else:
        d = os.path.abspath(cwd)
        root = None
        while True:
            if os.path.exists(os.path.join(d, "Cargo.lock")):
                root = d
                break
            if os.path.exists(os.path.join(d, "Cargo.toml")):
                root = root or d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        if root is None:
            return False
        target = os.path.join(root, "target")
    profile = "release" if "--release" in args else "debug"
    profiles = option_values(args, {"--profile"})
    if profiles:
        profile = {"dev": "debug", "test": "debug", "bench": "release"}.get(profiles[0], profiles[0])
    return not os.path.isdir(os.path.join(target, profile))


def _make_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    return [tool] + [a for a in positionals(rest, {"-C", "-f", "-j", "-l", "-o", "-W"}) if "=" not in a][:2]


def _makeflags_env(granted: int, want: int, jobserver: str | None) -> dict[str, str]:
    return {"MAKEFLAGS": jobserver_flags(want, jobserver) if jobserver else f"-j{granted}"}


def _make_direct_env(_tool: str, args: list[str], granted: int, want: int, jobserver: str | None,
                     _test_threads: bool, _caller_env: dict | None) -> dict[str, str]:
    # make runs in parallel only when asked (-j): a Makefile not written for it can break.
    return _makeflags_env(granted, want, jobserver) if asks_for_jobs(args) else {}


def _make_rewrite_argv(_tool: str, args: list[str], _granted: int, jobserver: str | None,
                       _test_threads: bool, _caller_env: dict | None) -> list[str] | None:
    if jobserver and asks_for_jobs(args):
        return without_jobs(args)  # its -j would make it ignore the jobserver
    return None


def _ninja_direct_env(_tool: str, _args: list[str], granted: int, want: int, jobserver: str | None,
                      _test_threads: bool, _caller_env: dict | None) -> dict[str, str]:
    # ninja is parallel by design.
    return _makeflags_env(granted, want, jobserver)


def _ninja_restart_safe(_tool: str, args: list[str]) -> bool:
    # a custom target (install, deploy) might do anything
    return set(positionals(args, {"-C", "-f", "-j", "-k", "-l", "-d", "-t", "-w"})) <= {"all", "test", "check"}


def _dotnet_restart_safe(_tool: str, args: list[str]) -> bool:
    return positionals(args)[:1] in (["build"], ["test"])


def _dotnet_reusable_check(_tool: str, args: list[str]) -> bool:
    if any(re.match(r"-[ld](:|$)", a) or re.search(r"CollectCoverage", a) for a in args):
        return False  # dotnet test's short --logger and --diag, and coverage through MSBuild
    return positionals(args)[:1] == ["test"]


def _dotnet_global_env(granted: int, _want: int, _jobserver: str | None, _test_threads: bool,
                       _caller_env: dict | None) -> dict[str, str]:
    # MSBuild sizes its worker nodes from the processor count, and node reuse leaves workers behind
    # after the build; the shared compiler server would compile outside the build's cgroup.
    return {"DOTNET_PROCESSOR_COUNT": str(granted), "MSBUILDDISABLENODEREUSE": "1",
            "UseSharedCompilation": "false"}


def _js_words(tool: str, rest: list[str], labels: Collection[str]) -> list[str]:
    words = [tool]
    words += [f"--filter {f}" for f in sorted(set(option_values(rest, {"--filter", "-F"})))]
    items = positional_items(rest, JS_VALUE_OPTIONS)
    verb = items[0][1] if items and items[0][1] in ("run", "run-script", "exec", "x", "dlx") else None
    if verb:
        items = items[1:]
    words += [arg for _, arg in items[:1]]
    # A package script in script_labels takes a label too (`verify:push docs`): the word right after
    # it, not an option's value; only a script it runs, not a binary (exec, x, dlx).
    if items and items[0][1] in labels and verb not in ("exec", "x", "dlx"):
        after = rest[items[0][0] + 1:][:1]
        if after and SCRIPT_WORD.fullmatch(after[0]):
            words.append(after[0])
    return words


def _vitest_reusable_check(_tool: str, args: list[str]) -> bool:
    if any(re.fullmatch(r"-[A-Za-z]*[uw][A-Za-z]*(=.*)?", a) for a in args):
        return False  # -u or -w grouped with other flags (-ut adds: updates snapshots) (#50)
    return positionals(args)[:1] == ["run"]


def _git_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    return [tool] + positionals(rest, {"-C", "-c", "--git-dir", "--work-tree"})[:1]


# ---- Built-in registrations -----------------------------------------------------------------

JS_TOOLS = frozenset({"npm", "pnpm", "yarn", "npx", "pnpx", "bunx"})

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        names=("cargo",),
        honors_grant=True,
        jobserver=True,
        command_words=_cargo_words,
        restart_safe=_cargo_restart_safe,
        reusable_check=_cargo_reusable_check,
        global_env=_cargo_global_env,
        write_dirs=_cargo_write_dirs,
        is_cold=_cargo_is_cold,
    ),
    ToolSpec(
        names=("make", "gmake"),
        honors_grant=True,
        jobserver=True,
        makeflags_jobserver=True,
        command_words=_make_words,
        direct_env=_make_direct_env,
        rewrite_argv=_make_rewrite_argv,
    ),
    ToolSpec(
        names=("ninja",),
        honors_grant=True,
        jobserver=True,
        makeflags_jobserver=True,
        command_words=_make_words,
        restart_safe=_ninja_restart_safe,
        direct_env=_ninja_direct_env,
        rewrite_argv=_make_rewrite_argv,
    ),
    ToolSpec(
        names=("dotnet",),
        honors_grant=True,
        restart_safe=_dotnet_restart_safe,
        reusable_check=_dotnet_reusable_check,
        global_env=_dotnet_global_env,
    ),
    ToolSpec(
        names=("cmake",),
        honors_grant=True,
        global_env=lambda g, _w, _js, _tt, _env: {"CMAKE_BUILD_PARALLEL_LEVEL": str(g)},
    ),
    ToolSpec(
        names=("go",),
        honors_grant=True,
        global_env=lambda g, _w, _js, _tt, _env: {"GOMAXPROCS": str(g)},
    ),
    ToolSpec(
        names=tuple(sorted(JS_TOOLS)),
        command_words=_js_words,
    ),
    ToolSpec(
        names=("tsc", "vue-tsc"),
        not_script=False,  # kept out of NOT_SCRIPTS for backwards config.toml compatibility
        restart_safe=lambda _t, _a: True,
        reusable_check=lambda _t, a: no_emit(a),
    ),
    ToolSpec(
        names=("eslint",),
        not_script=False,  # kept out of NOT_SCRIPTS for backwards config.toml compatibility
        restart_safe=lambda _t, _a: True,
        reusable_check=lambda _t, _a: True,
    ),
    ToolSpec(
        names=("vitest",),
        not_script=False,  # kept out of NOT_SCRIPTS for backwards config.toml compatibility
        restart_safe=lambda _t, _a: True,
        reusable_check=_vitest_reusable_check,
    ),
    ToolSpec(
        names=("git",),
        command_words=_git_words,
    ),
    ToolSpec(
        names=("docker", "podman", "buildah"),
        container_storage=True,
    ),
)

REGISTRY: Mapping[str, ToolSpec] = build_registry(SPECS)
_PATTERN_SPECS: tuple[ToolSpec, ...] = tuple(s for s in SPECS if s.name_pattern is not None)
JOBSERVER_TOOLS: frozenset[str] = frozenset(n for s in SPECS if s.jobserver for n in s.names)
MAKEFLAGS_JOBSERVER_TOOLS: frozenset[str] = frozenset(n for s in SPECS if s.makeflags_jobserver for n in s.names)
HONORS_GRANT_TOOLS: frozenset[str] = frozenset(n for s in SPECS if s.honors_grant for n in s.names)
CONTAINER_TOOLS: frozenset[str] = frozenset(n for s in SPECS if s.container_storage for n in s.names)
NOT_SCRIPT_TOOLS: frozenset[str] = frozenset(n for s in SPECS if s.not_script for n in s.names)


def lookup(tool: str) -> ToolSpec | None:
    spec = REGISTRY.get(tool)
    if spec is not None:
        return spec
    for spec in _PATTERN_SPECS:
        if spec.name_pattern and spec.name_pattern.fullmatch(tool):
            return spec
    return None


# ---- Dispatch helpers (distinct names from commands.* to avoid sim.py facade collisions) ----


def honors_grant_tool(tool: str) -> bool:
    spec = lookup(tool)
    return bool(spec and spec.honors_grant)


def is_not_script(tool: str) -> bool:
    spec = lookup(tool)
    return bool(spec and spec.not_script)


def uses_jobserver(tool: str) -> bool:
    spec = lookup(tool)
    return bool(spec and spec.jobserver)


def uses_makeflags_jobserver(tool: str) -> bool:
    spec = lookup(tool)
    return bool(spec and spec.makeflags_jobserver)


def uses_container_storage(tool: str) -> bool:
    spec = lookup(tool)
    return bool(spec and spec.container_storage)


def words_for(tool: str, rest: list[str], labels: Collection[str] = ()) -> list[str] | None:
    spec = lookup(tool)
    if spec and spec.command_words:
        return spec.command_words(tool, rest, labels)
    return None


def is_restart_safe(tool: str, args: list[str]) -> bool:
    spec = lookup(tool)
    return bool(spec and spec.restart_safe and spec.restart_safe(tool, args))


def is_reusable_check(tool: str, args: list[str]) -> bool:
    spec = lookup(tool)
    return bool(spec and spec.reusable_check and spec.reusable_check(tool, args))


def env_with_merged(tool: str, args: list[str], granted: int, want: int, jobserver: str | None,
                    test_threads: bool, caller_env: dict | None = None) -> tuple[dict[str, str], frozenset[str]]:
    """Parallelism and cgroup-containment env vars plus the exact subset of keys explicitly merged
    with `caller_env` by the callback that emitted them."""
    values: dict[str, str] = {"BUILDQ_CORES": str(granted)}
    merged: set[str] = set()
    for spec in SPECS:
        if spec.global_env:
            emitted = spec.global_env(granted, want, jobserver, test_threads, caller_env)
            for k, v in emitted.items():
                values[k] = v
                if k in spec.merge_env_vars:
                    merged.add(k)
                else:
                    merged.discard(k)
    direct = lookup(tool)
    if direct and direct.direct_env:
        emitted = direct.direct_env(tool, args, granted, want, jobserver, test_threads, caller_env)
        for k, v in emitted.items():
            values[k] = v
            if k in direct.merge_env_vars:
                merged.add(k)
            else:
                merged.discard(k)
    return values, frozenset(merged)


def env_for(tool: str, args: list[str], granted: int, want: int, jobserver: str | None,
            test_threads: bool, caller_env: dict | None = None) -> dict[str, str]:
    """Parallelism and cgroup-containment env vars for a build."""
    return env_with_merged(tool, args, granted, want, jobserver, test_threads, caller_env)[0]


def rewrite_argv_for(tool: str, argv: list[str], tool_at: int, granted: int, jobserver: str | None,
                     test_threads: bool = False, caller_env: dict | None = None) -> list[str] | None:
    if tool_at < 0 or tool_at >= len(argv):
        return None
    spec = lookup(tool)
    if not (spec and spec.rewrite_argv):
        return None
    original = argv[tool_at + 1:]
    rewritten = spec.rewrite_argv(tool, list(original), granted, jobserver, test_threads, caller_env)
    if rewritten is None or rewritten == original:
        return None
    return argv[:tool_at + 1] + rewritten


def write_dirs_for(tool: str, args: list[str], cwd: str, env: dict) -> list[str]:
    spec = lookup(tool)
    if spec and spec.write_dirs:
        return spec.write_dirs(tool, args, cwd, env)
    return []


def is_cold_for(tool: str, args: list[str], cwd: str, env: dict) -> bool:
    spec = lookup(tool)
    if spec and spec.is_cold:
        return spec.is_cold(tool, args, cwd, env)
    return False
