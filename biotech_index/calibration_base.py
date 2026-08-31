"""Importable facade for the numeric calibration script.

Windows ``spawn`` workers must be able to import the module that owns submitted
callables and dataclasses. The production calibration implementation remains in
``scripts/28_calibrate_biotech_opportunity.py``; executing it in this module's
namespace gives those objects a stable, importable module identity.
"""

from __future__ import annotations

from pathlib import Path


_SOURCE_PATH = Path(__file__).resolve().parent / "scripts" / "28_calibrate_biotech_opportunity.py"
__file__ = str(_SOURCE_PATH)
exec(compile(_SOURCE_PATH.read_bytes(), str(_SOURCE_PATH), "exec"), globals(), globals())  # noqa: S102
