import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scoring  # noqa: E402

W = {k: w for k, w in zip([k for k, _, _ in scoring.SCORED], [15, 15, 10, 15, 15, 10, 10, 10])}

# Строки «Пример A/B/C» из исходного шаблона.
EXAMPLE = {
    "A": dict(mcap=8000, fdv=10000, tvl_now=1200, tvl_6m=900, addr_now=400000, addr_6m=300000,
              revenue_1y=150, devs=900, unlock_6m=0.03, investors=5),
    "B": dict(mcap=1500, fdv=6000, tvl_now=600, tvl_6m=700, addr_now=250000, addr_6m=320000,
              revenue_1y=40, devs=300, unlock_6m=0.12, investors=4),
    "C": dict(mcap=3000, fdv=3500, tvl_now=200, tvl_6m=80, addr_now=90000, addr_6m=40000,
              revenue_1y=25, devs=150, unlock_6m=0.01, investors=3),
}


def test_directions_and_bounds():
    r = scoring.score(EXAMPLE, W)
    assert r["A"]["norm"]["devs"] == 1 and r["C"]["norm"]["devs"] == 0          # больше = лучше
    assert r["B"]["norm"]["unlock_6m"] == 0 and r["C"]["norm"]["unlock_6m"] == 1  # меньше = лучше
    assert all(0 <= x["score"] <= 100 for x in r.values())
    assert sorted(x["rank"] for x in r.values()) == [1, 2, 3]


def test_needs_two_values_and_skips_empty():
    rows = {"A": dict(EXAMPLE["A"]), "B": dict(EXAMPLE["B"], devs=None)}
    rows["A"]["investors"] = None
    r = scoring.score(rows, W)
    assert "devs" not in r["A"]["norm"] and "investors" not in r["B"]["norm"]


def test_equal_values_give_half():
    rows = {"A": dict(investors=3), "B": dict(investors=3)}
    r = scoring.score(rows, W)
    assert r["A"]["score"] == r["B"]["score"] == 50
    assert r["A"]["rank"] == r["B"]["rank"] == 1


def test_example_scores_match_template():
    # Эталон посчитан формулами шаблона (pycel) на строках «Пример».
    r = scoring.score(EXAMPLE, W)
    assert [round(r[k]["score"], 3) for k in "ABC"] == [71.662, 33.0, 50.0]
