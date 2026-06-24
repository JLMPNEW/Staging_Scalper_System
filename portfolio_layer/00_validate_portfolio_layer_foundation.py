#!/usr/bin/env python3
"""Stage 0 acceptance tests for the portfolio-layer foundation.

Gates (all must pass to proceed to Stage 1):
  1.  portfolio_layer imports (in a clean subprocess) without loading any sector/PROD module.
  1b. AST scan: no source file imports a sibling sector package (catches lazy/future imports).
  2.  Static scan finds no PROD_Scalper_System path/import coupling in the package.
  3.  config.yaml resolves DB, output, cache, and macro-serving paths, all under Staging.
  4.  Layer DB initializes clean with runs and data_quality_issues tables present.
"""
from __future__ import annotations

import ast
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[0]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import load_yaml  # noqa: E402
from portfolio_layer.core.db import connect, init_db, table_exists  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import is_within, resolve_runtime_paths  # noqa: E402


LOGGER = logging.getLogger("validate_portfolio_layer_foundation")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

FORBIDDEN_TOP_LEVEL = ("technology", "biotech_index", "med_devices", "SEC_FORM4_Runner", "ticker_mapping")
PROD_MARKER = "PROD_Scalper_System"
# Real coupling = a filesystem path into PROD or an import of a PROD module, not prose that
# merely names the boundary. Match PROD only when it is a path component, or the legacy marker
# when adjacent to a path separator/import.
PROD_COUPLING_RE = re.compile(
    r"(?i)(?:[A-Za-z]:)?[\\/][^\r\n\"']*[\\/]PROD[\\/]"
    r"|[\\/]PROD_Scalper_System|PROD_Scalper_System[\\/]"
    r"|^\s*(?:from|import)\s+(?:PROD_Scalper_System|PROD)\b"
)
REQUIRED_TABLES = ("runs", "data_quality_issues")


def test_independent_imports() -> tuple[bool, str]:
    """Import portfolio_layer in a clean subprocess; assert no sector/PROD modules loaded."""
    probe = (
        "import sys\n"
        f"sys.path.insert(0, r'{PROJECT_ROOT}')\n"
        "import portfolio_layer\n"
        "import portfolio_layer.core.config, portfolio_layer.core.db, "
        "portfolio_layer.core.paths, portfolio_layer.core.logging_utils\n"
        f"forbidden = {FORBIDDEN_TOP_LEVEL!r}\n"
        "bad = sorted({n.split('.')[0] for n in sys.modules if n.split('.')[0] in forbidden})\n"
        "prod = sorted({n for n, m in sys.modules.items() "
        f"if getattr(m, '__file__', None) and {PROD_MARKER!r} in str(m.__file__)}})\n"
        "print('BAD=' + ','.join(bad))\n"
        "print('PROD=' + ','.join(prod))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    if result.returncode != 0:
        return False, f"probe failed: {result.stderr.strip()}"
    out = result.stdout
    bad = [line[len("BAD="):] for line in out.splitlines() if line.startswith("BAD=")][0]
    prod = [line[len("PROD="):] for line in out.splitlines() if line.startswith("PROD=")][0]
    if bad:
        return False, f"sector packages imported: {bad}"
    if prod:
        return False, f"PROD modules imported: {prod}"
    return True, "no sector or PROD modules imported on package import"


def _forbidden_sector_packages() -> set[str]:
    """Sibling code packages (any dir beside portfolio_layer containing Python) are forbidden imports.

    Derived dynamically from the project tree so a *future* sector package is covered without editing
    this validator; the hard-coded list is kept as a floor.
    """
    forbidden: set[str] = set(FORBIDDEN_TOP_LEVEL)
    for child in PROJECT_ROOT.iterdir():
        if not child.is_dir() or child.name == PACKAGE_ROOT.name or child.name.startswith("."):
            continue
        if (child / "__init__.py").exists() or any(child.glob("*.py")):
            forbidden.add(child.name)
    return forbidden


def test_no_forbidden_sector_imports() -> tuple[bool, str]:
    """AST-scan every package .py for imports of a sibling sector package.

    Catches lazy/conditional imports the runtime probe can miss (e.g. `from technology...` inside a
    function body), and auto-covers sector packages added in the future.
    """
    forbidden = _forbidden_sector_packages()
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: unparseable ({type(exc).__name__})")
            continue
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            for root in roots:
                if root in forbidden:
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{getattr(node, 'lineno', 0)} imports '{root}'"
                    )
    if offenders:
        return False, f"forbidden sector imports: {offenders}"
    return True, f"no imports of {len(forbidden)} sibling sector packages across package source"


def test_no_prod_references() -> tuple[bool, str]:
    """Static scan: no PROD path/import coupling anywhere in the package source/config."""
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*"):
        if path.suffix.lower() not in {".py", ".yaml", ".yml", ".md"}:
            continue
        if path.name == Path(__file__).name:
            continue  # this validator legitimately names the marker in its own regex
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if PROD_COUPLING_RE.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}")
    if offenders:
        return False, f"PROD path/import coupling found in: {offenders}"
    return True, "no PROD_Scalper_System path/import coupling in package"


def test_paths_under_staging() -> tuple[bool, str]:
    """config resolves DB, output, cache, and macro-serving paths, all under Staging."""
    config = load_yaml(DEFAULT_CONFIG)
    paths = resolve_runtime_paths(config, DEFAULT_CONFIG.resolve())
    checks = {
        "database_path": paths.database_path,
        "output_dir": paths.output_dir,
        "cache_dir": paths.cache_dir,
        "macro_serving_db_path": paths.macro_serving_db_path,
    }
    escaped = {name: str(p) for name, p in checks.items() if not is_within(p, PROJECT_ROOT)}
    if escaped:
        return False, f"paths resolve outside Staging: {escaped}"
    return True, "db/output/cache/macro paths all resolve under Staging"


def test_db_initializes() -> tuple[bool, str]:
    """Init the schema into a throwaway DB and confirm required tables exist."""
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "portfolio_layer_test.sqlite"
    conn = connect(db_path)
    try:
        init_db(conn)
        missing = [t for t in REQUIRED_TABLES if not table_exists(conn, t)]
    finally:
        conn.close()  # release WAL handles before cleanup (Windows file lock)
        shutil.rmtree(tmp, ignore_errors=True)
    if missing:
        return False, f"missing required tables: {missing}"
    return True, f"required tables present: {', '.join(REQUIRED_TABLES)}"


def main() -> int:
    configure_utc_logging()
    tests = [
        ("1. independent imports (no sector/PROD)", test_independent_imports),
        ("1b. no forbidden sector imports (AST scan)", test_no_forbidden_sector_imports),
        ("2. no PROD path/import coupling", test_no_prod_references),
        ("3. paths resolve under Staging", test_paths_under_staging),
        ("4. DB initializes with required tables", test_db_initializes),
    ]
    all_pass = True
    for name, fn in tests:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - report any failure as a gate failure
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        all_pass = all_pass and ok
        LOGGER.info("[%s] %s -- %s", "PASS" if ok else "FAIL", name, detail)
    if all_pass:
        LOGGER.info("STAGE 0 ACCEPTANCE: PASS (clear to proceed to Stage 1)")
        return 0
    LOGGER.error("STAGE 0 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
