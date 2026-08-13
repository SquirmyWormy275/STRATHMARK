# Prediction Engine V2 Locked Validation

Core release gate: **PASSED**

The workbook checksum was verified before evaluation. Selection and calibration were frozen before the locked 2025-07-01 through 2026-02-06 role was scored.

| Metric | V2 core | Strict incumbent |
| --- | ---: | ---: |
| Rows | 128 | 128 |
| MAE (seconds) | 16.1301 | 20.5172 |
| RMSE (seconds) | 33.6904 | 44.4791 |
| Median absolute error (seconds) | 4.7401 | 7.2358 |
| 90% interval coverage | 0.945 | n/a |
| Mean interval width (seconds) | 80.4133 | n/a |

MAE relative improvement: 21.383%.
RMSE relative worsening: -24.256%.

The optional residual learner is inactive because no candidate was frozen before the locked test. Cohort measurements and minimum-sample labels are retained in the JSON report.
