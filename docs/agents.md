# Coding agents

Coding agents are the case buildq was built for. This page covers making the queue the default for them, teaching them to use it, and moving over from a slot script.

## Keeping builds in the queue

Telling agents to use the queue works most of the time. Two optional pieces make it the default:

- **Shims** (`./install.sh --shims`, then put `~/.local/share/buildq/shims` first on `PATH`, after
  anything else that changes it, such as nvm or rustup). The shims stand in for `cargo`, `pnpm`,
  `npm`, `npx`, `yarn`, `make`, `ninja` and `dotnet`. A heavy command runs as `buildq run -- <the
  real tool> ...`: any cargo subcommand that compiles (everything but `fmt`, `metadata`, `tree`,
  `clean` and the like, external subcommands included), npm and pnpm scripts named like
  `build*`, `test*`, `lint*` or `e2e*`, `vitest run`, `make` (except long-running targets such as
  `dev` or `up`), `dotnet build/test`. Anything else, and any watch mode, runs directly, as does
  everything inside a build, in GitHub Actions jobs (`GITHUB_ACTIONS` and `RUNNER_NAME` set; `CI=true`
  alone isn't enough, since people set it to turn watch modes off), and under rust-analyzer.
  `cargo run` and `dotnet run` build in the queue, then run the program directly, with stdin.
  `BUILDQ_SHIM=off` turns them off for one command.
- **The Rust guard** (`./install.sh --rustc-guard=~/src,~/work`). Cargo runs every `rustc` under those
  directories through `buildq-rustc-guard`, which refuses to compile unless the process is inside a
  buildq build. It checks the build's cgroup, which the kernel sets and an environment variable
  can't fake. It lets through version queries, GitHub Actions jobs, editors
  (`rust-analyzer`), and anything listed in `~/.config/buildq/guard.env`, where `RUN_WITH` also sets
  the command its refusal tells you to use (default `buildq run --`). Prefer directories to the
  bare `--rustc-guard`, which writes `$CARGO_HOME/config.toml`: that file is also read by CI jobs on
  the machine and by containers that mount `~/.cargo`, where the wrapper's path doesn't exist.

> [!WARNING]
> Neither is a security boundary: anyone can call a tool by its full path or point `RUSTC_WRAPPER`
> elsewhere. They keep well-meaning agents and scripts in line. A hard lock needs root: run the
> agents as a user that can't execute compilers and can only submit to the queue.

## How agents learn to use it

They learn to use it in three places, from least
to most effort for them:

- **Where they hit it.** With the shims and the Rust guard, an agent's plain `cargo test` goes
  through the queue, or is refused with the command to use instead. buildq's own messages say what
  to do next: `buildq run` prints the ticket and how to look at it, warns when the command's history
  says it usually runs longer than a tool call may last (and how to drop it off instead), and
  `buildq submit` says how to wait for its ticket. A wrapper sets `BUILDQ_AS` and the guard's
  `RUN_WITH` so that all of these name the wrapper's commands.
- **A skill.** `./install.sh --skill` links a `buildq` skill into `~/.claude/skills` (Claude Code),
  `~/.codex/skills` (Codex) and `~/.agents/skills`, for whichever of those agents' directories exist.
  An agent loads it only when it is about to build or meets a ticket or a refusal. It covers running
  a command, dropping a long one off, what each wait message means and what to do about it, and
  what not to do (set `-j`, wait on `uptime`, start a build again after a timeout). Notes for one
  machine go in `~/.config/buildq/agents.md`, which `--skill` adds at the skill's end ("On this
  machine"): a wrapper, local rules, where build output goes. Run `./install.sh --skill` again
  after editing it. The skill's source is [skills/buildq/SKILL.md](../skills/buildq/SKILL.md).
- **Their standing instructions** (`AGENTS.md`, `CLAUDE.md`): one rule ("run heavy commands through
  the queue") is enough once the skill has the details.

Two more things help. A pre-command hook enforces it where the harness has one (for example a
Claude Code `PreToolUse` hook on `Bash`; inside a build, `BUILDQ_JOB` is set). And long builds are
submitted, then waited on: `T=$(buildq submit -- cargo test)`, then `buildq wait "$T" --timeout 9m`
as many times as needed, which fits a tool call's time limit. Or run `buildq wait "$T"` as a
background command if the harness reports when one ends.

There is no MCP server, on purpose. A build has to start from the agent's shell, with that shell's
working directory, environment and limits. An MCP tool runs in the harness's process instead, and
would quietly build with the wrong ones. For watching the queue, `buildq status --json` and `buildq
list --json` already give structured output.

## Migrating from a slot script

If heavy jobs run today behind a `flock`-based semaphore, list its lock files under
`[[flock_slots]]`: builds holding one are counted as running builds, measured, with their growth
held. Keep the old script as the fallback (`BUILDQ_FALLBACK="old-slot-script"`), so builds still
have a gate while the daemon is down. Polling can't stop the two from admitting into the same free
memory at the same moment, so don't run both as gates side by side for long.
