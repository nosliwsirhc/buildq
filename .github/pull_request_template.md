## Summary

<!-- Briefly describe what this PR changes and why. Link any related issue, e.g. Fixes #123. -->

## How it was tested

- [ ] `python3 -m unittest discover -s tests -v`
- [ ] `python3 bin/buildqd --check-config --config config.example.toml`
- [ ] `BUILDQ_TEST_SCOPES=1 python3 -m unittest discover -s tests -v` *(if on systemd and touching runner/service code)*
- [ ] `python3 bench/sim.py check` *(if touching scheduling or admission logic)*
- [ ] `sh -n` on edited shell scripts *(if touching `install.sh`, `bin/buildq-rustc-guard`, or `init/`)*
- [ ] Updated documentation *(if user-facing behavior or config changed)*

## Notes for reviewers

<!-- Optional: trade-offs, follow-ups, or anything you'd like extra eyes on. -->
