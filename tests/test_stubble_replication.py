import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "historical"))

import stubble_replication as sr  # noqa: E402


def test_one_sided_p_converts_both_directions():
    assert sr.one_sided_p(0.04, 0.3) == 0.02 and sr.one_sided_p(0.04, -0.3) == 0.98


def test_stouffer_matches_hand_calculation_and_is_monotone():
    z, p = sr.stouffer([0.05, 0.05])
    assert abs(z - 2 * 1.6449 / np.sqrt(2)) < 1e-3 and abs(p - 0.0100) < 5e-4
    assert sr.stouffer([0.5, 0.5, 0.5])[1] == 0.5 or abs(sr.stouffer([0.5, 0.5, 0.5])[1] - 0.5) < 1e-9
    assert sr.stouffer([0.01, 0.2])[1] < sr.stouffer([0.2, 0.2])[1]


def test_decision_rule_needs_both_combined_p_and_positive_median_and_ignores_untestable_seasons():
    rows = pd.DataFrame({"testable": [True, True, True, False], "rho": [0.3, 0.2, 0.25, -0.9], "p_one_sided": [0.03, 0.04, 0.02, 0.99]})
    d = sr.decide(rows)
    assert d["holds"] and d["testable_seasons"] == 3 and d["seasons_positive"] == 3
    neg = rows.assign(rho=[-0.3, -0.2, -0.25, 0.9], p_one_sided=[0.97, 0.96, 0.98, 0.01])
    assert not sr.decide(neg)["holds"]
    assert not sr.decide(rows.assign(testable=False))["holds"]
