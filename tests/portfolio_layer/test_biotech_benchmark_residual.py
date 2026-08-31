from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "portfolio_layer" / "optimizer" / "09_run_portfolio_optimizer.py"


def load_optimizer_module():
    spec = importlib.util.spec_from_file_location("biotech_residual_optimizer_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def score_rows() -> dict[str, dict[str, str]]:
    shared = {
        "source_pipeline": "biotech",
        "active_sleeve_weight": "0.55",
        "benchmark_residual_weight": "0.45",
        "benchmark_residual_ticker": "XBI",
        "production_policy_id": "policy-v1",
        "production_policy_sha256": "a" * 64,
    }
    return {
        "BIO1": dict(shared),
        "BIO2": dict(shared),
        "TECH": {"source_pipeline": "semiconductors"},
    }


def test_biotech_overlay_transforms_covariance_and_preserves_gross() -> None:
    module = load_optimizer_module()
    universe = ["BIO1", "BIO2", "TECH"]
    covariance = pd.DataFrame(
        np.eye(4),
        index=pd.Index(["BIO1", "BIO2", "TECH", "XBI"]),
        columns=pd.Index(["BIO1", "BIO2", "TECH", "XBI"]),
    )
    mu, transformed_covariance, overlay = module.biotech_benchmark_overlay(
        score_rows(),
        universe,
        covariance,
        np.array([0.10, 0.20, 0.30]),
    )

    assert overlay is not None
    assert mu.tolist() == pytest.approx([0.055, 0.11, 0.30])
    assert transformed_covariance.shape == (3, 3)
    realized = module.realize_biotech_benchmark_weights(
        universe,
        np.array([0.20, 0.10, 0.70]),
        overlay,
    )
    assert realized == {
        "BIO1": pytest.approx(0.11),
        "BIO2": pytest.approx(0.055),
        "TECH": pytest.approx(0.70),
        "XBI": pytest.approx(0.135),
    }
    assert sum(realized.values()) == pytest.approx(1.0)



def test_biotech_overlay_supports_distinct_cohort_reliability_weights() -> None:
    module = load_optimizer_module()
    rows = score_rows()
    rows["BIO1"].update(
        {
            "active_sleeve_weight": "0.60",
            "benchmark_residual_weight": "0.40",
            "industry": "commercial_profitable_quality_or_mature",
        }
    )
    rows["BIO2"].update(
        {
            "active_sleeve_weight": "0.30",
            "benchmark_residual_weight": "0.70",
            "industry": "late_clinical_pivotal_or_registrational",
        }
    )
    covariance = pd.DataFrame(
        np.eye(4),
        index=pd.Index(["BIO1", "BIO2", "TECH", "XBI"]),
        columns=pd.Index(["BIO1", "BIO2", "TECH", "XBI"]),
    )

    mu, _transformed_covariance, overlay = module.biotech_benchmark_overlay(
        rows,
        ["BIO1", "BIO2", "TECH"],
        covariance,
        np.array([0.10, 0.20, 0.30]),
    )

    assert overlay is not None
    assert mu.tolist() == pytest.approx([0.06, 0.06, 0.30])
    realized = module.realize_biotech_benchmark_weights(
        ["BIO1", "BIO2", "TECH"],
        np.array([0.20, 0.10, 0.70]),
        overlay,
    )
    assert realized == {
        "BIO1": pytest.approx(0.12),
        "BIO2": pytest.approx(0.03),
        "TECH": pytest.approx(0.70),

        "XBI": pytest.approx(0.15),
    }
    assert sum(realized.values()) == pytest.approx(1.0)

def test_sensitivity_band_uses_the_same_transformed_mu_as_the_optimizer() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "weight_sensitivity_band"
    ]
    assert len(calls) == 1
    assert isinstance(calls[0].args[0], ast.Name)
    assert calls[0].args[0].id == "mu_optimized"


def test_biotech_overlay_fails_closed_on_inconsistent_contract() -> None:
    module = load_optimizer_module()
    rows = score_rows()
    rows["BIO2"]["benchmark_residual_weight"] = "0.25"
    covariance = pd.DataFrame(
        np.eye(4),
        index=pd.Index(["BIO1", "BIO2", "TECH", "XBI"]),
        columns=pd.Index(["BIO1", "BIO2", "TECH", "XBI"]),
    )

    with pytest.raises(ValueError, match="inconsistent"):
        module.biotech_benchmark_overlay(
            rows,
            ["BIO1", "BIO2", "TECH"],
            covariance,
            np.array([0.10, 0.20, 0.30]),
        )
