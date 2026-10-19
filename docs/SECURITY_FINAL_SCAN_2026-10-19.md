# Security Cloud final-scan follow-up: 3.0.0rc14

This candidate addresses twelve claims from the scan of merged rc13 source. The rc13
publication was held before upload while these findings were reproduced. Published
STRATHEX 7.4.5 keeps its immutable reviewed rc13 source dependency; no existing tag,
package, profile, key, saved competition or numeric model is rewritten.

| Finding boundary | Change and regression evidence |
| --- | --- |
| Health admission | A separate finite pool bounds real health work, including cancellation while a worker remains active; readiness and requested cutoff stay fresh. |
| Shadow body buffering | Authenticated bodies have global/per-bearer capacity before receiving bytes, with separate receipt/status recovery capacity. |
| Shadow evidence/deadlines | Selected evidence has per-athlete, total-row and byte limits before nonce admission. The caller deadline reaches calculation phase checks and the atomic ledger commit. |
| Workspace password work | Concurrent password verification and fixed-window attempts are bounded before expensive work; CSRF and loopback restrictions remain. |
| ZIP central directory | The raw bounded input is checked for actual directory entries and metadata bytes before `ZipFile` creates objects. Forged low counts do not bypass the budget. |
| Workbook coordinates | Bounded ASCII coordinates and row text are checked before conversion or base-26 arithmetic. Valid boundary cells remain accepted. |
| Archive removal snapshot | The signed digest comes from the exact bounded bytes decoded and compared with authority material; atomic path replacement cannot substitute a second generation. Local copy checks are bounded too. |
| Restore manifests | Regular, non-link, bounded files are read once before JSON or signature parsing; FIFO, oversize and symlink regressions fail early. |
| Plaintext archive permissions | Private creation applies from the first byte on POSIX and Windows; no-clobber publication preserves a concurrent winner and shared directories are not tightened. |
| Credential rotation | A private OS lock spans state lookup, secret staging and event append across registry instances/processes. Concurrent overflow retries explicitly; a lost acknowledgment does not delete the committed secret. |
| Public operation conflicts | Forward migration 0019 reserves the complete request identity before preparatory phases and binds it to independent event-witness authentication. Restart, conflicting routes/bodies, concurrent claims and rollback are covered. |
| Tournament opening race | The writer transaction rechecks all latest bound snapshot selections before committing the opening event. A deterministic interleaving cannot combine different selections. |

ZIP inputs allow at most 1,024 members and 1 MiB central-directory metadata; multi-disk
or ZIP64 end-directory forms outside this small-container policy are refused. Ordinary
small workbooks, including ZIP64 local headers, remain supported. Existing expanded
byte, ratio, worksheet, row and column budgets still apply.

Plaintext authority archives require trusted ancestry and private file creation.
Authenticated **encrypted** recovery archives and their receipts remain permitted on
removable/shared destinations; the independent digest/origin rules introduced in rc13
still apply. Existing recovery keys and historical ciphertext remain readable.

Credential changes that fail after staging retain inactive material for exact retry or
explicit incident recovery. Retention grants no credential authority without its event.
Do not automatically delete a pending secret after an ambiguous database acknowledgment.
Concurrent callers receive a retryable credential error rather than replacing another
call's token. A separately provisioned Windows identity/DPAPI store remains required.

Read [authority trust and migration](AUTHORITY_TRUST.md#public-operation-reservations-from-rc14)
before upgrading event authority. Keep the original database and source-bound runtime;
legacy public keys without a full request binding fail closed. The maintained pair for
new profiles is rc14 with STRATHEX 7.4.6 after its release gates pass. Installing it does
not activate or migrate retained rc7/rc12/rc13 profiles. Windows V7 operational eligibility
still requires its independent witness, CNG identity and existing qualification policy.
