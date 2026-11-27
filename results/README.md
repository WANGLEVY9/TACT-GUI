# Experimental artifacts

These files document the engineering and development experiments. The original Round 8 run and the two technical retries have separate records and accounting. The recorded results support offline inspection and reanalysis of the observed arms. Cross-application efficacy still requires an independent prospective evaluation.

| Run | Included evidence |
| --- | --- |
| Rounds 3–4 | [Local fixture report](round3-local-fixture/report.json) and [Android acceptance summary](round4-assets/acceptance-summary.json) |
| Round 5 | [Frozen app and split manifest](round5-android/manifest-v2.json), [development collection](round5-android/development-v2/collection.json), [CPU model and metadata](round5-android/model-v2/), and [policy comparison](round5-android/comparison-v2/comparison.json) |
| Round 6 | [Paired mechanism recording](round6-mechanism/live-v1/) and [offline budget-prefix replay](round6-design/budget-prefix.json) |
| Round 7 | [Four-app mechanism recording](round7-mechanism/live-development/), [cross-fitted analysis](round7-mechanism/development-fit-audited.json), and [selected trajectory audit](round7-mechanism/trajectory-audit/report.json) |
| Round 8 | [Original crossover recording](round8-crossover/live/), [primary analysis](round8-crossover/primary-interaction.json), and [technical-retry sensitivity analysis](round8-crossover/recovery/sensitivity.json) |
| Console | [Browser validation screenshot](console-validation/tact-console.png) |
| Round 9 | [Cross-platform adaptation record](../docs/ROUND9-CROSS-PLATFORM-ADAPTATION.md) and [offline Web smoke protocol](../configs/web-mechanism-smoke.json) |
| Round 11 | [Android model contrasts](round11-routing/android/routing-summary.json), [Web development contrasts](round11-routing/web/routing-summary.json), and [Web template holdout](round11-routing/web-holdout/routing-summary.json) |
| Round 12 | [Android intent traces](round12-intent/android-validated/), [Web intent traces](round12-intent/web-validated/), and [semantic interpretation probe](round12-intent/web-semantic/) |
| Round 13 | [Android paired trajectories](round13-uplift/android/) and [analysis](round13-uplift/android-analysis.json), [Web paired trajectories](round13-uplift/web/) and [analysis](round13-uplift/web-analysis.json) |

The mechanism recording directories contain the frozen protocol, reports, available pre-call PNGs, raw model replies, and event journals. Final reports include budget limits, settled spend, and pending reservations. The repository excludes raw SQLite files, emulator disk images, APKs, and host logs. Live setup instructions are in the [repository README](../README.md#android-experiments).

The offline Web fixture is generated on demand and is not archived as a research run. Cross-platform scope and remaining gates are in the [research note](../research/跨平台研究范畴与范围限定-2026-11-25.md).

## Offline verification

From the repository root, export the module path and create a fresh output directory. No emulator or model API is required.

```sh
export PYTHONPATH=src
out=$(mktemp -d)
python -m budgetmas.console --port 8765
```

Open [the local console](http://127.0.0.1:8765/) and select a run. For an example of a goal achieved and then lost, select Round 8, **calculator multiply · 2 calls**, and **Direct**. To recompute the recorded offline analyses, stop the console or run these commands in a separate terminal with the same setup.

```sh
python scripts/round5_budget_replay.py results/round5-android/manifest-v2.json \
  results/round5-android/comparison-v2/comparison.json "$out/budget-prefix.json"
python -m budgetmas.paired_learning results/round7-mechanism/live-development/protocol.json \
  results/round7-mechanism/live-development/report.json "$out/development-fit.json"
python -m budgetmas.horizon analyze results/round8-crossover/live/protocol.json \
  results/round8-crossover/live/report.json "$out/primary-interaction.json"
```

Compare the generated JSON with the corresponding archived files in the table. For the Round 5 CPU fit, install the optional `pilot` extra and use `budgetmas android-pilot fit` with the frozen manifest and development collection as inputs.
