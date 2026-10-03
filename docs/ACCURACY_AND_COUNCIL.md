# Accuracy and local council evaluation

STRATHMARK 3.0.0rc6 adds Formula tail improvements, full-row regression controls, prospective freezing and encrypted recovery to the separate Linux profile. STRATHEX 7.4.1 keeps explicit V2/V3 selection and retained installation profiles. The rc5 development evidence below remains visibly historical; issued marks, judge placings and Windows CNG eligibility are separate from these accuracy comparisons.

## Chronological development benchmark

The owner authorized an isolated read-only copy of historical workbook results. The original workbook is unchanged. Of 1,311 legacy rows, 1,291 dated completions are admitted; 20 undated rows cannot satisfy a causal cutoff. Whole recorded competition/year groups are disjoint: training through 2022 has 666 rows/88 groups, 2023 tuning has 116/8, 2024 calibration has 231/21, and 2025 onward evaluation has 278/20.

The builder receives training, tuning and calibration roles in a separate process and cannot receive audit rows. Settings are selected by row-weighted mean absolute error, in seconds, of the predicted medians on tuning competitions. Formula priors receive only authenticated training-role rows. Calibration targets set PIT calibration and a finite-sample interval floor; audit targets cannot set interval width. Every prediction receives only observations with earlier timestamps and observation sequences. Native evaluation runs the actual Formula governor, exported/reloaded CatBoost, and Linux distribution pooling.

These are development comparisons. Several candidate aggregates have been examined during development, so this cohort is not an untouched final promotion test. Independent future competitions are still needed. Workbook completions lack authenticated issue receipts and legal field rosters. Raw cutting-time error does not prove handicap fairness or legal placings. No model-quality promotion, production CNG identity, or official competition authority is inferred from this report.

The matched V2 comparison refits the unchanged V2 algorithm on the same pre-2023 population, then supplies date-prior individual history. It deliberately avoids using a newer packaged V2 artifact whose training cutoff would include evaluation targets.

## Measured rc5 development results

All 278 evaluation results are retained. MAE is the average absolute error in raw cutting seconds, not start marks or adjusted placings.

| Predictor | Previous candidate MAE | rc5 candidate MAE | rc5 median absolute error |
| --- | ---: | ---: | ---: |
| Universal ML diagnostic | 29.53 s | 23.71 s | 8.43 s |
| Native calibrated ML | 29.05 s | 23.96 s | 8.42 s |
| Formula | 34.10 s | 31.23 s | 14.08 s |
| Full V3 ensemble | 29.39 s | 25.21 s | 10.09 s |
| Matched chronological V2 | 25.89 s | 25.89 s | 11.04 s |

The ML diagnostic MAE decreases 19.7%; the full ensemble decreases 14.2%. Exact 90% interval coverage is 88.8% for calibrated ML and 91.7% for the ensemble. Previous ML coverage was 21.6%. The first report incorrectly labeled its outer-quantile coverage as 90%; a separate correction recomputes the exact .05/.95 endpoints from the preserved original receipts. No original report or failing candidate was overwritten.

The ensemble's common-quantile mean log pinball loss is 0.1142, and its quantile CRPS improves from 24.43 to 18.72 seconds (23.4%). The uncalibrated universal model's mean log pinball loss worsens from 0.1917 to 0.2033 despite its lower median-prediction MAE; the numeric improvement is not uniform across all scores. Standing-block MAE is 29.66 seconds across 102 rows; underhand is 22.63 across 176. Private output includes every material and diameter slice, including small cohorts and difficult large errors. Formula improved much more on tuning (22.10 to 14.39 seconds) than on later results; that difference is retained as evidence of changing context and limited generalization.

A paired whole-tournament bootstrap (10,000 resamples, seed 20261003) estimates the ensemble change versus the previous V3 at -4.18 seconds, with a 95% interval of -7.43 to -1.46. Versus matched V2, the change is -0.68 seconds with an interval of -5.25 to +3.17: a reliable superiority claim over V2 is not supported. Large tail errors remain. Future development must measure new competitions and improving difficult contexts without deleting them or changing raw-time definitions.

## Predictor changes

ML receives exact-context, same-event and same-material scaled history, history depth, and recent relevant observations. Residual training adds the causally available history anchor back at inference and calibration, preserving raw seconds and unseen-diameter extrapolation. Tuning selects among declared depth/iteration/weight/anchor settings; difficult results are retained. Positive distributions retain the median and widen using calibration-role residuals rather than collapsing nominal intervals.

Formula uses robust training-only context medians and MAD variances. Five or more exact-context results use that context; smaller groups use same-event/material observations scaled under the existing declared diameter exponent. The candidate includes a 225–500 mm grid at 25 mm spacing and observed training sizes. Lineage names the signed training envelope, exact source rows and conversion exponent. Three prior observations and the frozen robust, recency, quality and conversion policy are retained. Unknown materials/events fall back to declared bootstrap priors. This conversion is a declared model policy, not a universal association rule.

An optional `formula_manifest.json` sits alongside the private `ml-bundle` directory. Its digest is part of the immutable competition source identity. Missing, changed or substituted Formula evidence cannot rebind an already selected competition. Keep the original environment and both components to resume a saved competition.

Run a private native comparison with:

```bash
strathmark-v3-benchmark --workbook /private/history-copy.xlsx \
  --bundle /private/candidate/ml-bundle --output /private/new-benchmark \
  --cutoff-at-utc 2026-10-03T00:00:00.000Z
```

The output refuses overwrite and records row receipts privately, workbook/component hashes, exclusions, event/material/diameter slices, raw and calibrated error, exact 90% coverage, common-quantile log pinball loss and quantile CRPS. The V2 training cutoff must precede evaluation targets. Model fitting and calibration boundaries must also precede the selected evaluation years; this command does not grant a blind-audit qualification.

## Local three-family council

`strathmark-v3-local-council` supplies a separate Linux development diagnostic using pinned Qwen3.5 9B, Ministral-3 8B and Gemma3 4B models through native Ollama JSON requests. All three execute locally. The provider transport uses a direct loopback connection, verifies the exact runtime binary/version and model digests before and after requests, and ignores ambient HTTP proxies. It retains provider packets, raw envelopes, response hashes, timings, validation decisions and one bounded correction attempt. Model text never executes as code.

The private profile declares the loopback origin, binary path/hash/version and three family/model/quantization pins. Models must already be installed under its adjacent `models` directory. An app-owned provider can be started after restart without modifying a system service:

```bash
strathmark-v3-local-provider --profile /private/local-council-profile.json start
strathmark-v3-local-council --profile /private/local-council-profile.json --status
strathmark-v3-local-council --profile /private/local-council-profile.json \
  --workbook /private/history-copy.xlsx --competitor-id YOUR_STABLE_ID \
  --event underhand --species YOUR_MATERIAL_CODE --size-mm 300 \
  --cutoff-at-utc 2026-10-03T00:00:00.000Z --output /private/new-receipt.json
strathmark-v3-local-provider --profile /private/local-council-profile.json stop
```

This council is a local development candidate with diagnostic equal-weight pooling. It does not impersonate the V7 contract's two-local/one-cloud promoted council. At least two validated committed members are required for a numeric diagnostic; abstentions and invalid/unavailable responses are counted explicitly. Normal Linux competition forecasts retain Formula/ML and disclose council unavailability for deliberate degraded/individual review. A small pilot or a low error on only available rows cannot justify inserting the council into official numeric authority.

The pilot selects one SHA256-ranked row per whole tournament before any provider call: eight tuning and 20 later evaluation competitions. It retains every selected row, failures, abstentions and latency. Available-subset error must be compared with V2/V3 on those identical rows and accompanied by the missing-response count. Revised protocol prompts are evaluated separately; failed receipts are preserved.

The initial native-provider pilot returned a two-member-or-better council for 11 of the 20 later evaluation rows, with nine unavailable. Its available-subset MAE was 22.33 seconds; on those identical rows the rc5 ensemble was 21.55 and matched V2 was 8.49. This does not support council promotion. A protocol-only prompt correction clarifies empty-history abstention payloads and ordered references; its revised pilot returned only one available council out of 20 evaluation rows (19 unavailable). Both pilots are retained development evidence and neither supports promotion. Review also found placeholder issued marks in the legacy projection; the local provider now receives a mark-free packet, with missing legal-field facts explicitly unavailable. The earlier pilots cannot qualify that corrected projection. The corrected mark-free pilot completed the same 84 native member calls on 28 preselected competition rows. It returned a numeric council for only one of 20 later evaluation rows, with 19 unavailable (two of eight tuning rows were available). Its 7-second error on that single available evaluation row cannot establish accuracy; numeric promotion remains refused. The public release aggregate retains all three pilot outcomes.

The designated independent workbook gate still requires Node v24.19.0 and artifact-tool 2.8.52. This host's 2.8.59 does not satisfy it. Explicit portable tests skip that exact-artifact rebuild; its frozen receipt and Windows production qualification are unchanged.

## rc6 tail audit and regression controls

The read-only workbook audit corrects a published count: there are 1,311 total rows, 1,291 dated admitted completions and 20 excluded undated rows. The role counts and measured accuracy are unchanged. Twenty-one possible repeated row pairs require field/round confirmation; equal recorded cuts can be legitimate separate heats. No rows were deleted or corrected. Original receipts remain immutable.

The worst 28 of 278 evaluation results account for 47.5% of rc5 pooled absolute error. Sparse history is a major limitation: the 20 results without same-event history averaged 86.84 seconds of pooled error. Inspecting an error does not establish that its recorded cut is wrong.

An eight-setting Formula experiment selects prior strength and robust scale on the disjoint 2023 tuning role. Training-derived prior values remain TRAIN-only; CAL and EVAL targets cannot select the setting. The selected context/discipline pseudo-count is one and the minimum robust scale is 0.8; population pseudo-count three and recency 730 days remain fixed. Tuning MAE improves from 14.39 to 13.47 seconds, while tuning p90 worsens from 26.00 to 28.30 seconds. The manifest is `formula:v3-tuned-priors-v1`; bootstrap and earlier trained manifests remain readable.

A native development replay retains all 278 later results. Pooled MAE improves from 25.21 to 23.12 seconds, p90 from 82.14 to 70.12, p99 from 171.90 to 164.17 and maximum error from 211.18 to 200.25. Quantile CRPS improves from 18.72 to 17.25 seconds. Exact 90% coverage rises from 91.73% to 94.60%, with wider mean intervals: 111.64 versus 94.36 seconds. This is a tradeoff, not a free accuracy gain. The universal ML diagnostic remains 23.71 seconds. An eight-trial related-event ML experiment was rejected on TUNE because its best MAE, 13.41 seconds, did not beat the current 12.97; it was never evaluated on EVAL.

`strathmark-v3-accuracy` recomputes metrics from every private row distribution and enforces an identical cohort, fixed MAE/tail/CRPS/coverage limits, and supported event/material/diameter/history slices. Supplied summary metrics cannot override it. It refuses substituted targets, duplicate IDs, dropped rows and nonfinite times. Passing is a development regression result, not numeric promotion.

```bash
strathmark-v3-accuracy workbook --workbook /private/history-copy.xlsx \
  --cutoff-at-utc 2026-10-03T00:00:00.000Z --output /private/new-quality.json
strathmark-v3-accuracy triage --benchmark /private/new-benchmark --output /private/new-triage.json
strathmark-v3-accuracy compare --baseline /private/old-benchmark \
  --benchmark /private/new-benchmark --output /private/new-regression.json
strathmark-v3-accuracy freeze --benchmark /private/new-benchmark --output /private/frozen-protocol.json
```

The prospective protocol freezes exact model, Formula and implementation hashes, previously examined row/group IDs and the policy before future competitions occur. A later benchmark accepts `--audit-after-utc` with that exact freeze timestamp. `prospective --protocol /private/frozen-protocol.json --benchmark /private/future-benchmark --output /private/new-future-check.json` refuses changed components, reused groups and targets at or before freezing. It requires at least 100 rows from ten new competitions. No such future dataset is currently available; that evaluation remains pending collection and cannot be manufactured from the development cohort.

The revised local council uses bounded relevant history, ordered response schemas and explicitly declared conversions of earlier raw cuts into the target context. Original raw milliseconds remain present; unsupported conversions remain null. The bootstrap conversion policy is pinned and is not another assessor's forecast. Raw provider errors and bounded correction attempts remain private evidence. The final conversion pilot validates all 84 responses, with 25 committed Ministral responses and 59 valid abstentions across the three families. Qwen and Gemma abstain on every selected row, so none of the 28 rows reaches the two-member numeric quorum. This improves transport/schema reliability but does not establish numeric availability or accuracy. All failed pilots remain retained; council numeric promotion remains refused.

Benchmark row files are now bound by both byte and canonical digests to a signed local development summary. Freeze/prospective checks verify that binding before accepting rows; copying a different receipt file or editing the summary is refused. The included ephemeral signer establishes file consistency, not independently trusted execution or production eligibility. Candidate evaluation freezes and rechecks both Formula and ML artifacts. Local council validation enforces the exact generated committed fact-code set even if the provider ignores its JSON schema.
