"""Shared portfolio-construction layer.

Sits above every sector/sub-sector AQR pipeline (biotech, med_devices, semiconductors,
software-infrastructure, and future sleeves). The sector pipelines decide WHO to own
(security selection / alpha); this layer decides HOW MUCH and WHEN (sizing, timing,
regime, allocation, hedging, exits).

This package is intentionally self-contained and independent of PROD_Scalper_System:
it must not import any sector package nor reach into the PROD tree.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.0.0"