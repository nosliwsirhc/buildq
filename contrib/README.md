# contrib

Files from the maintainer's workstation, kept here so they're versioned. `install.sh` doesn't
install them.

| File | What it is |
| --- | --- |
| `heavy-run` | The wrapper the workstation's coding agents call instead of `buildq` (it sets `BUILDQ_AS`). This copy adds #68: it turns away a script that isn't committed to its project (`bash run.sh`, `bash -c "…"`, `./run.sh`), and `--script "why"` lets one through. It was written and reviewed in two rounds and checked with a stub `BUILDQ_BIN`, but it hasn't had a real run yet (the workstation is in a build freeze). It goes live after #67's verdict. It falls back to two sibling scripts that aren't kept here: `heavy-run-slots` and `heavy-run-wsl`. |
| `trial54.py` | The numbers for #67's trial of `scale_mem_with_cores = "never"`: builds per day under each setting, outgrown plans, breaches, OOM kills, memory stops, agents' memory waits, held memory and the lowest available memory, read from `~/.local/state/buildq/metrics.db`. |
