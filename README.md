# TACT-GUI

**Test-Time Allocation for Collaborative GUI Testing**

TACT-GUI is a research framework for budget-constrained collaborative GUI testing across **Android GUI** and **WebUI**. It supports controlled comparisons of direct execution, self-review, and independent diagnosis, with resource accounting and independent replay verification. The research scope, inclusions/exclusions and platform-stratified analysis are defined in [跨平台研究范畴与范围限定](research/跨平台研究范畴与范围限定-2026-11-25.md).

- A hierarchical SQLite ledger reserves model calls and device operations before execution and records actual use.
- Candidate planners select tasks and execution configurations subject to resource, dependency, and worker constraints.
- Android and Web experiments restore registered checkpoints, run paired continuations, and verify outcomes by replay.
- Development models estimate paired changes in task completion and cost, then use application-group cross-fitting for offline policy comparisons.

The Python package and CLI use the existing name `budgetmas`. Current experiments measure verified completion of functional GUI tasks. Archived Android recordings are development diagnostics; the Web backend currently provides an offline adaptation fixture only. Neither the development fits, offline replays, nor the Web fixture establish an advantage on new applications or cross-platform efficacy.

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

## Web environment adaptation

The Web protocol uses the same `computer_use` action language, hierarchical ledger, paired arms and independent replay verifier as Android. A deterministic in-memory fixture validates this contract with no browser, API key, network or ADB device:

```sh
PYTHONPATH=src python -m budgetmas.mechanism configs/web-mechanism-smoke.json --fixture --out results/web-fixture-smoke
```

The console also registers this profile: choose **Web adaptation smoke** and the `fixture` device. It is an engineering fixture, not a paper benchmark. For real frozen sites, install the optional Web extra and provide a device map whose values are registered Playwright targets:

```sh
python -m pip install -e '.[web]'
python -m playwright install chromium
PYTHONPATH=src python -m budgetmas.mechanism <web-protocol.json> --device-map <device-map.json> --out <fresh-output>
```

Freeze site builds, browser version, viewport, locale, timezone, network rules and initial state before any prospective Web experiment. The [Round 10 experiment notes](docs/ROUND10-FRONTIER-AND-WEB.md) document the native MiniWoB++ smoke test and the Android observed-arm budget replay, with reproduction commands.

The [Round 11 routing study](docs/ROUND11-ALIBABA-ROUTING.md) adds live Alibaba model, role and output-quota contrasts on Android and native MiniWoB++, including a Web task-template holdout. The run reports record pinned model receipts, replay outcomes, list-cost estimates and matched comparisons.

The [Round 12 intent gate](docs/ROUND12-INTENT.md) adds ordered, grounded clicks, clarification for ambiguous instructions, and optional budgeted semantic interpretation. It includes native Web and Android probes and records routing decisions in the console.

The [Round 13 paired study](docs/ROUND13-PAIRED-UPLIFT.md) learns the terminal benefit of diagnostic upgrades on matched Android and Web states, evaluates family-held-out risk, and replays allocation with measured model costs.

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

Start the local console from the repository root. A fresh checkout includes saved mechanism and paired routing runs in `results/` for browsing without a device or API key.

```sh
budgetmas-console
```

Open [TACT-GUI Console](http://127.0.0.1:8765/). The run explorer shows ADB captures taken before each model call, recorded actions, token usage, budget settlement, and independent replay results. The test library browses registered checkpoints by app and protocol. The artifacts view opens or downloads frozen protocols, reports, and available analyses.

To collect data, connect a dedicated `BudgetMASPilot` emulator and configure GUI-Plus as described above. Pass `--manifest results/manifest.json` to use a newly frozen manifest. Select checkpoints, horizon, and verification options in **New run**, then review the call limit and confirm. The console starts the mechanism runner on one emulator and saves its output under `results/console/`. Stopping the runner preserves files already written and keeps any unresolved budget reservations in the ledger.

To cycle through the eight registered Android development checkpoints, start the console and run the campaign supervisor in a separate terminal. Each batch runs one checkpoint across four strategies and independently replays the actions.

```sh
python scripts/run_console_campaign.py --serial emulator-5582 --seconds 3600 \
  --call-limit 480 --seed 20261125 --out results/hourly-campaigns/local-hour
```

[Follow live batches](http://127.0.0.1:8765/?live=1) to see the active strategy and latest frame. The supervisor admits each batch against the campaign call ceiling and writes the run IDs, budgets and outcomes to the campaign directory. At the deadline it stops the active batch and preserves its partial recording. **Stop run** also stops the campaign. The recordings are repeated measurements on development checkpoints.

See the [architecture diagram](architecture/architecture.svg) and [algorithm notes](docs/ALGORITHM.md) for the planning and accounting design. Legacy Android protocols without an explicit `platform` field continue to use the frozen ADB manifest unchanged.
