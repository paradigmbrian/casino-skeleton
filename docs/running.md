# Running the agent layer


```bash
# 1. Dependencies (the agent layer's, kept separate from the project's own requirements.txt)
uv pip install --python .venv/bin/python -r agents/requirements.txt

# 2. Install the post-commit hook. .git/hooks/ is not tracked by git, so this is a setup
#    step on every fresh clone, not something a commit can carry.
ln -sf ../../hooks/post-commit .git/hooks/post-commit

# 3. Credentials
export ANTHROPIC_API_KEY=...

# 4. Run it
.venv/bin/python -m agents.cli up -v
```

While it runs, in another terminal:

```bash
.venv/bin/python -m agents.cli status          # queue depth, recent runs, spend this hour
.venv/bin/python -m agents.cli events          # the event log; --follow to tail it
.venv/bin/python -m agents.cli stop            # drain and exit (Ctrl-C works too)
```

`stop` writes `agents/state/STOP`, which every loop checks. In-flight work finishes; nothing
new is dispatched.

The supervisor logs one aligned line per event — dispatches, run starts, sensor output, and
outcomes with their cost — so a cascade is readable as it happens:

```
21:19:12  ▶ up       supervisor            2 worker slot(s), 4 sensor(s)
21:19:12  ▸ dispatch commit.pushed         → reviewer   depth 0
21:19:12  ● start    reviewer              agent/reviewer-ef2360f8
21:19:25  · sensor   CoverageSensor        1 event(s): coverage.gap
21:21:43  ✗ BUDGET   reviewer              $0.75  17t
21:22:07  ✓ MERGED   reviewer              $0.72  18t  docs/reviews/dd43412.md
```

`-v` widens the agent layer's own logging only. The SDK's transport internals are a separate
opt-in (`--debug-sdk`) because at DEBUG they bury the handful of lines that matter.
`--log-format json` restores machine-readable output, carrying the same structured fields.


---

See also: [architecture.md](architecture.md) for what the layer is doing while it runs.
