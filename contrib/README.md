# contrib

Files from the maintainer's workstation, kept here so they're versioned. `install.sh` doesn't
install them.

| File | What it is |
| --- | --- |
| `heavy-run` | The wrapper the workstation's coding agents call instead of `buildq` (it sets `BUILDQ_AS`). This copy adds #68: it turns away a script that isn't committed to its project (`bash run.sh`, `bash -c "…"`, `./run.sh`), and `--script "why"` lets one through. It went live on the workstation on 2026-11-20, after #67's verdict, checked with a stub `BUILDQ_BIN` (eight kinds of call refused, eight let through). This copy is the live file: it also carries the workstation's rule that some projects build only at release time (`rb_guard`, which reads a library that isn't kept here and does nothing without its switch file). It falls back to two sibling scripts that aren't kept here: `heavy-run-slots` and `heavy-run-wsl`. |
| `trial54.py` | The numbers for #67's trial of `scale_mem_with_cores = "never"`: builds per day under each setting, outgrown plans, breaches, OOM kills, memory stops, agents' memory waits, held memory and the lowest available memory, read from `~/.local/state/buildq/metrics.db`. |
