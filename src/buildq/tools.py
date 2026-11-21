# SPDX-License-Identifier: MIT
"""Tool and compiler registry: keys, parallelism env, argv rewrites, restart/reuse, and disk dirs."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
import os
import re
from types import MappingProxyType

SCRIPT_WORD = re.compile(r"[a-z][a-z_-]{0,23}")
CARGO_VALUE_OPTIONS = frozenset({
    "--manifest-path", "--target-dir", "-j", "--jobs", "--color", "--config",
    "-Z", "--profile", "--target", "-F", "--features", "--bin", "--test",
    "--example", "--bench", "-p", "--package", "--exclude", "--message-format",
})
JS_VALUE_OPTIONS = frozenset({"-C", "--dir", "--prefix", "--filter", "-F", "--workspace", "--cwd"})
VERIFY_CARGO = frozenset({"check", "c", "clippy", "test", "t", "nextest"})
RESTART_CARGO = frozenset({"build", "b", "check", "c", "clippy", "test", "t", "doc", "d", "nextest", "bench"})


def positionals(args: list[str], value_options: Collection[str] = frozenset()) -> list[str]:
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg.startswith("-"):
            skip = arg in value_options
        else:
            out.append(arg)
    return out


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
    honors_grant: bool = False
    jobserver: bool = False
    makeflags_jobserver: bool = False
    command_words: Callable[[str, list[str], Collection[str]], list[str]] | None = None
    restart_safe: Callable[[str, list[str]], bool] | None = None
    reusable_check: Callable[[str, list[str]], bool] | None = None
    # Exported to every build (so wrapper scripts invoking this tool inherit core limits); receives
    # `caller_env` so a tool can merge into an existing caller variable without clobbering it.
    global_env: Callable[[int, int, str | None, bool, dict | None], dict[str, str]] | None = None
    # Exported only when this tool is the direct command (e.g. MAKEFLAGS for make -j / ninja).
    direct_env: Callable[[str, list[str], int, int, str | None, bool, dict | None], dict[str, str]] | None = None
    # Optional argv adjustment when this tool is the direct command (e.g. stripping -j for jobserver).
    rewrite_argv: Callable[[str, list[str], int, int, str | None], list[str] | None] | None = None
    # Extra output directories to watch in `buildq top` when this tool runs.
    write_dirs: Callable[[str, list[str], str, dict], list[str]] | None = None
    # Whether this invocation looks like a cold build from scratch.
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
    words = _cargo_words("cargo", before, ())
    return words[1] if len(words) > 1 else ""


def _cargo_restart_safe(_tool: str, args: list[str]) -> bool:
    return _cargo_subcommand(args) in RESTART_CARGO


def _cargo_reusable_check(_tool: str, args: list[str]) -> bool:
    sub = _cargo_subcommand(args)
    return sub in VERIFY_CARGO and (sub != "nextest" or "run" in args)


def _cargo_global_env(granted: int, want: int, jobserver: str | None, test_threads: bool,
                      _caller_env: dict | None) -> dict[str, str]:
    g, w = str(granted), str(want)
    values: dict[str, str] = {}
    if test_threads:
        values["RUST_TEST_THREADS"] = g
        values["NEXTEST_TEST_THREADS"] = g
    if jobserver:
        # cargo reads CARGO_MAKEFLAGS first and hands its jobserver to build scripts.
        values["CARGO_MAKEFLAGS"] = f"-j{w} --jobserver-auth=fifo:{jobserver}"
    else:
        values["CARGO_BUILD_JOBS"] = g  # no tokens to bound it, so cargo is told the number
    return values


def _cargo_write_dirs(_tool: str, _argv: list[str], cwd: str, env: dict) -> list[str]:
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


def _cargo_is_cold(_tool: str, argv: list[str], cwd: str, env: dict) -> bool:
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
    profile = "release" if "--release" in argv else "debug"
    profiles = option_values(argv, {"--profile"})
    if profiles:
        profile = {"dev": "debug", "test": "debug", "bench": "release"}.get(profiles[0], profiles[0])
    return not os.path.isdir(os.path.join(target, profile))


def _make_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    return [tool] + [a for a in positionals(rest, {"-C", "-f", "-j", "-l", "-o", "-W"}) if "=" not in a][:2]


def _make_direct_env(_tool: str, args: list[str], granted: int, want: int, jobserver: str | None,
                     _test_threads: bool, _caller_env: dict | None) -> dict[str, str]:
    # make runs in parallel only when asked (-j): a Makefile not written for it can break.
    if not asks_for_jobs(args):
        return {}
    if jobserver:
        return {"MAKEFLAGS": f"-j{want} --jobserver-auth=fifo:{jobserver}"}
    return {"MAKEFLAGS": f"-j{granted}"}


def _make_rewrite_argv(_tool: str, argv: list[str], tool_at: int, _granted: int,
                       jobserver: str | None) -> list[str] | None:
    if jobserver and asks_for_jobs(argv[tool_at + 1:]):
        return argv[:tool_at] + without_jobs(argv[tool_at:])  # its -j would make it ignore the jobserver
    return None


def _ninja_direct_env(_tool: str, _args: list[str], granted: int, want: int, jobserver: str | None,
                      _test_threads: bool, _caller_env: dict | None) -> dict[str, str]:
    # ninja is parallel by design.
    if jobserver:
        return {"MAKEFLAGS": f"-j{want} --jobserver-auth=fifo:{jobserver}"}
    return {"MAKEFLAGS": f"-j{granted}"}


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
    at, skip = [], False  # where each positional word is (not an option's value)
    for i, arg in enumerate(rest):
        if skip:
            skip = False
        elif arg.startswith("-"):
            skip = arg in JS_VALUE_OPTIONS
        else:
            at.append(i)
    verb = rest[at[0]] if at and rest[at[0]] in ("run", "run-script", "exec", "x", "dlx") else None
    if verb:
        at = at[1:]
    words += [rest[i] for i in at[:1]]
    # A package script in script_labels takes a label too (`verify:push docs`): the word right after
    # it, not an option's value; only a script it runs, not a binary (exec, x, dlx).
    if at and rest[at[0]] in labels and verb not in ("exec", "x", "dlx"):
        after = rest[at[0] + 1:][:1]
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
        restart_safe=lambda _t, _a: True,
        reusable_check=lambda _t, a: no_emit(a),
    ),
    ToolSpec(
        names=("eslint",),
        restart_safe=lambda _t, _a: True,
        reusable_check=lambda _t, _a: True,
    ),
    ToolSpec(
        names=("vitest",),
        restart_safe=lambda _t, _a: True,
        reusable_check=_vitest_reusable_check,
    ),
    ToolSpec(
        names=("git",),
        command_words=_git_words,
    ),
    ToolSpec(
        names=("docker", "podman", "buildah"),
    ),
)

REGISTRY: Mapping[str, ToolSpec] = build_registry(SPECS)
JOBSERVER_TOOLS: frozenset[str] = frozenset(n for s in SPECS if s.jobserver for n in s.names)
MAKEFLAGS_JOBSERVER_TOOLS: frozenset[str] = frozenset(n for s in SPECS if s.makeflags_jobserver for n in s.names)
HONORS_GRANT_TOOLS: frozenset[str] = frozenset(n for s in SPECS if s.honors_grant for n in s.names)
REGISTERED_TOOL_NAMES: frozenset[str] = frozenset(REGISTRY)


def lookup(tool: str) -> ToolSpec | None:
    return REGISTRY.get(tool)


# ---- Dispatch helpers (distinct names from commands.* to avoid sim.py facade collisions) ----


def words_for(tool: str, rest: list[str], labels: Collection[str] = ()) -> list[str] | None:
    spec = REGISTRY.get(tool)
    if spec and spec.command_words:
        return spec.command_words(tool, rest, labels)
    return None


def is_restart_safe(tool: str, args: list[str]) -> bool:
    spec = REGISTRY.get(tool)
    return bool(spec and spec.restart_safe and spec.restart_safe(tool, args))


def is_reusable_check(tool: str, args: list[str]) -> bool:
    spec = REGISTRY.get(tool)
    return bool(spec and spec.reusable_check and spec.reusable_check(tool, args))


def env_for(tool: str, args: list[str], granted: int, want: int, jobserver: str | None,
            test_threads: bool, caller_env: dict | None = None) -> dict[str, str]:
    """Parallelism and cgroup-containment env vars for a build (caller's own settings win later)."""
    values: dict[str, str] = {"BUILDQ_CORES": str(granted)}
    for spec in SPECS:
        if spec.global_env:
            values.update(spec.global_env(granted, want, jobserver, test_threads, caller_env))
    direct = REGISTRY.get(tool)
    if direct and direct.direct_env:
        values.update(direct.direct_env(tool, args, granted, want, jobserver, test_threads, caller_env))
    return values


def rewrite_argv_for(tool: str, argv: list[str], tool_at: int, granted: int,
                     jobserver: str | None) -> list[str] | None:
    spec = REGISTRY.get(tool)
    if spec and spec.rewrite_argv:
        return spec.rewrite_argv(tool, argv, tool_at, granted, jobserver)
    return None


def write_dirs_for(tool: str, argv: list[str], cwd: str, env: dict) -> list[str]:
    spec = REGISTRY.get(tool)
    if spec and spec.write_dirs:
        return spec.write_dirs(tool, argv, cwd, env)
    return []


def is_cold_for(tool: str, argv: list[str], cwd: str, env: dict) -> bool:
    spec = REGISTRY.get(tool)
    if spec and spec.is_cold:
        return spec.is_cold(tool, argv, cwd, env)
    return False
