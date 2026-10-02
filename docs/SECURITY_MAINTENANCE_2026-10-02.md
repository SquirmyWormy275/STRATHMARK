# Dependency security maintenance — 2026-10-02

The October cleanup enabled dependency alerts and grouped security update proposals. Routine version PRs are disabled to keep branch churn bounded; upgrades of numeric dependencies and reviewed pins remain deliberate work.

GitHub reported HTTPX2 response decompression amplification below 2.12.0 ([advisory](https://github.com/advisories/GHSA-8xx6-hgc6-gc2m)) and Starlette request-form denial of service below 1.3.1 ([advisory](https://github.com/advisories/GHSA-82w8-qh3p-5jfq)), alongside earlier HTTPX2/Starlette advisories. Current source API floors are HTTPX2 2.12.0 and Starlette 1.3.1, with FastAPI 0.142.2. The coordinated reviewed STRATHEX test set and STRATHMARK V3 lock use HTTPX2 2.13.1 and Starlette 1.7.0. Minimum-secure and current API sets run in hosted CI.

No numeric model/artifact dependency, frozen OpenAPI identity, release tag, signed receipt, or V3 production eligibility is replaced. Source-bound installed release evidence must be regenerated for the changed source/dependencies on the designated host. The STRATHEX V3 service source pin is historical reviewed code and was deliberately not repointed to current main. Its installed rehearsal must coordinate a reviewed security-maintained service source and consumer pin before production qualification; portable current-main success does not attest that older installation.

Security alerts are resolved by actual dependency changes and verified default-branch scanning, not by dismissing findings. Linux environments and exact built distributions are installed and checked with the updated sets.
