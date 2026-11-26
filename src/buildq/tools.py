# SPDX-License-Identifier: MIT
"""Tool and compiler registry: keys, parallelism env, argv rewrites, restart/reuse, and disk dirs."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
import os
import re
import shlex
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
CARGO_BUILTIN_SUBCOMMANDS = frozenset({
    "build", "b", "check", "c", "test", "t", "run", "r", "clippy", "doc", "d", "bench",
    "publish", "package", "install", "uninstall", "clean", "fmt", "fix", "rustc", "rustdoc",
    "fetch", "update", "add", "remove", "rm", "metadata", "tree", "vendor", "verify-project",
    "generate-lockfile", "locate-project", "pkgid", "read-manifest", "new", "init", "search",
    "owner", "login", "logout", "yank", "config", "report", "help", "version",
})
CARGO_NON_COLD_SUBCOMMANDS = frozenset({
    "clean", "fmt", "metadata", "tree", "version", "help", "search", "login", "logout",
    "owner", "yank", "locate-project", "pkgid", "read-manifest", "verify-project", "new",
    "init", "add", "remove", "rm", "update", "fetch", "generate-lockfile", "vendor",
    "config", "report", "uninstall", "install",
})
CARGO_INFO_FLAGS = frozenset({"-h", "--help", "-V", "--version"})


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
    # Optional invocation-aware grant check (e.g. only `zig build`, or unwrapping `uv run`).
    honors_grant_args: Callable[[str, list[str]], bool] | None = None
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
    # When a spec defines both global_env and direct_env, direct_env supersedes global_env for
    # direct invocations of that spec.
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


def _cargo_manifest_label(path: str) -> str:
    """Stable, idempotent folder label for Cargo's `--manifest-path` option (empty for root Cargo.toml)."""
    cleaned = path.strip()
    if not cleaned or cleaned == "…":
        return ""
    norm = os.path.normpath(cleaned)
    if os.path.basename(norm) == "Cargo.toml":
        norm = os.path.dirname(norm)
    if not norm or norm in (".", "/"):
        return ""
    if os.path.isabs(norm) or norm.startswith(".."):
        leaf = os.path.basename(norm)
        return leaf if leaf and leaf not in (".", "..") else ""
    return norm


def _cargo_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    words = [tool]
    if rest and rest[0].startswith("+"):
        rest = rest[1:]
    pos = positionals(rest, CARGO_VALUE_OPTIONS)
    if pos:
        words.append(pos[0])
        if pos[0] not in CARGO_BUILTIN_SUBCOMMANDS and len(pos) >= 2 and SCRIPT_WORD.fullmatch(pos[1]):
            words.append(pos[1])
    manifests = [m for p in option_values(rest, {"--manifest-path"}) if (m := _cargo_manifest_label(p))]
    if manifests:
        words.append(f"--manifest-path {manifests[-1]}")
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


def _cargo_target_dir(args: list[str], cwd: str, env: dict, *, require_explicit: bool = False) -> str | None:
    """Resolve Cargo's target directory: `--target-dir` beats `CARGO_TARGET_DIR`, which beats
    the workspace/crate `target/` walked up from `--manifest-path` (if given) or `cwd`."""
    before = args[:args.index("--")] if "--" in args else args
    target_dirs = option_values(before, {"--target-dir"})
    if target_dirs:
        return os.path.join(cwd, target_dirs[-1])
    target = env.get("CARGO_TARGET_DIR")
    if target:
        return os.path.join(cwd, target)
    manifests = option_values(before, {"--manifest-path"})
    if require_explicit and not manifests:
        return None
    if manifests:
        mpath = os.path.abspath(os.path.join(cwd, manifests[-1]))
        start = os.path.dirname(mpath) if os.path.basename(mpath) == "Cargo.toml" or not os.path.isdir(mpath) else mpath
    else:
        start = os.path.abspath(cwd)
    d = start
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
        return os.path.join(start, "target") if manifests else None
    return os.path.join(root, "target")


def _cargo_write_dirs(_tool: str, args: list[str], cwd: str, env: dict) -> list[str]:
    target = _cargo_target_dir(args, cwd, env)
    return [target] if target else []


def _cargo_is_cold(_tool: str, args: list[str], cwd: str, env: dict) -> bool:
    """Whether a cargo command will build from scratch: its target directory has no build of that
    profile yet. History of warm builds says little about a cold one."""
    before = args[:args.index("--")] if "--" in args else args
    if any(a in CARGO_INFO_FLAGS for a in before):
        return False
    sub = _cargo_subcommand(before)
    if not sub or sub in CARGO_NON_COLD_SUBCOMMANDS:
        return False
    if sub == "sqlx":
        rest = before[1:] if before and before[0].startswith("+") else before
        pos = positionals(rest, CARGO_VALUE_OPTIONS)
        if len(pos) < 2 or pos[1] != "prepare":
            return False
    target = _cargo_target_dir(before, cwd, env)
    if not target:
        return False
    profile = "release" if "--release" in before else "debug"
    profiles = option_values(before, {"--profile"})
    if profiles:
        profile = {"dev": "debug", "test": "debug", "bench": "release"}.get(profiles[-1], profiles[-1])
    triples = option_values(before, {"--target"})
    if triples:
        return not os.path.isdir(os.path.join(target, triples[-1], profile))
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


DOTNET_GLOBAL_VALUE_OPTIONS = frozenset({
    "--roll-forward", "--fx-version", "--additionalprobingpath", "--depsfile", "--runtimeconfig",
})
DOTNET_BUILD_SUBCOMMANDS = frozenset({"build", "test", "publish", "pack", "restore", "clean", "run", "msbuild"})
DOTNET_VALUE_OPTIONS = frozenset({
    "-f", "--framework", "-r", "--runtime", "-a", "--arch", "--os", "-o", "--output",
    "--artifacts-path", "-v", "--verbosity", "-l", "--logger", "--filter", "-d", "--diag",
    "-s", "--settings", "--results-directory", "--collect", "--test-adapter-path",
    "--blame-hang-timeout", "--blame-crash-dump-type", "--blame-hang-dump-type",
    "--source", "--configfile", "--packages", "--version-suffix", "--launch-profile",
    "-e", "--environment", "--tl", "--terminalLogger",
})
DOTNET_MSBUILD_SWITCH = re.compile(r"^/[A-Za-z]+:")


def _dotnet_subcommand_index(args: list[str]) -> int | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in DOTNET_GLOBAL_VALUE_OPTIONS:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return i
    return None


def _dotnet_norm_config(val: str) -> str:
    return {"debug": "Debug", "release": "Release"}.get(val.lower(), val)


def _dotnet_config_from_props(raw: str) -> str | None:
    cfg: str | None = None
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            val = v.strip()
            if k.strip().lower() == "configuration" and val and val != "…":
                cfg = _dotnet_norm_config(val)
    return cfg


def _dotnet_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    sub_idx = _dotnet_subcommand_index(rest)
    if sub_idx is None:
        return [tool]
    sub = os.path.basename(rest[sub_idx].rstrip("/")) or rest[sub_idx]
    sub_args = rest[sub_idx + 1:]
    if sub not in DOTNET_BUILD_SUBCOMMANDS:
        extra = [os.path.basename(p.rstrip("/")) or p for p in positionals(sub_args) if p != "…"][:1]
        return [tool, sub] + extra
    explicit_target: str | None = None
    pos_target: str | None = None
    config: str | None = None
    no_build = False
    no_restore = False
    i = 0
    while i < len(sub_args):
        arg = sub_args[i]
        if arg in ("--project", "--solution"):
            val = sub_args[i + 1] if i + 1 < len(sub_args) else ""
            if val and val != "…":
                explicit_target = os.path.basename(val.rstrip("/")) or val
            i += 2 if i + 1 < len(sub_args) else 1
            continue
        if arg.startswith(("--project=", "--project:", "--solution=", "--solution:")):
            val = re.split(r"[=:]", arg, maxsplit=1)[1]
            if val and val != "…":
                explicit_target = os.path.basename(val.rstrip("/")) or val
            i += 1
            continue
        if arg == "-p":
            val = sub_args[i + 1] if i + 1 < len(sub_args) else ""
            if "=" in val:
                found = _dotnet_config_from_props(val)
                if found:
                    config = found
            elif val and val != "…":
                explicit_target = os.path.basename(val.rstrip("/")) or val
            i += 2 if i + 1 < len(sub_args) else 1
            continue
        if arg.startswith("-p="):
            val = arg[3:]
            if "=" in val:
                found = _dotnet_config_from_props(val)
                if found:
                    config = found
            elif val and val != "…":
                explicit_target = os.path.basename(val.rstrip("/")) or val
            i += 1
            continue
        if arg.startswith(("-p:", "--property:", "-property:", "--property=", "-property=")):
            val = re.split(r"[=:]", arg, maxsplit=1)[1]
            found = _dotnet_config_from_props(val)
            if found:
                config = found
            i += 1
            continue
        if arg in ("--property", "-property"):
            val = sub_args[i + 1] if i + 1 < len(sub_args) else ""
            if "=" in val:
                found = _dotnet_config_from_props(val)
                if found:
                    config = found
            i += 2 if i + 1 < len(sub_args) else 1
            continue
        if arg in ("-c", "--configuration"):
            val = sub_args[i + 1] if i + 1 < len(sub_args) else ""
            if val and val != "…":
                config = _dotnet_norm_config(val)
            i += 2 if i + 1 < len(sub_args) else 1
            continue
        if arg.startswith(("-c=", "-c:", "--configuration=", "--configuration:")):
            val = re.split(r"[=:]", arg, maxsplit=1)[1]
            if val and val != "…":
                config = _dotnet_norm_config(val)
            i += 1
            continue
        if arg == "--no-build":
            no_build = True
            i += 1
            continue
        if arg == "--no-restore":
            no_restore = True
            i += 1
            continue
        if arg in DOTNET_VALUE_OPTIONS:
            i += 2
            continue
        if arg.startswith("-") or DOTNET_MSBUILD_SWITCH.match(arg):
            i += 1
            continue
        if arg != "…" and pos_target is None:
            pos_target = os.path.basename(arg.rstrip("/")) or arg
        i += 1
    words = [tool, sub]
    target = explicit_target or pos_target
    if target:
        words.append(target)
    if config:
        words.append(f"-c {config}")
    if no_build:
        words.append("--no-build")
    if no_restore:
        words.append("--no-restore")
    return words


def _dotnet_restart_safe(_tool: str, args: list[str]) -> bool:
    idx = _dotnet_subcommand_index(args)
    return idx is not None and args[idx] in ("build", "test")


def _dotnet_reusable_check(_tool: str, args: list[str]) -> bool:
    if any(re.match(r"-[ld](:|$)", a) or re.search(r"CollectCoverage", a) for a in args):
        return False  # dotnet test's short --logger and --diag, and coverage through MSBuild
    idx = _dotnet_subcommand_index(args)
    return idx is not None and args[idx] == "test"


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


def _find_marker_root(start: str, markers: tuple[str, ...], limit: int = 8) -> str | None:
    here = os.path.abspath(start)
    for _ in range(limit):
        if any(os.path.exists(os.path.join(here, m)) for m in markers):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


# ---- Gradle ---------------------------------------------------------------------------------

GRADLE_VALUE_OPTIONS = frozenset({
    "-p", "--project-dir", "-b", "--build-file", "-c", "--settings-file", "-g", "--gradle-user-home",
    "-I", "--init-script", "-x", "--exclude-task", "--max-workers", "--console", "--console-unicode",
    "--warning-mode", "--priority", "--include-build", "--project-cache-dir",
    "--configuration-cache-problems", "-F", "--dependency-verification", "-M",
    "--write-verification-metadata", "--update-locks", "--develocity-url",
    "--develocity-plugin-version", "--tests", "-D", "--system-prop", "-P", "--project-prop",
})
GRADLE_SHORT_UNSAFE_RESTART = re.compile(r"-[qidswamtuUS]*(?:t[qidswamtuUSvh?]*|[MI].*)")
GRADLE_UNSAFE_LEAF = re.compile(
    r"(publish|upload|deploy|install|uninstall|push|format|apply|fix|clean|connected|instrumented|"
    r"firebase|bundletool|run|serve|start|stop|migrate|seed|drop|destroy|delete|remove|sign|promote|distribute)",
    re.I,
)
GRADLE_RESTART_LEAF = re.compile(
    r"^(?:build|assemble|bundle|compile|classes|testClasses|jar|war|test|check|verify|lint|"
    r"ktlintCheck|detekt|spotlessCheck|checkstyle|pmd|spotbugs|jacocoTestReport)(?:[A-Z0-9].*)?$"
)


def _gradle_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    words = [tool] + positionals(rest, GRADLE_VALUE_OPTIONS)[:2]
    if not ({"--stop", "--status", "--foreground"} & set(rest)) and not _gradle_honors_grant(tool, rest):
        words.append("--max-workers")
    return words


def _gradle_opts_tokens(raw: str) -> list[str]:
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _gradle_merged_opts(granted: int, caller_env: dict | None, *,
                        skip_daemon: bool = False, skip_workers: bool = False) -> dict[str, str]:
    existing = str((caller_env or {}).get("GRADLE_OPTS") or "")
    tokens = _gradle_opts_tokens(existing)
    add: list[str] = []
    # Keep Gradle inside the build's cgroup and capped to granted workers unless caller set them.
    if not skip_daemon and not any(tok.startswith("-Dorg.gradle.daemon=") for tok in tokens):
        add.append("-Dorg.gradle.daemon=false")
    if not skip_workers and not any(tok.startswith("-Dorg.gradle.workers.max=") for tok in tokens):
        add.append(f"-Dorg.gradle.workers.max={granted}")
    if not add:
        return {}
    # Prepend defaults so JVM's last-wins -D precedence always preserves caller flags and quoting.
    return {"GRADLE_OPTS": f"{' '.join(add)} {existing}".strip()}


def _gradle_global_env(granted: int, _want: int, _jobserver: str | None, _test_threads: bool,
                       caller_env: dict | None) -> dict[str, str]:
    return _gradle_merged_opts(granted, caller_env)


def _gradle_direct_env(_tool: str, args: list[str], granted: int, _want: int, _jobserver: str | None,
                       _test_threads: bool, caller_env: dict | None) -> dict[str, str]:
    before = args[:args.index("--")] if "--" in args else args
    if {"--stop", "--status", "--foreground"} & set(before):
        return {}
    has_daemon = (
        any(a in ("--daemon", "--no-daemon")
            or a.startswith(("-Dorg.gradle.daemon=", "--system-prop=org.gradle.daemon=")) for a in before)
        or any(a in ("-D", "--system-prop") and b.startswith("org.gradle.daemon=")
               for a, b in zip(before, before[1:]))
    )
    has_workers = (
        any(a == "--max-workers"
            or a.startswith(("--max-workers=", "-Dorg.gradle.workers.max=", "--system-prop=org.gradle.workers.max="))
            for a in before)
        or any(a in ("-D", "--system-prop") and b.startswith("org.gradle.workers.max=")
               for a, b in zip(before, before[1:]))
    )
    return _gradle_merged_opts(granted, caller_env, skip_daemon=has_daemon, skip_workers=has_workers)


def _gradle_honors_grant(_tool: str, args: list[str]) -> bool:
    before = args[:args.index("--")] if "--" in args else args
    if {"--stop", "--status", "--foreground"} & set(before):
        return False
    return not (
        any(a == "--max-workers"
            or a.startswith(("--max-workers=", "-Dorg.gradle.workers.max=", "--system-prop=org.gradle.workers.max="))
            for a in before)
        or any(a in ("-D", "--system-prop") and b.startswith("org.gradle.workers.max=")
               for a, b in zip(before, before[1:]))
    )


def _gradle_restart_safe(_tool: str, args: list[str]) -> bool:
    if "--" in args:
        return False
    if any(a in ("-t", "--continuous", "--stop", "--status", "--foreground", "--write-locks",
                 "--update-locks", "--refresh-keys", "-M", "--write-verification-metadata",
                 "--export-keys", "-I", "--init-script")
           or a.startswith(("--update-locks=", "--write-verification-metadata=", "--export-keys=",
                            "--init-script=", "-M", "-I"))
           or GRADLE_SHORT_UNSAFE_RESTART.fullmatch(a)
           for a in args):
        return False
    tasks = positionals(args, GRADLE_VALUE_OPTIONS)
    if not tasks:
        return False
    for task in tasks:
        leaf = task.rsplit(":", 1)[-1]
        if not leaf or not GRADLE_RESTART_LEAF.fullmatch(leaf) or GRADLE_UNSAFE_LEAF.search(leaf):
            return False
    return True


def _gradle_write_dirs(_tool: str, args: list[str], cwd: str, env: dict) -> list[str]:
    dirs: list[str] = []
    if env.get("GRADLE_USER_HOME"):
        dirs.append(os.path.join(cwd, env["GRADLE_USER_HOME"]))
    before = args[:args.index("--")] if "--" in args else args
    for g in option_values(before, {"-g", "--gradle-user-home"}):
        dirs.append(os.path.join(cwd, g))
    proj = option_values(before, {"-p", "--project-dir"})
    start = os.path.abspath(os.path.join(cwd, proj[-1])) if proj else os.path.abspath(cwd)
    here = start
    nearest_build = None
    settings_root = None
    for _ in range(8):
        if any(os.path.exists(os.path.join(here, m)) for m in ("settings.gradle", "settings.gradle.kts")):
            settings_root = here
            break
        if nearest_build is None and any(os.path.exists(os.path.join(here, m))
                                         for m in ("build.gradle", "build.gradle.kts")):
            nearest_build = here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    root = settings_root or nearest_build
    if root:
        dirs.append(os.path.join(root, "build"))
        for task in positionals(before, GRADLE_VALUE_OPTIONS):
            if ":" in task:
                parts = [p for p in task.split(":")[:-1] if p]
                if parts:
                    subproj = os.path.join(root, *parts)
                    if os.path.isdir(subproj):
                        dirs.append(os.path.join(subproj, "build"))
    if nearest_build and nearest_build != root:
        dirs.append(os.path.join(nearest_build, "build"))
    return dirs


# ---- CMake & CTest --------------------------------------------------------------------------

CMAKE_CONFIGURE_VALUE_OPTIONS = frozenset({
    "-S", "-B", "-G", "-A", "-T", "-C", "-D", "-U", "--preset", "--toolchain", "--install-prefix",
})
CMAKE_SAFE_TARGETS = frozenset({"all", "test", "check", "ALL_BUILD"})


def _cmake_build_targets(before: list[str]) -> list[str]:
    """Collect all targets passed to cmake --build's `-t <tgt>...` / `--target <tgt>...`,
    applying CMake's `;`-separated list semantics (`all;install` -> `['all', 'install']`)."""
    raw: list[str] = []
    i = 0
    while i < len(before):
        arg = before[i]
        if arg in ("-t", "--target"):
            i += 1
            while i < len(before) and not before[i].startswith("-"):
                raw.append(before[i])
                i += 1
            continue
        if arg.startswith("--target="):
            val = arg[len("--target="):]
            if val:
                raw.append(val)
        elif arg.startswith("-t") and len(arg) > 2:
            raw.append(arg[2:])
        i += 1
    targets: list[str] = []
    for item in raw:
        for piece in item.split(";"):
            if piece:
                targets.append(piece)
    return targets


def _cmake_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    if rest[:1] == ["--build"]:
        words = [tool, "--build"]
        if len(rest) > 1 and not rest[1].startswith("-"):
            bdir = os.path.basename(rest[1].rstrip("/")) or rest[1]
            words.append(bdir)
        words += [f"--preset {p}" for p in option_values(rest, {"--preset"})[:1]]
        words += [f"--config {c}" for c in option_values(rest, {"--config"})[:1]]
        words += [f"--target {t}" for t in sorted(set(_cmake_build_targets(rest)))[:2]]
        return words
    if rest[:1] == ["--workflow"]:
        return [tool, "--workflow"] + [f"--preset {p}" for p in option_values(rest, {"--preset"})[:1]]
    if rest[:1] == ["--install"]:
        return [tool, "--install"]
    pos = positionals(rest, CMAKE_CONFIGURE_VALUE_OPTIONS)
    return ([tool]
            + [f"--preset {p}" for p in option_values(rest, {"--preset"})[:1]]
            + [os.path.basename(p.rstrip("/")) or p for p in pos[:1]])


def _cmake_restart_safe(_tool: str, args: list[str]) -> bool:
    # A build preset can hide an `install` or custom mutating target, and `--` forwards arbitrary
    # targets to the native build tool; require an explicit build dir and standard safe targets.
    if "--" in args or args[:1] != ["--build"]:
        return False
    if len(args) < 2 or args[1].startswith("-"):
        return False
    if any(a in ("--preset", "--list-presets", "--help", "-h", "--version")
           or a.startswith(("--preset=", "--list-presets=")) for a in args[1:]):
        return False
    return all(t in CMAKE_SAFE_TARGETS for t in _cmake_build_targets(args))


def _cmake_honors_grant(_tool: str, args: list[str]) -> bool:
    before = args[:args.index("--")] if "--" in args else args
    if before[:1] != ["--build"]:
        return False
    return not any(a in ("-j", "--parallel") or a.startswith(("-j", "--parallel=")) for a in before[1:])


def _cmake_write_dirs(_tool: str, args: list[str], cwd: str, _env: dict) -> list[str]:
    before = args[:args.index("--")] if "--" in args else args
    dirs: list[str] = []
    if before[:1] == ["--build"] and len(before) > 1 and not before[1].startswith("-"):
        dirs.append(os.path.join(cwd, before[1]))
    elif not before or before[0] not in ("-E", "-P", "--install", "--workflow"):
        for b in option_values(before, {"-B"}):
            dirs.append(os.path.join(cwd, b))
    return dirs


def _ctest_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    words = [tool]
    words += [f"--preset {p}" for p in option_values(rest, {"--preset"})[:1]]
    words += [f"-C {c}" for c in option_values(rest, {"-C", "--build-config"})[:1]]
    words += [f"-R {r}" for r in option_values(rest, {"-R", "--tests-regex"})[:1]]
    if not _ctest_honors_grant(tool, rest):
        words.append("-j")
    # Keep dashboard/script/build-and-test modes distinct from ordinary test runs in history & replay.
    for a in rest:
        if re.fullmatch(r"-(?:D|M|T|S|A).*", a):
            words.append(a[:2])
            break
        if CTEST_UNSAFE_LONG.fullmatch(a):
            words.append(a.split("=", 1)[0])
            break
    return words


def _ctest_honors_grant(_tool: str, args: list[str]) -> bool:
    return not any(a in ("-j", "--parallel") or a.startswith(("-j", "--parallel=")) for a in args)


CTEST_UNSAFE_LONG = re.compile(
    r"--(?:dashboard|test-model|test-action|script|script-new-process|add-notes|track|group|submit|"
    r"extra-submit|submit-index|http-header|http1\.0|tomorrow-tag|overwrite|build-and-test|"
    r"test-command|(?!build-config(?:=|$))build-[a-z-]+)(=.*)?$"
)


def _ctest_restart_safe(_tool: str, args: list[str]) -> bool:
    # Restrict to ordinary test mode: reject dashboard submission (-D/-M/-T/-A), CTest scripts
    # (-S/-SP/--script*), and delegated --build-and-test / --test-command modes.
    return not any(re.fullmatch(r"-(?:D|M|T|S|A).*", a) or CTEST_UNSAFE_LONG.fullmatch(a) for a in args)


def _ctest_write_dirs(_tool: str, args: list[str], cwd: str, _env: dict) -> list[str]:
    return [os.path.join(cwd, d) for d in option_values(args, {"--test-dir"})]


# ---- Direct C/C++ (gcc, g++, clang, clang++, cc, c++) ---------------------------------------

CC_VALUE_OPTIONS = frozenset({
    "-o", "-x", "-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-L", "-l",
    "-D", "-U", "-MF", "-MT", "-MQ", "-MJ", "-target", "--target", "-arch", "-sysroot", "--sysroot",
    "-B", "-Xclang", "-Xlinker", "-Xpreprocessor", "-Xassembler", "-Xanalyzer", "-Xarch_host",
    "-Xarch_device", "-Xcuda-fatbinary", "-Xcuda-ptxas", "-Xopenmp-target", "-u", "-T", "-e",
    "-soname", "-rpath", "-aux-info", "-wrapper", "-dumpdir", "-dumpbase", "-serialize-diagnostics",
    "--serialize-diagnostics", "-gen-cdb-fragment-path", "-load", "-working-directory",
})
CC_OPT_FLAG = re.compile(r"-O(?:[0-3sgz]|fast)?$")
CC_EMIT_EXACT = frozenset({
    "-c", "-S", "-E", "-M", "-MM", "-MD", "-MMD", "-MG", "-MP", "-save-temps", "--save-temps",
    "--coverage", "-Xpreprocessor", "-Xclang", "-Xlinker", "-Xassembler", "-Xanalyzer", "--analyze",
    "-analyze", "--serialize-diagnostics", "-serialize-diagnostics", "--write-dependencies",
    "--write-user-dependencies", "--dependencies", "-dumpdir", "-fstack-usage", "-fcallgraph-info",
    "-gen-cdb-fragment-path", "-load", "-###", "-dumpversion", "-dumpfullversion", "-dumpmachine",
    "--precompile", "-precompile", "-extract-api", "--extract-api", "-V",
})
CC_EMIT_PREFIXES = (
    "-o", "-MF", "-MT", "-MQ", "-MJ", "--output", "-save-temps=", "--save-temps=", "-ftime-trace",
    "-fprofile", "-dumpbase", "-aux-info", "-ftest-coverage", "-Wp,", "-Wl,", "-Wa,", "-X",
    "-fdump-", "-fdiagnostics-format=", "-fdiagnostics-add-output=", "-fdiagnostics-set-output=",
    "-fdeps-file=", "-save-stats", "-fmodule-output", "-fplugin", "-print-", "--print-",
    "-emit-", "--emit-", "--serialize-diagnostics=", "-serialize-diagnostics=",
)


def _cc_scan(args: list[str]) -> tuple[list[str], list[str], bool]:
    """Scan C/C++ compiler args consuming option values cleanly -> (options, positionals, has_rsp)."""
    # GCC/Clang expand `@file` response files before parsing options, even after a value option.
    has_rsp = any(a.startswith("@") for a in args)
    opts: list[str] = []
    pos: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            pos.extend(args[i + 1:])
            break
        if arg.startswith("@"):
            i += 1
        elif arg == "-":
            pos.append(arg)
            i += 1
        elif arg.startswith("-"):
            opts.append(arg)
            i += 2 if arg in CC_VALUE_OPTIONS and i + 1 < len(args) else 1
        else:
            pos.append(arg)
            i += 1
    return opts, pos, has_rsp


def _cc_flag_and_source_words(rest: list[str]) -> list[str]:
    opts, pos, _ = _cc_scan(rest)
    words = [f for f in ("-c", "-S", "-E", "-shared", "-fsyntax-only") if f in opts]
    opt = next((a for a in opts if CC_OPT_FLAG.fullmatch(a)), None)
    if opt:
        words.append(opt)
    if any(a == "-flto" or a.startswith("-flto=") for a in opts):
        words.append("-flto")
    files = [p for p in pos if p != "-"]
    if files:
        words.append(os.path.basename(files[0]))
    else:
        outs = option_values(rest, {"-o"})
        if outs:
            words.append(os.path.basename(outs[0]))
    return words


def _cc_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    return [tool] + _cc_flag_and_source_words(rest)


def _cc_restart_safe(_tool: str, args: list[str]) -> bool:
    opts, _, has_rsp = _cc_scan(args)
    if has_rsp:
        return False  # response file could pass -wrapper or unexpected options
    return not any(a == "-wrapper" or a.startswith("-wrapper=") for a in opts)


def _cc_reusable_check(_tool: str, args: list[str]) -> bool:
    opts, pos, has_rsp = _cc_scan(args)
    if has_rsp or "-fsyntax-only" not in opts:
        return False
    if any(a == "-wrapper" or a.startswith("-wrapper=") for a in opts):
        return False
    if any(a in CC_EMIT_EXACT or a.startswith(CC_EMIT_PREFIXES) for a in opts):
        return False
    if "-" in pos:
        return False  # reads stdin
    return bool(pos)


# ---- Zig ------------------------------------------------------------------------------------

ZIG_BUILD_VALUE_OPTIONS = frozenset({
    "-j", "-p", "--prefix", "--prefix-lib-dir", "--prefix-exe-dir", "--prefix-include-dir",
    "--sysroot", "--search-prefix", "--libc", "--cache-dir", "--global-cache-dir", "--zig-lib-dir",
    "--build-file", "--build-runner", "--seed", "--maxrss", "--color",
    "--summary", "--debounce", "--system", "--port",
})
ZIG_COMPILE_VALUE_OPTIONS = frozenset({
    "-O", "-target", "-mcpu", "-M", "--dep", "--name", "--cache-dir", "--global-cache-dir",
    "--zig-lib-dir", "--test-filter", "--test-name-prefix", "--test-cmd", "-I", "-L",
    "-l", "-isystem", "-dirafter", "-idirafter", "-framework", "-F", "-rpath", "-T", "--script", "-ofmt",
})


def _zig_is_long_running(args: list[str]) -> bool:
    return any(a in ("--watch", "--fuzz", "--webui", "--listen", "--time-report")
               or a.startswith(("--webui=", "--listen=", "--time-report=")) for a in args)


def _zig_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    sub = rest[0] if rest and not rest[0].startswith("-") else ""
    if not sub:
        return [tool]
    sub_args = rest[1:]
    if sub == "build":
        words = [tool, "build"] + positionals(sub_args, ZIG_BUILD_VALUE_OPTIONS)[:2]
        is_release = any(
            (a.startswith("-Doptimize=") and a[len("-Doptimize="):].startswith("Release"))
            or (a.startswith("--release=") and a[len("--release="):] not in ("", "off"))
            or a == "--release"
            for a in sub_args
        )
        if is_release:
            words.append("--release")
        if asks_for_jobs(sub_args):
            words.append("-j")
        return words
    if sub in ("test", "test-obj", "build-exe", "build-lib", "build-obj", "ast-check", "run"):
        words = [tool, sub]
        words += [f"-O {m}" for m in option_values(sub_args, {"-O"})[:1]]
        pos = positionals(sub_args, ZIG_COMPILE_VALUE_OPTIONS)
        words += [os.path.basename(p.rstrip("/")) or p for p in pos[:1]]
        return words
    if sub in ("cc", "c++"):
        return [tool, sub] + _cc_flag_and_source_words(sub_args)
    return [tool, sub] + [os.path.basename(p.rstrip("/")) or p for p in positionals(sub_args)[:1]]


def _zig_honors_grant(_tool: str, args: list[str]) -> bool:
    if args[:1] != ["build"]:
        return False
    before = args[:args.index("--")] if "--" in args else args
    return not asks_for_jobs(before) and not _zig_is_long_running(before)


def _zig_rewrite_argv(_tool: str, args: list[str], granted: int, _jobserver: str | None,
                      _test_threads: bool, _caller_env: dict | None) -> list[str] | None:
    if args[:1] != ["build"]:
        return None
    before = args[:args.index("--")] if "--" in args else args
    if asks_for_jobs(before) or _zig_is_long_running(before) or ({"--help", "-h", "-l", "--list-steps"} & set(before)):
        return None
    return [args[0], f"-j{granted}", *args[1:]]


def _zig_restart_safe(_tool: str, args: list[str]) -> bool:
    if not args or args[0].startswith("-"):
        return False
    sub, rest = args[0], args[1:]
    before = rest[:rest.index("--")] if "--" in rest else rest
    if _zig_is_long_running(before) or any(a.startswith("@") for a in before):
        return False
    if sub == "build":
        if "--" in rest:
            return False
        if any(a == "-p" or a.startswith(("-p", "--prefix")) for a in before):
            return False
        return set(positionals(before, ZIG_BUILD_VALUE_OPTIONS)) <= {"install", "test", "check", "build"}
    if sub in ("test", "test-obj", "build-exe", "build-lib", "build-obj", "ast-check"):
        if any(a == "--test-cmd" or a.startswith("--test-cmd=") for a in before):
            return False
        return True
    if sub in ("cc", "c++"):
        return _cc_restart_safe(sub, rest)
    return False


def _zig_reusable_check(_tool: str, args: list[str]) -> bool:
    if not args or args[0] not in ("test", "ast-check"):
        return False
    rest = args[1:]
    if _zig_is_long_running(rest) or any(a.startswith("@") for a in rest):
        return False
    if any(a in ("--test-no-exec", "--test-cmd", "-fno-emit-bin", "--show-builtin", "-o", "--output",
                 "--verbose-llvm-ir", "--verbose-llvm-bc")
           or a.startswith(("--test-cmd=", "-femit-", "--output=", "--verbose-llvm-ir=", "--verbose-llvm-bc="))
           for a in rest):
        return False
    return bool(positionals(rest, ZIG_COMPILE_VALUE_OPTIONS))


def _zig_project_root(before: list[str], cwd: str) -> str | None:
    bfiles = option_values(before, {"--build-file"})
    if bfiles:
        return os.path.dirname(os.path.abspath(os.path.join(cwd, bfiles[-1])))
    return _find_marker_root(cwd, ("build.zig",))


def _zig_write_dirs(_tool: str, args: list[str], cwd: str, env: dict) -> list[str]:
    dirs: list[str] = []
    for var in ("ZIG_LOCAL_CACHE_DIR", "ZIG_GLOBAL_CACHE_DIR"):
        if env.get(var):
            dirs.append(os.path.join(cwd, env[var]))
    before = args[:args.index("--")] if "--" in args else args
    for val in option_values(before, {"--cache-dir", "--global-cache-dir"}):
        dirs.append(os.path.join(cwd, val))
    if args[:1] == ["build"]:
        for val in option_values(before, {"-p", "--prefix"}):
            dirs.append(os.path.join(cwd, val))
    root = _zig_project_root(before, cwd)
    if root:
        dirs.append(os.path.join(root, ".zig-cache"))
        if args[:1] == ["build"]:
            dirs.append(os.path.join(root, "zig-out"))
    return dirs


def _zig_is_cold(_tool: str, args: list[str], cwd: str, env: dict) -> bool:
    if args[:1] != ["build"]:
        return False
    before = args[:args.index("--")] if "--" in args else args
    cache_opts = option_values(before, {"--cache-dir"})
    if cache_opts:
        cache = os.path.join(cwd, cache_opts[-1])
    elif env.get("ZIG_LOCAL_CACHE_DIR"):
        cache = os.path.join(cwd, env["ZIG_LOCAL_CACHE_DIR"])
    else:
        root = _zig_project_root(before, cwd)
        if root is None:
            return False
        cache = os.path.join(root, ".zig-cache")
    return not os.path.isdir(cache)


# ---- Python (pytest, mypy, pyright, python -m, uv) -----------------------------------------

PYTEST_VALUE_OPTIONS = frozenset({
    "-k", "-m", "-c", "-o", "--override-ini", "-p", "-n", "--numprocesses", "--dist", "--maxfail",
    "--tb", "--show-capture", "--capture", "--junitxml", "--junit-xml", "--rootdir", "--basetemp",
    "--confcutdir", "--ignore", "--ignore-glob", "--deselect", "--durations", "--durations-min",
    "--log-level", "--log-format", "--log-file", "--cov", "--cov-report", "--cov-config", "--html",
    "--json-report-file", "-W", "--filterwarnings", "--verbosity", "-r", "--pdbcls",
})
PYTEST_NON_REUSABLE = frozenset({
    "--lf", "--last-failed", "--ff", "--failed-first", "--nf", "--new-first", "--sw", "--stepwise",
    "--stepwise-skip", "--stepwise-reset", "--sw-reset", "--cache-clear", "--cov", "--cov-report",
    "--junitxml", "--junit-xml", "--html", "--json-report", "--json-report-file", "--resultlog",
    "--log-file", "--basetemp", "-o", "--override-ini", "--snapshot-update", "--update-goldens",
    "--benchmark-save", "--benchmark-autosave", "--benchmark-json", "--debug",
    "--collect-only", "--collectonly", "--co", "--setup-only", "--setuponly", "--setup-plan",
    "--setupplan", "--setup-show", "--fixtures", "--funcargs", "--fixtures-per-test", "--markers",
    "--cache-show", "-V", "--version", "-h", "--help",
})
PYTEST_NON_REUSABLE_PREFIXES = (
    "--cov=", "--cov-report=", "--junitxml=", "--junit-xml=", "--html=", "--json-report=",
    "--json-report-file=", "--resultlog=", "--log-file=", "--basetemp=", "-o", "--override-ini=",
    "--benchmark-save=", "--benchmark-json=", "--cache-show=", "--debug=",
)


def _pytest_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    targets = []
    for p in positionals(rest, PYTEST_VALUE_OPTIONS)[:2]:
        head = p.split("::", 1)[0]
        targets.append(os.path.basename(head.rstrip("/")) or head)
    return [tool] + targets


def _pytest_restart_safe(_tool: str, args: list[str]) -> bool:
    return not any(
        a.startswith("@")
        or a in ("-f", "--looponfail", "--pdb", "--pdbcls", "--trace")
        or a.startswith("--pdbcls=")
        or re.fullmatch(r"-[qvxsbl]*f.*", a)
        for a in args
    )


def _pytest_reusable_check(_tool: str, args: list[str]) -> bool:
    if not _pytest_restart_safe("pytest", args):
        return False
    return not any(
        a in PYTEST_NON_REUSABLE
        or a.startswith(PYTEST_NON_REUSABLE_PREFIXES)
        or re.fullmatch(r"-[qvxsbl]*[oVh].*", a)
        for a in args
    )


MYPY_VALUE_OPTIONS = frozenset({
    "--config-file", "--cache-dir", "--python-version", "--platform", "--package", "-p", "--module",
    "-m", "-c", "--command", "--exclude", "--follow-imports", "--output", "--junit-xml", "--html-report",
    "--xml-report", "--txt-report", "--linecount-report", "--linecoverage-report",
    "--lineprecision-report", "--cobertura-xml-report", "--any-exprs-report", "--xslt-html-report",
    "--xslt-txt-report", "--custom-typeshed-dir", "--custom-typing-module", "--python-executable",
})


def _mypy_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    words = [tool]
    words += [f"-p {p}" for p in option_values(rest, {"-p", "--package"})[:1]]
    words += [f"-m {m}" for m in option_values(rest, {"-m", "--module"})[:1]]
    pos: list[str] = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--shadow-file":  # takes 2 args: SOURCE SHADOW
            i += 3
            continue
        if arg in MYPY_VALUE_OPTIONS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        pos.append(os.path.basename(arg.rstrip("/")) or arg)
        i += 1
    return words + pos[:2]


def _mypy_restart_safe(_tool: str, args: list[str]) -> bool:
    return not any(a.startswith("@") or a in ("--install-types", "--non-interactive", "-w", "--watch") for a in args)


def _mypy_reusable_check(_tool: str, args: list[str]) -> bool:
    if not _mypy_restart_safe("mypy", args):
        return False
    return not any(
        a in ("--command", "-V", "--version", "-h", "--help", "--shadow-file",
              "--junit-xml", "--output", "--stats", "--dump-graph", "--dump-deps",
              "--timing-stats", "--linechecking-stats", "--perf-stats-file")
        or a.startswith(("-c", "--command=", "--shadow-file=", "--junit-xml=", "--output=",
                         "--timing-stats=", "--linechecking-stats=", "--perf-stats-file="))
        or re.fullmatch(r"-[iv]*[Vh].*", a)
        or re.fullmatch(r"--[a-z-]+-report(=.*)?", a)
        for a in args
    )


PYRIGHT_VALUE_OPTIONS = frozenset({
    "-p", "--project", "--pythonpath", "--python-path", "--pythonversion", "--python-version",
    "--pythonplatform", "--python-platform", "--typeshedpath", "--typeshed-path", "--venvpath",
    "--venv-path", "--level", "--baselinefile", "--createstub", "--verifytypes", "--gitlabcodequality",
})


def _pyright_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    return [tool] + [os.path.basename(p.rstrip("/")) or p for p in positionals(rest, PYRIGHT_VALUE_OPTIONS)[:2]]


def _pyright_restart_safe(_tool: str, args: list[str]) -> bool:
    return not any(a.startswith("@") or a in ("-w", "--watch", "--createstub", "--writebaseline")
                   or a.startswith("--createstub=") for a in args)


def _pyright_reusable_check(_tool: str, args: list[str]) -> bool:
    if not _pyright_restart_safe("pyright", args):
        return False
    return not any(
        a in ("--outputjson", "--stats", "--baselinefile", "--gitlabcodequality", "--verifytypes",
              "-V", "--version", "--help", "-h", "-c", "--command")
        or a.startswith(("--baselinefile=", "--gitlabcodequality=", "--verifytypes=", "--command="))
        for a in args
    )


def _python_module_call(args: list[str]) -> tuple[str, list[str]] | None:
    """Strictly parse safe Python interpreter flags before `-m MODULE`; None for scripts or unknown flags."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-m":
            return (args[i + 1], args[i + 2:]) if i + 1 < len(args) else None
        if arg.startswith("-m") and len(arg) > 2:
            return arg[2:], args[i + 1:]
        if re.fullmatch(r"-[BEIOPSsbuvq]+", arg):
            i += 1
            continue
        if arg in ("-W", "-X", "--check-hash-based-pycs"):
            if i + 1 >= len(args):
                return None
            i += 2
            continue
        if arg.startswith(("-W", "-X", "--check-hash-based-pycs=")):
            i += 1
            continue
        return None
    return None


def _python_module_restart_safe(mod: str, mod_args: list[str]) -> bool:
    if mod == "unittest":
        return True
    if mod == "pytest":
        return _pytest_restart_safe("pytest", mod_args)
    if mod == "mypy":
        return _mypy_restart_safe("mypy", mod_args)
    if mod in ("pyright", "basedpyright"):
        return _pyright_restart_safe(mod, mod_args)
    return False


def _python_module_reusable_check(mod: str, mod_args: list[str]) -> bool:
    if mod == "unittest":
        return not any(a in ("-h", "--help") or re.fullmatch(r"-[bcfqv]*h.*", a) for a in mod_args)
    if mod == "pytest":
        return _pytest_reusable_check("pytest", mod_args)
    if mod == "mypy":
        return _mypy_reusable_check("mypy", mod_args)
    if mod in ("pyright", "basedpyright"):
        return _pyright_reusable_check(mod, mod_args)
    return False


def _python_restart_safe(_tool: str, args: list[str]) -> bool:
    call = _python_module_call(args)
    return bool(call and _python_module_restart_safe(*call))


def _python_reusable_check(_tool: str, args: list[str]) -> bool:
    call = _python_module_call(args)
    return bool(call and _python_module_reusable_check(*call))


UV_TOP_VALUE_OPTIONS = frozenset({
    "--directory", "--project", "--color", "--cache-dir", "--config-file", "--python-preference",
})
UV_RUN_VALUE_OPTIONS = frozenset({
    "--extra", "--no-extra", "--group", "--no-group", "--only-group", "--no-editable-package",
    "--env-file", "-w", "--with", "--with-editable", "--with-requirements", "-p", "--python",
    "--directory", "--project", "--package", "--color", "--cache-dir", "--config-file",
    "--index-url", "--extra-index-url", "--index", "--find-links", "-f", "--python-preference",
    "--refresh-package", "--reinstall-package", "--resolution", "--prerelease", "--fork-strategy",
    "--exclude-newer", "--link-mode", "-C", "--config-setting",
})
UV_STRICT_TOP_FLAGS = frozenset({
    "-q", "--quiet", "-v", "--verbose", "--offline", "--frozen", "--locked", "--no-cache",
    "--no-progress", "--no-config", "--managed-python", "--no-managed-python", "--no-python-downloads",
})
UV_STRICT_RUN_FLAGS = UV_STRICT_TOP_FLAGS | frozenset({
    "--no-dev", "--only-dev", "--all-extras", "--no-default-groups", "--all-groups", "--no-editable",
    "--exact", "--no-env-file", "--isolated", "--active", "--no-sync", "--refresh", "--reinstall",
    "--all-packages", "--no-project",
})
UV_STRICT_RUN_VALUE_OPTIONS = frozenset({
    "--extra", "--no-extra", "--group", "--no-group", "--only-group", "-w", "--with",
    "--with-requirements", "-p", "--python", "--package", "--python-preference", "--resolution",
    "--prerelease", "--fork-strategy", "--exclude-newer", "--link-mode", "--color",
})
UV_REUSE_FORBIDDEN_FLAGS = frozenset({
    "--refresh", "--reinstall", "--exact", "--all-extras", "--all-groups",
    "-w", "--with", "--with-requirements", "--extra", "--no-extra", "--group", "--no-group", "--only-group",
})


def _uv_words(tool: str, rest: list[str], labels: Collection[str]) -> list[str]:
    items = positional_items(rest, UV_TOP_VALUE_OPTIONS)
    if not items:
        return [tool]
    sub_idx, sub = items[0]
    if sub != "run":
        before = rest[:rest.index("--")] if "--" in rest else rest
        return [tool] + positionals(before, UV_TOP_VALUE_OPTIONS)[:2]
    after = rest[sub_idx + 1:]
    is_module = False
    i = 0
    while i < len(after):
        arg = after[i]
        if arg == "--":
            i += 1
            break
        if arg in ("-m", "--module"):
            is_module = True
            i += 1
            continue
        if arg in UV_RUN_VALUE_OPTIONS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        break
    inner = after[i:]
    if "--" in inner:
        inner = inner[:inner.index("--")]
    if not inner:
        return [tool, "run"]
    if is_module:
        return [tool, "run", "-m", os.path.basename(inner[0])]
    inner_tool = os.path.basename(inner[0])
    inner_args = inner[1:]
    if inner_tool != "uv":
        matched = words_for(inner_tool, inner_args, labels)
        if matched is not None:
            return [tool, "run"] + matched
    if inner_tool in ("python", "python3") or re.fullmatch(r"python3\.\d+", inner_tool):
        call = _python_module_call(inner_args)
        if call:
            return [tool, "run", inner_tool, f"-m {call[0]}"]
        pos = positionals(inner_args, {"-m", "-c", "-W", "-X"})
        return [tool, "run", inner_tool] + [os.path.basename(p.rstrip("/")) or p for p in pos[:1]]
    return [tool, "run", inner_tool] + [os.path.basename(p.rstrip("/")) or p for p in positionals(inner_args)[:1]]


def _uv_parse_strict(args: list[str]) -> tuple[str, list[str], bool, list[str]] | None:
    """Strictly parse `uv [safe-flags] build|run [safe-opts] [--] CMD`; None for unknown options.
    Returns `(subcommand, inner_argv, is_module, run_option_names)`."""
    i = 0
    top_opts: list[str] = []
    while i < len(args) and args[i].startswith("-"):
        if args[i] in UV_STRICT_TOP_FLAGS:
            top_opts.append(args[i])
            i += 1
            continue
        return None
    if i >= len(args):
        return None
    sub = args[i]
    i += 1
    if sub == "build":
        return "build", args[i:], False, top_opts
    if sub != "run":
        return None
    is_module = False
    run_opts = list(top_opts)
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg in ("-m", "--module"):
            is_module = True
            run_opts.append(arg)
            i += 1
            continue
        if arg in UV_STRICT_RUN_FLAGS:
            run_opts.append(arg)
            i += 1
            continue
        if arg in UV_STRICT_RUN_VALUE_OPTIONS:
            if i + 1 >= len(args):
                return None
            run_opts.append(arg)
            i += 2
            continue
        opt_name, eq, _ = arg.partition("=")
        if eq and opt_name in UV_STRICT_RUN_VALUE_OPTIONS:
            run_opts.append(opt_name)
            i += 1
            continue
        return None
    if i >= len(args):
        return None
    return "run", args[i:], is_module, run_opts


def _uv_honors_grant(_tool: str, args: list[str]) -> bool:
    items = positional_items(args, UV_TOP_VALUE_OPTIONS)
    if not items:
        return True
    sub_idx, sub = items[0]
    if sub != "run":
        return sub in ("build", "sync", "pip", "lock", "export", "venv", "tool")
    after = args[sub_idx + 1:]
    i = 0
    while i < len(after):
        arg = after[i]
        if arg == "--":
            i += 1
            break
        if arg in UV_RUN_VALUE_OPTIONS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        break
    if i >= len(after):
        return False
    inner_tool = os.path.basename(after[i])
    if inner_tool == "uv":
        return False
    # Only tools whose parallelism comes from inherited global_env (not direct-only argv rewrites
    # like `zig build`) honor grants when launched inside `uv run`.
    inner_spec = lookup(inner_tool)
    if not (inner_spec and inner_spec.global_env is not None):
        return False
    return honors_grant_for(inner_tool, after[i + 1:])


def _uv_restart_safe(_tool: str, args: list[str]) -> bool:
    parsed = _uv_parse_strict(args)
    if not parsed:
        return False
    sub, inner, is_module, _ = parsed
    if sub == "build":
        return True
    if is_module:
        return _python_module_restart_safe(inner[0], inner[1:])
    inner_tool = os.path.basename(inner[0])
    if inner_tool == "uv":
        return False
    return is_restart_safe(inner_tool, inner[1:])


def _uv_reusable_check(_tool: str, args: list[str]) -> bool:
    parsed = _uv_parse_strict(args)
    if not parsed:
        return False
    sub, inner, is_module, run_opts = parsed
    # Require explicit --no-sync and no refresh/reinstall/dependency-setup flags so reuse never
    # skips syncing .venv or installing requested packages.
    if sub != "run" or "--no-sync" not in run_opts or (UV_REUSE_FORBIDDEN_FLAGS & set(run_opts)):
        return False
    if is_module:
        return _python_module_reusable_check(inner[0], inner[1:])
    inner_tool = os.path.basename(inner[0])
    if inner_tool == "uv":
        return False
    return is_reusable_check(inner_tool, inner[1:])


# ---- Go -------------------------------------------------------------------------------------

GO_VALUE_OPTIONS = frozenset({
    "-C", "-o", "-p", "-asmflags", "-buildmode", "-buildvcs", "-compiler", "-gccgoflags",
    "-gcflags", "-installsuffix", "-ldflags", "-mod", "-modfile", "-overlay", "-pgo", "-pkgdir",
    "-tags", "-toolexec", "-covermode", "-coverpkg", "-bench", "-benchtime", "-blockprofile",
    "-blockprofilerate", "-count", "-coverprofile", "-cpu", "-cpuprofile", "-exec", "-fuzz",
    "-fuzzminimizetime", "-fuzztime", "-list", "-memprofile", "-memprofilerate", "-mutexprofile",
    "-mutexprofilefraction", "-outputdir", "-parallel", "-run", "-shuffle", "-skip", "-timeout",
    "-trace", "-vet", "-vettool",
})
GO_UNSAFE_RESTART_OPTIONS = frozenset({
    "-exec", "-toolexec", "-vettool", "-o", "-fuzz", "-fuzztime", "-fuzzminimizetime",
    "-coverprofile", "-cpuprofile", "-memprofile", "-blockprofile", "-mutexprofile",
    "-trace", "-outputdir",
})
GO_NON_REUSABLE_OPTIONS = GO_UNSAFE_RESTART_OPTIONS | frozenset({"-c", "-n", "-h", "-help", "-V"})


def _go_norm_flag(arg: str) -> str:
    return "-" + arg[2:] if arg.startswith("--") and len(arg) > 2 else arg


def _go_subcommand_and_args(args: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(args):
        arg = args[i]
        norm = _go_norm_flag(arg)
        if norm == "-C":
            i += 2
            continue
        if norm.startswith("-C="):
            i += 1
            continue
        if arg.startswith("-"):
            return "", []
        return arg, args[i + 1:]
    return "", []


def _go_words(tool: str, rest: list[str], _labels: Collection[str]) -> list[str]:
    sub, sub_args = _go_subcommand_and_args(rest)
    if not sub:
        return [tool]
    pkg: str | None = None
    has_race = False
    has_p = False
    tags: list[str] = []
    i = 0
    while i < len(sub_args):
        arg = sub_args[i]
        norm = _go_norm_flag(arg)
        if norm == "-args":
            break
        if norm == "-race":
            has_race = True
            i += 1
            continue
        if norm.startswith("-race="):
            if norm.split("=", 1)[1].lower() not in ("false", "0"):
                has_race = True
            i += 1
            continue
        if norm == "-p":
            has_p = True
            i += 2 if i + 1 < len(sub_args) and not sub_args[i + 1].startswith("-") else 1
            continue
        if norm.startswith("-p="):
            has_p = True
            i += 1
            continue
        if norm == "-tags":
            if i + 1 < len(sub_args):
                val = sub_args[i + 1]
                if val and val != "…":
                    tags.extend(t for t in re.split(r"[\s,]+", val) if t)
                i += 2
            else:
                i += 1
            continue
        if norm.startswith("-tags="):
            val = norm.split("=", 1)[1]
            if val and val != "…":
                tags.extend(t for t in re.split(r"[\s,]+", val) if t)
            i += 1
            continue
        if norm in GO_VALUE_OPTIONS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        if pkg is None and arg != "…":
            if arg == "./...":
                pkg = "./..."
            elif arg.startswith("/"):
                pkg = os.path.basename(arg.rstrip("/")) or "/"
            else:
                cleaned = arg.rstrip("/")
                if cleaned.startswith("./"):
                    cleaned = cleaned[2:]
                pkg = cleaned or "."
        i += 1
    words = [tool, sub]
    if pkg:
        words.append(pkg)
    if has_race:
        words.append("-race")
    if tags:
        words.append(f"-tags {','.join(sorted(set(tags)))}")
    if has_p:
        words.append("-p")
    return words


def _go_restart_safe(_tool: str, args: list[str]) -> bool:
    sub, sub_args = _go_subcommand_and_args(args)
    if sub not in ("build", "test", "vet"):
        return False
    for arg in sub_args:
        norm = _go_norm_flag(arg)
        if norm == "-args":
            break
        if norm.split("=", 1)[0] in GO_UNSAFE_RESTART_OPTIONS:
            return False
    return True


def _go_reusable_check(_tool: str, args: list[str]) -> bool:
    sub, sub_args = _go_subcommand_and_args(args)
    if sub != "vet":
        return False
    for arg in sub_args:
        norm = _go_norm_flag(arg)
        if norm.split("=", 1)[0] in GO_NON_REUSABLE_OPTIONS:
            return False
    return True


def _go_write_dirs(_tool: str, _args: list[str], cwd: str, env: dict) -> list[str]:
    dirs: list[str] = []
    for var in ("GOCACHE", "GOMODCACHE"):
        val = env.get(var)
        if val and val != "off":
            dirs.append(os.path.join(cwd, val))
    return dirs


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
        command_words=_dotnet_words,
        restart_safe=_dotnet_restart_safe,
        reusable_check=_dotnet_reusable_check,
        global_env=_dotnet_global_env,
    ),
    ToolSpec(
        names=("cmake",),
        honors_grant=True,
        honors_grant_args=_cmake_honors_grant,
        command_words=_cmake_words,
        restart_safe=_cmake_restart_safe,
        global_env=lambda g, _w, _js, _tt, _env: {"CMAKE_BUILD_PARALLEL_LEVEL": str(g)},
        write_dirs=_cmake_write_dirs,
    ),
    ToolSpec(
        names=("ctest",),
        honors_grant=True,
        honors_grant_args=_ctest_honors_grant,
        command_words=_ctest_words,
        restart_safe=_ctest_restart_safe,
        global_env=lambda g, _w, _js, _tt, _env: {"CTEST_PARALLEL_LEVEL": str(g)},
        write_dirs=_ctest_write_dirs,
    ),
    ToolSpec(
        names=("gradle", "gradlew"),
        honors_grant=True,
        honors_grant_args=_gradle_honors_grant,
        merge_env_vars=("GRADLE_OPTS",),
        command_words=_gradle_words,
        restart_safe=_gradle_restart_safe,
        # Ordinary Gradle test/check/lint tasks write XML/HTML reports and build artifacts by
        # default; let Gradle's own up-to-date checks or explicit --reuse handle reuse.
        global_env=_gradle_global_env,
        direct_env=_gradle_direct_env,
        write_dirs=_gradle_write_dirs,
    ),
    ToolSpec(
        names=("zig",),
        honors_grant=True,
        honors_grant_args=_zig_honors_grant,
        command_words=_zig_words,
        restart_safe=_zig_restart_safe,
        reusable_check=_zig_reusable_check,
        rewrite_argv=_zig_rewrite_argv,
        write_dirs=_zig_write_dirs,
        is_cold=_zig_is_cold,
    ),
    ToolSpec(
        names=("gcc", "g++", "clang", "clang++", "cc", "c++"),
        name_pattern=re.compile(r"(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+)-\d+"),
        command_words=_cc_words,
        restart_safe=_cc_restart_safe,
        reusable_check=_cc_reusable_check,
    ),
    ToolSpec(
        names=("uv",),
        honors_grant=True,
        honors_grant_args=_uv_honors_grant,
        command_words=_uv_words,
        restart_safe=_uv_restart_safe,
        reusable_check=_uv_reusable_check,
        global_env=lambda g, _w, _js, _tt, _env: {"UV_CONCURRENT_BUILDS": str(g)},
    ),
    ToolSpec(
        names=("pytest", "py.test"),
        command_words=_pytest_words,
        restart_safe=_pytest_restart_safe,
        reusable_check=_pytest_reusable_check,
        global_env=lambda g, _w, _js, _tt, _env: {"PYTEST_XDIST_AUTO_NUM_WORKERS": str(g)},
    ),
    ToolSpec(
        names=("mypy",),
        command_words=_mypy_words,
        restart_safe=_mypy_restart_safe,
        reusable_check=_mypy_reusable_check,
    ),
    ToolSpec(
        names=("pyright", "basedpyright"),
        command_words=_pyright_words,
        restart_safe=_pyright_restart_safe,
        reusable_check=_pyright_reusable_check,
    ),
    ToolSpec(
        names=("python", "python3"),
        name_pattern=re.compile(r"python3\.\d+"),
        # command_words stays None so commands.py's interpreter / script_labels logic runs unchanged.
        restart_safe=_python_restart_safe,
        reusable_check=_python_reusable_check,
    ),
    ToolSpec(
        names=("go",),
        honors_grant=True,
        command_words=_go_words,
        restart_safe=_go_restart_safe,
        reusable_check=_go_reusable_check,
        global_env=lambda g, _w, _js, _tt, _env: {"GOMAXPROCS": str(g)},
        write_dirs=_go_write_dirs,
    ),
    ToolSpec(
        names=tuple(sorted(JS_TOOLS)),
        command_words=_js_words,
    ),
    ToolSpec(
        names=("tsc", "vue-tsc"),
        not_script=False,  # kept out of NOT_SCRIPTS for backwards config.toml compatibility
        restart_safe=lambda _t, _a: True,
        reusable_check=lambda _t, a: "-w" not in a and no_emit(a),
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


def honors_grant_for(tool: str, args: list[str] | None = None) -> bool:
    spec = lookup(tool)
    if not spec:
        return False
    if args is not None and spec.honors_grant_args is not None:
        return spec.honors_grant_args(tool, args)
    return spec.honors_grant


def honors_grant_tool(tool: str) -> bool:
    return honors_grant_for(tool, None)


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
    direct = lookup(tool)
    for spec in SPECS:
        if spec.global_env and not (spec is direct and spec.direct_env is not None):
            emitted = spec.global_env(granted, want, jobserver, test_threads, caller_env)
            for k, v in emitted.items():
                values[k] = v
                if k in spec.merge_env_vars:
                    merged.add(k)
                else:
                    merged.discard(k)
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
