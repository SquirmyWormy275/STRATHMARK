"""Operator entry point for Prediction Engine V2 training and release evidence.

V2 never trains in the calculation path and never reads the production
database. The pinned workbook pipeline is intentionally two-phase so model
selection and conformal calibration are frozen before the locked test is
opened. With no arguments this command verifies the published report and
packaged artifact without evaluating locked rows again.

Examples::

    python train_model.py
    python train_model.py --prepare
    python train_model.py --open-locked-test

The locked command refuses to run after a final report exists. Removing a
published report to rerun the gate is an explicit governance action, not part
of this tool.
"""

from __future__ import annotations

from scripts.validate_v2 import main as _validation_main


def main(argv: list[str] | None = None) -> int:
    """Delegate to the reproducible V2 validation and artifact workflow."""

    return _validation_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
