# DeepSWE evaluation

Runs AutoSE headless on [DeepSWE](https://github.com/datacurve-ai/deep-swe) tasks with
[Pier](https://github.com/datacurve-ai/pier) (Harbor-compatible), using any
OpenAI-compatible server for the model.

| File | Purpose |
|---|---|
| `autose_agent.py` | Pier installed-agent adapter. Installs AutoSE into the task image at build time and runs `autose --events --yes --mode standard` in `/app`. |
| `sample_tasks.py` | Seeded task subset. Sorts task names before shuffling, so the subset depends only on the seed (Pier's own `--sample-seed` shuffles in directory-listing order). |
| `run.sh` | Runs the subset with Pier's local Docker environment. |
| `serve_vllm.sbatch` | Serves the model with vLLM on one L40S of the turing Slurm cluster. |
| `tunnel.sh` | Makes that server reachable from task containers on the Docker host. |
| `report.py` | Solved tasks, wall time and tokens per task from a Pier job directory. |

## Topology

DeepSWE tasks run with `network_mode = "no-network"`. Pier puts the task container on an
internal network whose only exit is a squid proxy that allows the agent's inference host
on ports 80/443. The model runs on a GPU node that the Docker host can reach only through
the cluster login node:

```
task container --HTTP_PROXY--> squid --> 172.17.0.1:80 (socat on docker0)
  --> 127.0.0.1:18000 --ssh -L via turing--> nodeXX:8000 (vLLM, API key)
```

## Running

On turing (weights and venv on the node-local `/scratch/$USER`):

```bash
sbatch serve_vllm.sbatch            # Qwen/Qwen3.8-27B-FP8 as qwen3.8-27b, 128K context
```

On the Docker host (Pier 0.3.1, `deep-swe` cloned to `~/autose-bench/deep-swe`, the
vLLM API key in `~/.autose-vllm-key`):

```bash
tmux new -d -s vllm-tunnel ./tunnel.sh node08
N_CONCURRENT=5 ./run.sh qwen38-27b-seed42
python3 report.py ~/autose-bench/jobs/qwen38-27b-seed42 --md report.md --csv report.csv
```

`run.sh` takes `MODEL`, `N_TASKS` (20), `SEED` (42), `N_CONCURRENT` and `AUTOSE_REF`
(the AutoSE commit installed in the task image).

## Harness choices to report

- Mode is fixed to `standard` (Plan -> Code -> Test); `auto` would add a classifier call
  per task, and only `standard` passes `context_limit` and `request_extras` through.
- AutoSE's wall clock (`AUTOSE_WALL_TIMEOUT_SEC`) is 90 minutes, below the task's 3-hour
  agent timeout, so AutoSE stops on its own and its usage totals are recorded.
- Grading diffs `base_commit..HEAD`. The task prompt asks the agent to commit; if it
  leaves changes uncommitted, the adapter commits them after the run
  (`commit_fallback=true`).
- Sampling follows the Qwen3.8 card's thinking-mode settings
  (temperature 1.0, top_p 0.95, top_k 20).
