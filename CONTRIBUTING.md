# Contributing to buildq

Thanks for your interest in contributing to **buildq**! Bug reports, documentation improvements,
testing on different Linux distributions and init systems, new build tool integrations, and code
fixes are all welcome.

## Ways to contribute

- **Report a bug or suggest a feature** using the [issue templates](https://github.com/nosliwsirhc/buildq/issues/new/choose).
- **Ask questions or share distro setups** in [GitHub Discussions](https://github.com/nosliwsirhc/buildq/discussions).
- **Improve the docs** — if something in [README.md](README.md), [init/README.md](init/README.md),
  or `docs/` was confusing or hard to follow, a pull request to make it clearer is always welcome.
- **Test on your Linux setup** — especially non-systemd distros (Alpine, Void, Gentoo, Artix,
  Devuan, Chimera, Slackware), WSL2, containers, or unusual build tools.
- **Add support for a build tool or compiler** — most tools only need a single `ToolSpec` entry in
  [src/buildq/tools.py](src/buildq/tools.py) (plus optional shim routing in
  [bin/buildq-shim](bin/buildq-shim) and [install.sh](install.sh)).
- **Pick up an open issue** — look for issues labeled
  [`good first issue`](https://github.com/nosliwsirhc/buildq/labels/good%20first%20issue) or
  [`help wanted`](https://github.com/nosliwsirhc/buildq/labels/help%20wanted).

## Submitting a pull request

For large changes or new scheduling behavior, open an issue or discussion first so we can align on
the approach before you spend time writing code.

1. Fork the repository on GitHub and clone your fork.
2. Create a branch from `main`:
   ```sh
   git checkout -b fix/my-change
   ```
3. Make your changes and run the checks below.
4. Keep pull requests focused on one change, with clear commit messages explaining *why*.
5. Push your branch and open a pull request against `main`.

> **First-time contributors:** GitHub Actions waits for a maintainer to approve CI runs on a
> first-time contributor's fork PR. That's normal — a maintainer will approve it when reviewing.

## Running tests locally

You need Linux and Python 3.11 or newer. No virtual environment or third-party packages are needed.

```sh
# Unit and end-to-end tests (runs a private daemon on a temp socket; never touches your installed daemon)
python3 -m unittest discover -s tests -v

# Also test real systemd user services, cgroup limits, freezing, and make's jobserver (systemd hosts only)
BUILDQ_TEST_SCOPES=1 python3 -m unittest discover -s tests -v

# Check replay simulator determinism (required if you touch scheduling or admission logic)
python3 bench/sim.py check

# Verify the example configuration loads cleanly
python3 bin/buildqd --check-config --config config.example.toml

# Check shell script syntax (when editing install.sh, bin/buildq-rustc-guard, or init/ scripts)
sh -n install.sh bin/buildq-rustc-guard init/cgroup-setup.sh init/runit/buildqd/run init/s6/buildqd/run init/sysvinit/buildqd
```

If you change scheduling behavior, you can also compare your branch against `main` using the replay
simulator — see [docs/benchmark.md](docs/benchmark.md).

## Project conventions

- **Zero third-party Python dependencies:** everything in `src/buildq/` and `bin/` uses the
  Python 3.11+ standard library only.
- **Pure POSIX `/bin/sh` for shell scripts:** `install.sh`, `bin/buildq-rustc-guard`, and the
  scripts under `init/` avoid bashisms so minimal distros like Alpine work without installing
  `bash`.
- **Plain-English docs:** keep [README.md](README.md), [init/README.md](init/README.md), and
  user-facing guides approachable for everyday developers; put deep kernel, cgroup, and protocol
  internals in [docs/design.md](docs/design.md).
- **License:** by contributing, you agree that your contributions will be licensed under the
  project's [MIT License](LICENSE).

## Code of Conduct & Security

- Please be respectful and constructive — see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- To report a security issue privately, see [SECURITY.md](SECURITY.md) rather than opening a public
  issue.
