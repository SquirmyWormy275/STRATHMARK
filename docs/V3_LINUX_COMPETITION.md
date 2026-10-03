# Linux V3 competition workflow

STRATHMARK 3.0.0rc6 and STRATHEX 7.4.1 provide the separate `strathmark.v3-linux-competition.v1` local competition profile. A judge deliberately selects V2 or V3 once per competition. Competitor IDs retain a one-to-one local/upstream binding for the entire competition. This profile runs real Formula and trained CatBoost assessors, distribution pooling, capability adjustment, and the V3 complete-field optimizer. It supports approval, separate issue acknowledgment, explicit outcomes, settlement, restart, later-round learning, and competition closure. Championship fields use fixed Mark 3 with signed field receipts.

This is a local operator policy, with persistent owner-private P-256 keys and signed SQLite command history. It does not satisfy or replace the Windows non-exportable CNG production qualification. The V7 service and the earlier numeric preview retain their own contracts and restrictions. The LLM council is unavailable: every Linux field requires deliberate degraded or individual review. A disagreement of five or more marks requires individual review. There is no V2 or single-assessor fallback.

## Install and start

Install exact reviewed wheels in separate Python 3.13 environments. Keep STRATHEX's exact V2 dependency in its environment; install STRATHMARK's `v3-candidate` extra and locked dependencies in the second environment. Use a verified trained bundle compatible with CatBoost 1.2.10 and `cp313`; private model files remain outside Git. Do not substitute a test fixture model for an operator model.

Initialize and inspect the persistent authority explicitly:

```bash
/path/to/v3/bin/python -I -m strathmark.v3.linux_lifecycle init \
  --runtime-root /private/operator/v3-authority \
  --ml-bundle /private/operator/ml-bundle
```

Then launch STRATHEX:

```bash
strathex --workbook /operator/competition.xlsx --data-dir /operator/strathex-data \
  --local-v3-python /path/to/v3/bin/python \
  --local-v3-ml-bundle /private/operator/ml-bundle \
  --local-v3-runtime-root /private/operator/v3-authority \
  --local-v3-backup-dir /independent-disk/recovery
```

The operator launcher requires a backup directory that already exists on a different filesystem from the live workbook. With this option, accepted backend mutations are archived and every member is read back before the response is acknowledged; workbook export preserves and verifies the preceding workbook independently. The standalone runtime API also permits local-only diagnostic use; it does not meet the operator launcher's independent recovery requirement. Never discard the installation private key, database, separate head, frozen histories, STRATHEX saves/authority/command ledger, exact wheels, or trained model. Use `backup` with the same runtime and `--backup-dir` to make another verified recovery archive. When restoring an archive, extract into a new private directory and explicitly set the installation directory to mode 700 and the key/identity/head/database files to mode 600 before opening it. A restored authority keeps the same signing identity; never initialize a replacement key over an existing competition.

## Judge workflow

1. Select V2 or V3 at event/tournament creation. V3 displays `LINUX READY`, records mode `local`, and locks at the first numeric operation. Children inherit the root choice.
2. Use mark-free forecasts for seeding. Generate exact heats and stands before calculating field-relative marks. All handicap fields have a Mark 3 reference and faster expected competitors start later.
3. Open the V3 approval queue. Review degraded fields explicitly; inspect independently optimized Formula/ML counterfactual field marks and individual evidence for red fields. Accept, exclude, or defer deliberately.
4. Confirm **Issue these approved marks now?** separately. Declining retains approved, unissued marks. Resume the queue to issue. Unissued fields cannot print official sheets or record results.
5. Record each issued competitor's raw cutting seconds, `DNF`, `DQ`, `DNS`, `VOID`, or `PENALTY <raw seconds> <penalty seconds>`. Authorize the official judge placings, preserving ties, then confirm the outcomes. Future-dated observations are rejected. Confirmed ID-bound outcomes and exact settlement requests are checkpointed before submission. Settlement binds the original receipt and issued acknowledgment. Nonfinishes and penalty rows do not train raw-time predictors; Excel preserves their explicit status.
6. Complete every field in the event's prior round before advancing. Different events advance independently. All same-round heats retain the frozen epoch, even after another heat settles. Advancing closes that event's completed predecessors; later rounds use valid completed results and earned assessor scores. The local weighting policy uses normalized CRPS with eight equal-prior opportunities, and bounds each assessor's weight to 0.1–0.9.
7. Finish and close the competition. Mark-free seeding rounds close without fabricated issue or settlement records. Saved restart reuses the exact selected source/model/key and committed receipts. Missing inference dependencies can block new forecasts while retained approval, issue, settlement, and exact-command recovery remain available.

Results are immutable revisions. For an official correction, rerun the same STRATHEX launcher with `--correct-v3-results /operator/strathex-data/saves/tournament_state.json`. Choose the settled field, give the official reason, and confirm a complete replacement outcome set. The new signed revision supersedes the latest settlement and reverses its earned scores; original revisions, issued marks, frozen epochs, and saved advancement remain intact. Review advancement separately. Excel retains superseded values outside active raw-time history. Current saved times and judge placings reflect the accepted correction. Never change a committed command ID or edit the runtime database. Source/model upgrades require a new competition or continued use of the original exact environment for a saved competition. They never silently rewrite frozen evidence or issued marks.

## Verification

`tests/v3/integration/test_linux_competition_lifecycle.py` trains a real synthetic CatBoost model and exercises the full lifecycle, same-round freezing, restart, earned weights, capability adjustment, and restored backup. Store tests cover concurrent writers, interrupted acknowledgment, changed-input retry rejection, rollback, corrupted backups, and rejection of Linux keys by Windows production gates. STRATHEX's installed `scripts/smoke_linux_competition.py` exercises the actual consumer and menu functions against installed wheels, with synthetic workbooks and real inference. It verifies separate approval/issue, blocked unissued sheets/results, DNS/penalty outcomes, championship receipts, restart, later-round change, and closure. These checks do not use live workbooks or claim blind model-quality qualification.

## Optional encrypted recovery archives

The Linux CLI accepts `--backup-encryption-policy /private/policy.json` together with `--backup-dir`. The policy has exactly `schema_version` (`strath-recovery-encryption-policy-v1`), `gpg_binary`, `gpg_binary_sha256`, `gpg_home`, and the full uppercase `recipient_fingerprint`. The GPG home must be private and user-owned. Its filesystem must differ from the archive destination. Install GPG and independently retain a protected recovery-key copy before retiring plaintext archives.

Encryption stages plaintext only in private local storage and writes ciphertext directly to the archive drive. Every archive is fully decrypted and checked before success; competition backups also verify every member. Source archives are preserved. `strathmark-v3-encrypt-backup encrypt --policy /private/policy.json --source /private/original.tar.gz --destination /independent/new.tar.gz.gpg` encrypts an existing archive; `verify` accepts the same policy and encrypted `--source`. Recovery requires the private key, not merely the public fingerprint. Safe Python tar extraction does not restore directory permissions: restore the authority directory to mode 700 before reopening; keys/database remain mode 600.
