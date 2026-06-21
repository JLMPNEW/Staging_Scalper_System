from __future__ import annotations

import logging
import time


def configure_utc_logging(level: int = logging.INFO) -> None:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=level,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )