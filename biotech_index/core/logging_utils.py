from __future__ import annotations

import logging
import time


def configure_utc_logging(level: int = logging.INFO) -> None:
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    date_format = "%Y-%m-%dT%H:%M:%SZ"
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=log_format, datefmt=date_format)
    root.setLevel(level)
    for handler in root.handlers:
        if handler.formatter is None:
            handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime
