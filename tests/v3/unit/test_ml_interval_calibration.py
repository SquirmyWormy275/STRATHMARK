import math

import pytest

from strathmark.v3.assessors.ml import PITCalibrator, build_positive_distribution


def test_calibration_radius_widens_collapsed_quantiles_and_preserves_median():
    calibrator = PITCalibrator(
        "calibration",
        (("0", "0"), ("1", "1")),
        "a" * 64,
        "strathmark-v3-ml-pit-calibrator-v2",
        "0.7",
    )
    reloaded = PITCalibrator.from_dict(calibrator.to_dict())
    distribution = build_positive_distribution([math.log(40)] * 7, reloaded)
    lower, upper = distribution.central_interval("0.05", "0.95")
    assert distribution.median_ms == 40000
    assert lower <= round(40000 * math.exp(-0.7))
    assert upper >= round(40000 * math.exp(0.7))


@pytest.mark.parametrize("radius", ["-1", "21", "NaN", "0.70"])
def test_calibration_radius_rejects_invalid_or_noncanonical_values(radius):
    with pytest.raises(ValueError):
        PITCalibrator(
            "calibration",
            (("0", "0"), ("1", "1")),
            "a" * 64,
            "strathmark-v3-ml-pit-calibrator-v2",
            radius,
        )
