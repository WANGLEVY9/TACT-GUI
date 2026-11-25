# TACT-GUI

**Test-Time Allocation for Collaborative GUI Testing**

TACT-GUI is a research framework for budget-constrained Android GUI testing. It supports controlled comparisons of direct execution, self-review, and independent diagnosis, with resource accounting and independent replay verification.

- A hierarchical SQLite ledger reserves model calls and device operations before execution and records actual use.
- Candidate planners select tasks and execution configurations subject to resource, dependency, and worker constraints.
- Android experiments restore registered checkpoints, run paired continuations, and verify outcomes by replay.
- Development models estimate paired changes in task completion and cost, then use application-group cross-fitting for offline policy comparisons.

The Python package and CLI use the existing name `budgetmas`. Current experiments measure verified completion of functional GUI tasks. The development fits and offline replays do not establish an advantage on new applications.

## Quick start

Requires Python 3.11 or newer. The core package has no external Python dependencies.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
budgetmas team-fixture --out results/local-fixture
budgetmas audit-budget results/local-fixture/budget.sqlite
```

The local fixture exercises the ledger, role execution, and replay without an emulator or API credentials. The output directory must be new. The optional `pilot` extra installs the CPU CatBoost dependency for the application-group cost baseline.

```sh
python -m pip install -e '.[pilot]'
```

## Android experiments

Use a dedicated Android 34 ARM64 emulator named `BudgetMASPilot` for the supplied mechanism protocols. Install Simple Calculator 5.12.0 from its [official release](https://github.com/SimpleMobileTools/Simple-Calculator/releases/tag/5.12.0). The pilot configuration also identifies the expected Android system applications and entry activities. It records application versions and the emulator build when freezing the manifest.

Some registered cases clear the target app's data. Use a dedicated emulator with no personal data. GUI-Plus requests require `DASHSCOPE_API_KEY` and, if your workspace uses a dedicated endpoint, `DASHSCOPE_BASE_URL`. Use the `gui-plus-2026-02-26` model snapshot. Requests may incur API usage charges.

```sh
adb devices -l
mkdir -p results
budgetmas android-pilot freeze configs/android-pilot.json results/manifest.json --serial emulator-5582
python -m budgetmas.mechanism configs/android-mechanism-smoke.json results/manifest.json \
  --serial emulator-5582 --out results/mechanism-smoke
```

The four-checkpoint smoke protocol allows up to 24 model calls across its three arms. `configs/android-mechanism-development.json` adds four application groups and both two-call and three-call task budgets. After running that protocol, fit and evaluate the development models with:

```sh
python -m budgetmas.paired_learning results/mechanism-development/protocol.json \
  results/mechanism-development/report.json results/development-fit.json
```

Run `python -m budgetmas.horizon --help` for the within-checkpoint budget crossover. Run `python -m budgetmas.trajectory_audit --help` for step-by-step replay of recorded failures. The repository includes [selected experimental artifacts](results/README.md). New runs write separate output, and each live setup acquires its own emulator and app binaries.

## Experiment console

Start the local console from the repository root. A fresh checkout includes three saved mechanism runs in `results/` for browsing without a device or API key.

```sh
budgetmas-console
```

Open [TACT-GUI Console](http://127.0.0.1:8765/). The run explorer shows ADB captures taken before each model call, recorded actions, token usage, budget settlement, and independent replay results. The test library browses registered checkpoints by app and protocol. The artifacts view opens or downloads frozen protocols, reports, and available analyses.

To collect data, connect a dedicated `BudgetMASPilot` emulator and configure GUI-Plus as described above. Pass `--manifest results/manifest.json` to use a newly frozen manifest. Select checkpoints, horizon, and verification options in **New run**, then review the call limit and confirm. The console starts the mechanism runner on one emulator and saves its output under `results/console/`. Stopping the runner preserves files already written and keeps any unresolved budget reservations in the ledger.

See the [architecture diagram](architecture/architecture.svg) and [algorithm notes](docs/ALGORITHM.md) for the planning and accounting design.
