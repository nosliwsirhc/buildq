# SPDX-License-Identifier: MIT
"""Command classification, key generation, environment hashing, and make-flag rewriting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex

from . import tools
from .tools import (
    CARGO_VALUE_OPTIONS,
    JOBSERVER_TOOLS,
    JS_TOOLS,
    JS_VALUE_OPTIONS,
    RESTART_CARGO,
    SCRIPT_WORD,
    VERIFY_CARGO,
    asks_for_jobs,
    no_emit,
    option_values,
    positionals,
    without_jobs,
)

MAKE_FLAG_VARS = ("MAKEFLAGS", "CARGO_MAKEFLAGS", "MFLAGS")
FD_JOBSERVER = re.compile(r"--jobserver-(?:auth|fds)=-?\d+,-?\d+")
# A word of make flags as make splits them: whitespace ends a word unless a backslash escapes it (a
# directory with a space in it is written `-Ia\ b`), and a backslash escapes the character after it.
MAKE_WORD = re.compile(r"(?:\\[\s\S]?|[^ \t\\])+")  # (only spaces and tabs split: a newline is in a word)
# Options whose argument may be the next word: that word is an argument, not an option.
MAKE_ARG_OPTIONS = frozenset({"-C", "-f", "-I", "-o", "-W", "-l", "--directory", "--file", "--makefile",
                              "--include-dir", "--old-file", "--assume-old", "--what-if", "--new-file",
                              "--assume-new", "--load-average", "--max-load"})


def make_words(flags: str) -> list[str]:
    """Make flags split into words as make splits them (MAKE_WORD), each kept exactly as written: a
    backslash keeps the next character (or, last, itself) in the word."""
    return MAKE_WORD.findall(flags)


def make_options(words: list[str]) -> list[str]:
    """The words that are options (not an option's separate argument, `-I DIR`), with make's
    backslash escapes taken out, as make reads them."""
    out, argument = [], False
    for w in words:
        plain = re.sub(r"\\(.)", r"\1", w, flags=re.S)
        if not argument:
            out.append(plain)
        argument = plain in MAKE_ARG_OPTIONS
    return out


def callers_jobs(options: str) -> bool:
    """Make options that set the job count or a jobserver of the caller's own (#37: whole options,
    not a word inside an option's argument)."""
    return any(w.startswith("-j") or w.startswith(("--jobs", "--jobserver"))
               for w in make_options(make_words(options)))


def split_make_flags(flags: str) -> tuple[str, str | None]:
    """Make flags split at their first `--`: the options before it, and the variable assignments
    after it, kept exactly as written (escaped spaces and all), or None if there is no `--`. A `--`
    after an escaped space (an odd number of backslashes before it) is part of a value, not the split."""
    for m in re.finditer(r"(?:^|[ \t])--(?=[ \t]|$)", flags):
        before = flags[:m.start()]
        if (len(before) - len(before.rstrip("\\"))) % 2 == 0:
            return before, flags[m.end() + 1:]
    return flags, None


def without_fd_jobserver(flags: str) -> str | None:
    """The make flags without an inherited jobserver passed by file descriptor
    (--jobserver-auth=R,W, or the older --jobserver-fds=R,W) and the -j that came with it, or None
    if they have none (#4). Those descriptors belong to the make that ran the caller and don't reach
    the build's service: make would warn and run serially, so the build gets buildq's jobserver
    instead. A fifo jobserver (fifo:PATH) can still be opened there, so it is left alone. Only the
    options before a `--` are looked at; the assignments after it are kept as they are."""
    options, assignments = split_make_flags(flags)
    words = make_words(options)  # (an escaped space or tab stays in its word, as written: #37)
    if not any(FD_JOBSERVER.fullmatch(w) for w in words):
        return None
    kept, drop_number = [], False
    for w in words:
        if FD_JOBSERVER.fullmatch(w) or re.fullmatch(r"-j\d*", w):
            drop_number = w == "-j"  # `-j 8`: its count goes with it
            continue
        if drop_number and w.isdigit():
            drop_number = False
            continue
        drop_number = False
        kept.append(w)
    kept = " ".join(kept)
    return kept if assignments is None else f"{kept} -- {assignments}".lstrip()


def with_make_flags(flags: str, more: str) -> str:
    """Make flags with more options added: before a `--` and the variable assignments after it
    (kept exactly as written), or make would take the options for targets."""
    options, assignments = split_make_flags(flags)
    words = make_words(options)
    if words and (len(words[-1]) - len(words[-1].rstrip("\\"))) % 2:
        words[-1] += "\\"  # a lone backslash at the end (`-Ia\` is the folder `a\`) mustn't escape what comes next
    head = " ".join(words + make_words(more))  # (an escaped trailing space stays: #37)
    return head if assignments is None else f"{head} -- {assignments}".lstrip()



WRAPPERS = {"nice", "ionice", "nohup", "setsid", "stdbuf", "time", "command", "exec", "chrt",
            "taskset", "sudo", "doas"}
VALUE_OPTIONS = {"-n", "-c", "-o", "-e", "-i", "-p", "-u", "-g", "-C"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
INTERPRETERS = {"python", "python3", "node", "deno", "ruby", "perl", "bun"}
# Names restartable_scripts refuses: tools and wrappers, not scripts (listing `git` would make `git
# push` restartable).
NOT_SCRIPTS = (SHELLS | INTERPRETERS | WRAPPERS | tools.NOT_SCRIPT_TOOLS
               | {"env", "timeout", "rustc", "kubectl", "aws", "gh", "ssh", "scp", "rsync",
                  "curl", "wget", "xargs", "parallel", "busybox", "uvx", "just", "mvn",
                  "terraform", "jest", "sbt", "heavy-run", "buildq",
                  "flock", "systemd-run", "run0", "pkexec", "nix-shell", "firejail", "xvfb-run",
                  "strace", "valgrind", "tsx", "ts-node", "zx", "pwsh", "nodejs"})
# (and any versioned interpreter or shell: python2, python3.12, pypy3, node22, bash5 …; the list can't
# be complete)
NOT_SCRIPT_PATTERN = re.compile(r"(python|pypy|node|ruby|perl|php|lua|bash|zsh|ksh|dash|sh|fish)[0-9.]*")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTHON_VALUE_OPTIONS = {"-m", "-c", "-W", "-X"}  # python's options that take the next word
SAFE_WORD = re.compile(r"^@?[A-Za-z0-9_.,+:%/-]{1,64}$")  # "@scope/pkg" is an npm package, not an address


def env_command(args: list[str]) -> list[str] | None:
    """What `env ARGS` runs, read as GNU env reads it: its options first (up to `--` or the first
    word that isn't one), then assignments (any word with "="), then the command. Only options whose
    reading is exact are read: -i, -0, -v (grouped too), --ignore-environment, --null, --debug, a lone
    `-`, and -u/-C with their value apart or attached (-uX, --unset=, --chdir=). None for any other
    (grouped with a value, abbreviated, -a, -S/--split-string, even with "=" in it): the command can't
    be told for certain."""
    rest = list(args)
    while rest and rest[0].startswith("-") and rest[0] != "-":
        option = rest.pop(0)
        if option == "--":
            break
        if option in ("--ignore-environment", "--null", "--debug") or re.fullmatch(r"-[i0v]+", option):
            continue
        if option in ("-u", "--unset", "-C", "--chdir") and rest:
            rest.pop(0)  # its value, written apart
            continue
        if re.fullmatch(r"(-u|-C).+|(--unset|--chdir)=.*", option):
            continue  # its value, written with it (an empty one fails, or a later -C wins: the command is the same)
        return None
    if rest[:1] == ["-"]:  # (env - …: the same as -i)
        rest = rest[1:]
    while rest and "=" in rest[0]:  # GNU env takes any word with "=" for an assignment
        rest = rest[1:]
    return rest


def strip_wrappers(argv: list[str]) -> list[str]:
    t = list(argv)
    while t:
        head = os.path.basename(t[0])
        if ASSIGNMENT.match(t[0]):
            t = t[1:]
        elif head == "env":
            rest = env_command(t[1:])
            if rest is None:
                return t  # env's command can't be told for certain: leave env as the command, unread
            t = rest
        elif head == "timeout":
            t = t[1:]
            while t and t[0].startswith("-"):
                option = t.pop(0)
                if option in ("-s", "-k", "--signal", "--kill-after") and t:
                    t.pop(0)
            t = t[1:]  # the duration
        elif head in WRAPPERS:
            t = t[1:]
            while t and t[0].startswith("-"):
                option = t.pop(0)
                if option in VALUE_OPTIONS and t:
                    t.pop(0)
            if head in ("taskset", "chrt"):
                while t and re.fullmatch(r"[0-9a-fA-Fx,\-]+", t[0]):
                    t.pop(0)
        else:
            break
    return t


def git_toplevel(cwd: str) -> str | None:
    d = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def repo_name(cwd: str) -> str:
    """The name of the git repository that holds cwd, the same for every worktree of it."""
    top = git_toplevel(cwd)
    if top is None:
        return os.path.basename(os.path.abspath(cwd).rstrip("/")) or "/"
    dot_git = os.path.join(top, ".git")
    if os.path.isdir(dot_git):
        return os.path.basename(top) or "/"
    try:
        with open(dot_git) as f:
            gitdir = os.path.normpath(os.path.join(top, f.read().split("gitdir:", 1)[1].strip()))
        common = gitdir
        commondir = os.path.join(gitdir, "commondir")
        if os.path.isfile(commondir):
            with open(commondir) as f:
                common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
        if os.path.basename(common) == ".git":
            return os.path.basename(os.path.dirname(common))
        return re.sub(r"\.git$", "", os.path.basename(common)) or os.path.basename(top)
    except (OSError, IndexError):
        return os.path.basename(top)


def command_words(argv: list[str], labels: "frozenset[str] | tuple" = ()) -> list[str]:
    t = strip_wrappers(argv)
    if not t:
        return ["?"]
    # `uv run -- CMD` uses `--` before its inner command; `_uv_words` handles both delimiters.
    if "--" in t and os.path.basename(t[0]) != "uv":
        t = t[: t.index("--")]
    # Nothing that could hold a credential reaches a key, not even through basename().
    t = [t[0]] + tools.redact_args(t[1:])
    tool = os.path.basename(t[0])
    rest = t[1:]
    matched = tools.words_for(tool, rest, labels)
    if matched is not None:
        words = matched
    elif tool in SHELLS or re.fullmatch(r"python3(\.\d+)?", tool) or tool in INTERPRETERS:
        words = [tool]
        if rest[:1] and not rest[0].startswith("-") and os.path.basename(rest[0]) in labels:
            # A script named in script_labels takes a label as the argument right after it, naming
            # what it runs (`persistent-cargo-target.sh architecture …` and `… rust-tests …` are
            # different builds of different sizes). Only when the script comes straight after the
            # interpreter (`bash wrap.sh label`: not a command string, `-lc wrap.sh`, or an option's
            # value, `--require wrap.js`), whatever its own arguments are, and only a plain
            # lowercase word.
            words.append(os.path.basename(rest[0]))
            if rest[1:] and SCRIPT_WORD.fullmatch(rest[1]):
                words.append(rest[1])
        elif "-c" in rest:
            words.append("-c")
            i = rest.index("-c")
            if i + 1 < len(rest):
                script = [w for w in rest[i + 1].split() if not ASSIGNMENT.match(w)]
                words += [os.path.basename(w) for w in script[:2]]
        else:
            values = PYTHON_VALUE_OPTIONS if "python" in tool else set()
            pos = positionals(rest, values)
            # (only python has -m modules; written as `-m MODULE`, so a key keys back to itself, #49;
            # only the interpreter's own -m, before the script: `script.py -m slow` is the script's)
            first, i = len(rest), 0
            while i < len(rest):
                if rest[i] in values:
                    i += 2
                elif rest[i].startswith("-"):
                    i += 1
                else:
                    first = i
                    break
            module = [f"-m {m}" for m in option_values(rest[:first], {"-m"})[:1]] if "python" in tool else []
            words += module + [os.path.basename(p) for p in pos[:1]]
    else:
        words = [tool] + [os.path.basename(p) for p in positionals(rest)[:2]]
    # Keys are logged and kept; nothing that could be a secret goes into one.
    return [w if all(SAFE_WORD.match(part) and not TOKEN_LIKE.match(part) for part in w.split(" ")) else "…"
            for w in words]


WRITING_FLAG = re.compile(r"--(fix|fix-type|write|bless|update|update-snapshots|updateSnapshot|watch|watchAll|"
                          r"allow-dirty|no-run)(=.*)?")
INFO_FLAGS = frozenset({"--help", "-h", "--version"})
# Options that write a file the caller may read next (a report, coverage, test results): a reused
# pass wouldn't write it.
REPORT_OPTIONS = {"-o", "--output-file", "--outputFile", "--coverage", "--logger", "--results-directory",
                  "--collect", "--diag", "--junit", "--reporter-file"}


def _tool_and_args(argv: list[str]) -> tuple[str, list[str]]:
    args = strip_wrappers(argv)
    if args and os.path.basename(args[0]) in ("npx", "pnpx", "bunx"):
        args = [a for a in args[1:] if a not in ("-y", "--yes", "--no-install")]
    return (os.path.basename(args[0]) if args else ""), args[1:]


def reusable_check(argv: list[str]) -> bool:
    """A command whose only result is a verdict on the source: cargo check, clippy, test or nextest
    run, dotnet test, `tsc --noEmit`, eslint, `vitest run`, `zig test`/`ast-check`,
    `gcc`/`clang -fsyntax-only`, `pytest`, `python -m unittest`/`pytest`/`mypy`/`pyright`, `mypy`,
    `pyright`, `uv run --no-sync <check>`, with nothing that writes (--fix, -u,
    --update-snapshots, watch modes, --no-run, which keeps the test binaries for later) or asks for
    help/version. A second pass on the same source tells nothing the first didn't, so the first
    answers it. Commands that produce files (builds, docs, bundles) and package scripts, which might
    do anything, never do."""
    tool, args = sure_tool_and_args(argv)  # (a wrapper with options: no tool, so no reuse)
    if any(a in INFO_FLAGS or WRITING_FLAG.fullmatch(a) or writes_report(a) for a in args):
        return False
    if any(a in ("--reporter", "--reporters") and writes_report(f"{a}={b}") for a, b in zip(args, args[1:])):
        return False  # `--reporter html`
    return tools.is_reusable_check(tool, args)


def writes_report(arg: str) -> bool:
    """An option that writes a file a reused pass wouldn't: a report, coverage, test results,
    in any of its spellings (--logger:trx, --outputFile.junit=x, --coverage.enabled, --reporter=html)."""
    name = re.split(r"[=:.]", arg, maxsplit=1)[0]
    if name in REPORT_OPTIONS:
        return True
    return bool(re.fullmatch(r"--reporter(s)?[=:]?(html|junit|json|lcov|teamcity|blob).*", arg))


SURE_WRAPPERS = {"nice", "ionice", "nohup", "setsid", "stdbuf", "time", "command", "exec", "sudo", "doas"}


def sure_tool_and_args(argv: list[str]) -> tuple[str, list[str]]:
    """The tool and its arguments for a decision that must not be fooled (restart_safe,
    reusable_check): read with plainly, so a wrapper with options yields no tool at all ("", [])
    rather than a guess (`time -f eslint bash deploy.sh` runs deploy.sh, not eslint)."""
    t = plainly(argv)
    if not t:
        return "", []
    if os.path.basename(t[0]) == "pnpm" and t[1:2] == ["exec"]:
        # A binary from node_modules/.bin, never a package script. pnpm's own options (before or
        # after exec: a filter, -r, -c's shell) aren't read: none of them is a plain `pnpm exec BIN`.
        if len(t) < 3 or t[2].startswith("-"):
            return "", []
        return os.path.basename(t[2]), t[3:]
    if os.path.basename(t[0]) in ("npx", "pnpx", "bunx"):
        t = [t[0]] + [a for a in t[1:] if a not in ("-y", "--yes", "--no-install")]
        return (os.path.basename(t[1]) if len(t) > 1 else ""), t[2:]
    return os.path.basename(t[0]), t[1:]


def plainly(argv: list[str]) -> list[str] | None:
    """The command, if it can be read for certain: past `env` (as env_command reads it),
    `timeout DURATION`, and the wrappers in SURE_WRAPPERS given no option, with the assignments
    `sudo` takes before its command. None for anything else: a wrapper's options might take a value
    that could be read as the command (`env -iu wrap.sh bash deploy.sh` runs deploy.sh), and a
    command is run, not read by a shell, so a word with "=" anywhere else is a program's path
    (`X=./deploy.sh cargo test` runs ./deploy.sh in the folder "X=.")."""
    t = list(argv)
    while t:
        head = os.path.basename(t[0])
        if "=" in t[0]:
            return None  # not an assignment here: a path, or sudo 'X-Y=./wrap.sh' (not one sudo takes)
        elif head == "env":
            t = env_command(t[1:])  # (options first, as env reads them: `--split-string=…` is one)
            if t is None:
                return None
        elif head == "timeout":
            if len(t) < 3 or t[1].startswith("-"):
                return None
            t = t[2:]
        elif head in SURE_WRAPPERS or head in WRAPPERS:
            if head not in SURE_WRAPPERS or (t[1:2] and t[1].startswith("-")):
                return None
            t = t[1:]
            if head == "sudo":
                while t and ASSIGNMENT.match(t[0]):  # sudo VAR=value … sets them for the command
                    t = t[1:]
        else:
            return t
    return None


def listed_script(argv: list[str], scripts) -> list[str] | bool | None:
    """For a script named in restartable_scripts, read for certain (plainly) and run directly or
    straight after its interpreter: True, or, if it runs the command after its `--` (a wrapper that
    sets up a target folder, say), that command, which decides. None if it isn't one. A `--`
    followed by an option (`run-tests.sh -- --nocapture`) passes arguments, not a command."""
    t = plainly(argv)
    if not t:
        return None
    tool, args = os.path.basename(t[0]), t[1:]
    if tool in scripts:
        script_args = args
    elif (tool in SHELLS or re.fullmatch(r"python3(\.\d+)?", tool) or tool in INTERPRETERS) and args[:1] \
            and not args[0].startswith("-") and os.path.basename(args[0]) in scripts:
        script_args = args[1:]
    else:
        return None
    if "--" in script_args:
        command = script_args[script_args.index("--") + 1:]
        if command and command[0].startswith("--") and command[0] != "--":
            return True  # its own long options (`run-tests.sh -- --nocapture`), not a command
        return command  # the command it runs (a short option or another `--` fails safe); empty: no
    return True


def restart_safe(argv: list[str], scripts: "frozenset[str] | tuple" = ()) -> bool:
    """A command that can be stopped and started again from the beginning without doing anything
    twice: a compile or a test run of cargo, dotnet, tsc, eslint or vitest, or a script named in
    restartable_scripts (see listed_script; a wrapper is only as safe as the command it runs). Any
    other script might upload or push before it compiles, so it is stopped and failed instead
    (unless --restartable)."""
    for _ in range(8):  # (a wrapper running a wrapper: at most this deep)
        inner = listed_script(argv, scripts) if scripts else None
        if inner is None:
            break
        if inner is True:
            return True
        if not inner:
            return False
        argv = inner
    else:
        return False
    tool, args = sure_tool_and_args(argv)
    return tools.is_restart_safe(tool, args)


# Options that move a command to another folder, or the folder its paths are read from: -C in any
# spelling (env, make, cargo, git, ninja; grouped, -iC, or with its value run on); --chdir (env,
# sudo) and --directory (make, uv) with the prefixes GNU getopt takes for them (--ch, --dir);
# vitest's --root and --dir, pytest's --rootdir, npm's --prefix, yarn's and bun's --cwd, gradle's
# --project-dir, and ctest's --test-dir.
CHANGES_FOLDER = re.compile(r"-[A-Za-z0-9]*C.*|--c(h(d(i(r)?)?)?)?(=.*)?|--di(r(e(c(t(o(r(y)?)?)?)?)?)?)?(=.*)?"
                            r"|--(root|rootdir|prefix|cwd|project-dir|test-dir)(=.*)?|-working-directory(=.*)?")
OUTPUT_OPTIONS = ("--target-dir", "--cache-location")  # where a tool writes, not what it reads: not checked
OUTPUT_VARIABLES = ("CARGO_TARGET_DIR=",)
# Variables that name tools, not source (where to find them, a compiler cache in front of rustc): a
# check reads the tree it was run in whichever cargo or sccache runs it (#48). Only system and
# installed-tool folders: a PATH into a sibling checkout may run a tool being built there. Any other wrapper
# (RUSTC_WRAPPER=/x/w.sh) runs for every compile and might read anything, and CARGO_HOME's config
# can point dependencies at a folder: those are still checked.
TOOL_FOLDER = r"(/usr(/local)?/s?bin|/s?bin|/home/[^/]+/\.cargo/bin|/home/[^/]+/\.local/bin)"
TOOL_FOLDER_RE = re.compile(rf"^{TOOL_FOLDER}(/.*)?$")
TOOL_VARIABLE = re.compile(rf"PATH={TOOL_FOLDER}(:{TOOL_FOLDER})*|RUSTUP_HOME=/home/[^/]+/\.rustup|"
                           r"(RUSTC_WRAPPER|RUSTC_WORKSPACE_WRAPPER)="
                           r"(sccache|/usr(/local)?/bin/sccache|/home/[^/]+/\.cargo/bin/sccache)")
CC_TOOL_VARIABLE = re.compile(rf"(CC|CXX)=({TOOL_FOLDER}/)?(cc|c\+\+|gcc|g\+\+|clang|clang\+\+)(-\d+)?")


def _trusted_tool_variable(arg: str) -> bool:
    if TOOL_VARIABLE.fullmatch(arg):
        return True
    if not CC_TOOL_VARIABLE.fullmatch(arg):
        return False
    val = arg.split("=", 1)[1]
    if "/" not in val:
        return True  # bare compiler name; resolved via PATH
    # Resolve symlinks so a ~/.local/bin/clang -> ~/Code/llvm/bin/clang link isn't exempted.
    return bool(TOOL_FOLDER_RE.fullmatch(os.path.realpath(val)))
# What a tool interprets rather than reads as it is: quoting, escapes (TOML's \u002f is a "/"),
# expansion, structured values, URLs. A word with any of these can't be read for certain.
UNREADABLE = re.compile(r"""[\x00-\x1f\\"'`$\[\]{}]|://""")  # (and control characters: cargo splits at \x1f)


def path_pieces(arg: str, tool: str = "", operand: bool = False) -> list[str]:
    """Every part of a plain word (see UNREADABLE) that might name a path: the word itself (a name
    with a space in it, `external project/x`), what follows each "=" in it, and each part between
    the separators a value may hold (spaces, ",", ";", ":", "="). Where a short option's name ends
    and its value begins can't be told (-Lvendor, -isystemvendor, -isystem/x), so every place the
    value might begin counts. An operand (after `--`) is a name even if it starts with "-". A
    response file's @ is taken off. For dotnet, an MSBuild switch (/p:…) is an option."""
    if tool in ("dotnet", "msbuild"):
        arg = re.sub(r"^/[A-Za-z]+:", "", arg)
    out = [arg] if operand or not arg.startswith("-") else []
    out += [arg[i + 1:] for i, c in enumerate(arg) if c == "="]
    pieces = re.split(r"[\s,;:=]+", arg)
    if re.match(r"-[^-]", arg):
        pieces.append(arg)  # whole, too: `-Lext deps/lib`
    for piece in pieces:
        piece = piece.lstrip("@")
        if piece.startswith("--"):
            continue  # an option's name (its value, after "=", is a piece of its own)
        if piece.startswith("-"):
            out += [piece[i:] for i in range(2, len(piece))]
        else:
            out.append(piece)
    return [p.lstrip("@") for p in out if p and (operand or not p.startswith("-"))]


def wrapper_settings(argv: list[str]) -> set[int]:
    """Where in a command its wrappers set variables: env's assignments (after its options, as
    env_command reads them) and sudo's VAR=value. None anywhere else: a command is run, not read by
    a shell, so a word with "=" before it or after another wrapper (`nice PATH=../x/check.sh`) is a
    program's path. None at all for a command that can't be read for certain (plainly)."""
    if plainly(argv) is None:
        return set()
    out, i = set(), 0
    while i < len(argv):
        head = os.path.basename(argv[i])
        if head == "env":
            rest = env_command(argv[i + 1:])
            command = len(argv) - len(rest)
            k = command - 1
            while k > i and "=" in argv[k] and not argv[k].startswith("-"):
                out.add(k)  # the assignments: the words with "=" just before the command, after any options
                k -= 1
            i = command
        elif head == "timeout":
            i += 2
        elif head in SURE_WRAPPERS:
            i += 1
            while head == "sudo" and i < len(argv) and ASSIGNMENT.match(argv[i]):
                out.add(i)
                i += 1
        else:
            break
    return out


def moving_words(argv: list[str]) -> list[str]:
    """The words of a command that may move it to another folder (CHANGES_FOLDER): all but those of
    a known tool's non-chdir options whose value or flag name may contain `C` (cargo `-pCore`/`-Fcli`,
    C/C++ `-fPIC`/`-DCONFIG=1`/`-ICore`, Gradle `-D...`/`-P...`, pytest `-k...`/`-m...`, mypy
    `-p...`/`-m...`) (#48). What follows cargo's `--` is still read: a custom test harness may take -C."""
    t = strip_wrappers(argv)
    at = len(argv) - len(t)
    if not t:
        return argv[1:]
    tool = os.path.basename(t[0])
    if tool == "cargo":
        end = t.index("--") if "--" in t else len(t)
        return argv[1:at] + [a for i, a in enumerate(t[1:], 1) if i >= end or a[:2] not in ("-p", "-F", "-j", "-Z")]
    spec = tools.lookup(tool)
    if (spec and "gcc" in spec.names) or (tool == "zig" and t[1:2] in (["cc"], ["c++"])):
        exempt = ("-f", "-W", "-D", "-U", "-I", "-L", "-l", "-m", "-O", "-std=", "-g", "-x")
        return argv[1:at] + [a for a in t[1:] if not a.startswith(exempt)]
    if tool in ("gradle", "gradlew"):
        return argv[1:at] + [a for a in t[1:] if not a.startswith(("-D", "-P"))]
    if tool in ("pytest", "py.test"):
        return argv[1:at] + [a for a in t[1:] if not a.startswith(("-k", "-m"))]
    if tool == "mypy":
        return argv[1:at] + [a for a in t[1:] if not a.startswith(("-p", "-m"))]
    return argv[1:]


def _uses_pytest(tool: str, args: list[str]) -> bool:
    if tool in ("pytest", "py.test"):
        return True
    if tool in ("python", "python3") or re.fullmatch(r"python3\.\d+", tool):
        call = tools._python_module_call(args)
        return bool(call and call[0] == "pytest")
    if tool == "uv":
        parsed = tools._uv_parse_strict(args)
        if parsed and parsed[0] == "run":
            _, inner, is_module, _ = parsed
            if is_module:
                return inner[0] == "pytest"
            return _uses_pytest(os.path.basename(inner[0]), inner[1:])
    return False


def paths_inside(argv: list[str], cwd: str, top: str, env: dict | None = None) -> bool:
    """Every path the command names is inside the tree whose source was recorded, so an earlier
    result for the same source answers for it. A path is any part of any word (path_pieces) with a
    slash or starting with . or ~, or one that names something that exists (`-p external`, a
    symlink), resolved through links from the build's folder. Three kinds match nothing:
    - a command with a word buildq can't read for certain (UNREADABLE: quotes, escapes, brackets,
      $, a URL; `--config 'paths=["../other"]'`): it is refused, not guessed at;
    - one that moves to another folder, or the folder its paths are read from (env -C, --chdir,
      make -C, vitest --root, clang -working-directory, uv UV_WORKING_DIR/UV_PROJECT, sudo given
      options): what its paths name can't be told here;
    - one pointed elsewhere (`--manifest-path ../other/Cargo.toml`, `RUSTC_WRAPPER=/x/w.sh`,
      `RUSTFLAGS=-L/x`, `CFLAGS=-isystem/x`).
    An output folder (--target-dir, CARGO_TARGET_DIR, eslint's --cache-location) is where it writes,
    not what it reads, and nor are variables saying where tools are (TOOL_VARIABLE) where a wrapper
    sets them.
    Source that no word names (a path dependency, a [patch] in a config file, a symlinked folder
    inside the tree) isn't seen: --again runs such a build regardless."""
    root = os.path.realpath(top)
    words = argv[1:]
    stripped = strip_wrappers(argv)
    tool = os.path.basename(stripped[0]) if stripped else ""
    if any(UNREADABLE.search(a) for a in argv):
        return False  # what it names can't be read for certain
    if any(CHANGES_FOLDER.fullmatch(a) for a in moving_words(argv)):
        return False
    basenames = {os.path.basename(a) for a in argv}
    setting = wrapper_settings(argv)  # where a wrapper sets variables (env X=1, sudo X=1)
    effective_env = dict(env) if env else {}
    for idx in sorted(setting):
        if "=" in argv[idx]:
            k, v = argv[idx].split("=", 1)
            effective_env[k] = v
    if "vitest" in basenames and any(re.fullmatch(r"-[A-Za-z]*r(=.*)?", a) for a in words):
        return False  # vitest's short --root, grouped (-sr) or with its value (-r=..) too
    if {"gradle", "gradlew"} & basenames and any(re.fullmatch(r"-[qidswamtuS]*p.*", a) for a in words):
        return False  # gradle's short --project-dir (standalone or clustered, e.g. -qp)
    if "uv" in basenames:
        if any(a == "--project" or a.startswith("--project=") for a in words):
            return False  # uv's --project moves project discovery to another folder
        if any(effective_env.get(k) for k in ("UV_WORKING_DIR", "UV_PROJECT", "UV_ENV_FILE")):
            return False  # env redirects uv to another directory or external .env file
    if effective_env.get("PYTEST_ADDOPTS") and _uses_pytest(tool, stripped[1:]):
        try:
            extra = shlex.split(str(effective_env["PYTEST_ADDOPTS"]))
        except ValueError:
            return False
        if not reusable_check(["pytest", *extra]) or not paths_inside(["pytest", *extra], cwd, top):
            return False
    if any(os.path.basename(a) == "sudo" and b.startswith("-") for a, b in zip(argv, argv[1:])):
        return False  # sudo's options include -D, its own --chdir
    skip = operands = False
    named = [argv[0]] if "/" in argv[0] and not argv[0].startswith("/") else []  # ../other/check.sh
    for i, arg in [(0, a) for a in named] + list(enumerate(words, 1)):
        if skip:
            skip = False
            continue
        if arg == "--" and not operands:
            operands = True  # what follows are names, whatever they start with (eslint -- --x.js)
            continue
        if arg in OUTPUT_OPTIONS:
            skip = True
            continue
        if arg.startswith(tuple(o + "=" for o in OUTPUT_OPTIONS) + OUTPUT_VARIABLES):
            continue
        if i in setting and _trusted_tool_variable(arg):
            continue  # a variable saying where tools are, set for the command (not an argument to it)
        for value in path_pieces(arg, tool, operands):
            candidate = os.path.join(cwd, os.path.expanduser(value))
            looks = "/" in value or value.startswith((".", "~"))
            if not looks and not os.path.lexists(candidate):
                continue  # a word (a subcommand, a test name), not a path
            path = os.path.realpath(candidate)
            if path != root and not path.startswith(root + os.sep):
                return False
    return True


# Variables that differ between sessions, terminals or runs but not in what a build does. Anything
# else counts, secrets included (only a hash of the whole environment is kept).
# What git reads to see a tree as its user does: where its configuration and ignore files are.
GIT_ENV = re.compile(r"HOME|PATH|XDG_CONFIG_HOME|GIT_CONFIG(_GLOBAL|_SYSTEM|_NOSYSTEM|_COUNT|_KEY_\d+|_VALUE_\d+)?|"
                     r"GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_INDEX_FILE|GIT_CEILING_DIRECTORIES|"
                     r"GIT_DISCOVERY_ACROSS_FILESYSTEM|GIT_ATTR_NOSYSTEM|GIT_OBJECT_DIRECTORY|"
                     r"GIT_ALTERNATE_OBJECT_DIRECTORIES")

VOLATILE_ENV = re.compile(
    r"PWD|OLDPWD|SHLVL|_|COLUMNS|LINES|WINDOWID|STY|TMUX|TMUX_PANE|TERM_SESSION_ID|SSH_AUTH_SOCK|SSH_AGENT_PID|"
    r"SSH_CLIENT|SSH_CONNECTION|SSH_TTY|GPG_TTY|DBUS_SESSION_BUS_ADDRESS|XDG_SESSION_ID|XDG_ACTIVATION_TOKEN|"
    r"INVOCATION_ID|JOURNAL_STREAM|MANAGERPID|MANAGERPIDFDID|SYSTEMD_EXEC_PID|NOTIFY_SOCKET|LISTEN_(PID|FDS|FDNAMES)|"
    r"CLAUDE_PID|CLAUDE_CODE_(SESSION_ID|CHILD_SESSION|SESSION_ATTENDED|MESSAGING_SOCKET|MESSAGING_TOKEN)|"
    r"T3CODE_THREAD_ID|CODEX_(THREAD|SESSION)_ID|"
    r"ANTIGRAVITY_(AGENT|AGENTAPI_EXE|APP_DATA_DIR|CONVERSATION_ID|CSRF_TOKEN|LS_ADDRESS|LS_VERSION|PROJECT_ID|SOURCE_METADATA|TRAJECTORY_ID)|"
    r"KITTY_WINDOW_ID|WEZTERM_PANE|VSCODE_GIT_IPC_HANDLE|VSCODE_IPC_HOOK_CLI|BUILDQ_(AS|FALLBACK|SOURCE|CALLER|SOCKET|BIN)")


def env_digest(env: dict[str, str]) -> str:
    kept = sorted((k, v) for k, v in env.items() if not VOLATILE_ENV.fullmatch(k))
    return hashlib.sha256(json.dumps(kept).encode()).hexdigest()


def dedupe_key(source: dict, argv: list[str], cwd: str, lane: str, env: dict[str, str], settings: list) -> str:
    """The same command, on the same source, in the same directory, lane, environment and settings
    (its timeout, umask, limits, given cores and memory, and the daemon's own [env])."""
    parts = [source.get("commit"), source.get("fingerprint"), argv, os.path.realpath(cwd), lane, env_digest(env),
             settings]
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def sane_source(raw) -> dict | None:
    """The client's record of the source a build is of, if it is well formed."""
    if not isinstance(raw, dict):
        return None
    commit, fingerprint, repo = raw.get("commit"), raw.get("fingerprint"), raw.get("repo")
    if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", commit)):
        return None
    if not (isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{16,64}", fingerprint)):
        return None
    remote = raw.get("remote")
    if not (isinstance(remote, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote)):
        remote = None
    return {"repo": repo if isinstance(repo, str) and os.path.isabs(repo) else None, "commit": commit,
            "dirty": bool(raw.get("dirty")), "fingerprint": fingerprint, "remote": remote}


def valid_post(post) -> bool:
    return (isinstance(post, dict) and all(isinstance(post.get(k), str) for k in ("repo", "sha", "state", "context",
                                                                             "description"))
            and all(isinstance(post.get(k), (int, float)) for k in ("first", "next")))


def sane_caller(raw) -> str | None:
    """Who submitted a build: the unit (a session's scope, a CI runner's service) its client ran in."""
    if isinstance(raw, str) and 0 < len(raw) <= 200 and raw.isprintable():
        return raw
    return None



def command_key(argv: list[str], cwd: str, lane: str, labels: "frozenset[str] | tuple" = ()) -> str:
    return f"{repo_name(cwd)}|{lane}|{' '.join(command_words(argv, labels))}"


def workspace_of(cwd: str, env: dict) -> str:
    """What a build writes to, so two builds that would fight over it run one at a time: its
    CARGO_TARGET_DIR if it has one, else its git worktree, else its directory."""
    target = env.get("CARGO_TARGET_DIR")
    if target:
        return "target:" + os.path.realpath(os.path.join(cwd, target))
    top = git_toplevel(cwd)
    if top in (None, "/", os.path.expanduser("~")):  # a dotfiles repo isn't a workspace
        top = cwd
    return "tree:" + os.path.realpath(top)


def within_home(path: str) -> str | None:
    """`path` resolved, if it lies inside the user's home (not home itself); else None. The
    folders `buildq top` measures are kept to these."""
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(path)
    return real if real.startswith(home + os.sep) else None


def write_dirs_of(cwd: str, env: dict, argv: list[str]) -> list[str]:
    """Where a build fills the disk, for `buildq top`: its CARGO_TARGET_DIR, tool-specific build
    output folders (e.g. target/ beside Cargo.lock), and its TMPDIR, in the environment it runs with.
    Only folders inside the user's home count, and none inside another."""
    found = []
    target = env.get("CARGO_TARGET_DIR")
    if target:
        found.append(os.path.join(cwd, target))
    stripped = strip_wrappers(argv)
    if stripped:
        found.extend(tools.write_dirs_for(os.path.basename(stripped[0]), stripped[1:], cwd, env))
    if env.get("TMPDIR"):
        found.append(os.path.join(cwd, env["TMPDIR"]))
    kept: list[str] = []
    for real in sorted({r for r in map(within_home, found) if r}, key=len):
        if not any(real == k or real.startswith(k + os.sep) for k in kept):
            kept.append(real)
    return kept


def disk_paths_of(cwd: str, env: dict) -> list[str]:
    """Where a build writes: its directory, its CARGO_TARGET_DIR and its TMPDIR, as far as they
    exist (or their nearest existing parent), each filesystem once."""
    paths, devices = [], set()
    for raw in (cwd, env.get("CARGO_TARGET_DIR"), env.get("TMPDIR")):
        if not raw:
            continue
        path = os.path.abspath(os.path.join(cwd, raw))
        while not os.path.exists(path) and os.path.dirname(path) != path:
            path = os.path.dirname(path)
        try:
            dev = os.stat(path).st_dev
        except OSError:
            continue
        if dev not in devices:
            devices.add(dev)
            paths.append(path)
    return paths


def is_cold(argv: list[str], cwd: str, env: dict) -> bool:
    """Whether a build looks cold (no existing output for its profile yet)."""
    stripped = strip_wrappers(argv)
    if not stripped:
        return False
    return tools.is_cold_for(os.path.basename(stripped[0]), stripped[1:], cwd, env)


def cargo_cold(argv: list[str], cwd: str, env: dict) -> bool:
    """Cargo-only wrapper over `is_cold` (kept for callers expecting Cargo-only semantics)."""
    stripped = strip_wrappers(argv)
    if not stripped or os.path.basename(stripped[0]) != "cargo":
        return False
    return tools.is_cold_for("cargo", stripped[1:], cwd, env)


SECRET_IN_URL = re.compile(r"(://[^/\s:@]+:)[^@\s/]+@")


SECRET_OPTION = re.compile(r"^(--?[A-Za-z0-9_-]*(?:key|token|secret|passw|pass|auth|cred)[A-Za-z0-9_-]*=).+$", re.I)
TOKEN_LIKE = re.compile(r"^(?:gh[pousr]_|github_pat_|sk-|xox[abpr]-|AKIA|glpat-)\S+$|^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9+_=-]{32,}$")


def redacted_label(argv: list[str], limit: int = 300) -> str:
    """The command for display, with NAME=value assignments, secret-looking options and words, and
    passwords in URLs hidden."""
    shown = []
    for arg in argv:
        arg = re.sub(r"(^|\s)([A-Za-z_][A-Za-z0-9_]*)=\S*", r"\1\2=…", arg)
        arg = SECRET_OPTION.sub(r"\1…", arg)
        if TOKEN_LIKE.match(arg):
            arg = "…"
        shown.append(SECRET_IN_URL.sub(r"\1…@", arg))
    text = shlex.join(shown)
    return text if len(text) <= limit else text[: limit - 1] + "…"

