'''Canonical validation for external and persisted security tickers.'''

from __future__ import annotations

import re
from typing import Any


_TICKER_RE = re.compile(r'[A-Z0-9]+(?:[.-][A-Z0-9]+)*')
_WINDOWS_DEVICE = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])')


def validate_investable_ticker(
    value: Any, *, context: str = 'security ticker',
) -> str:
    '''Return an uppercase ASCII ticker or fail closed on unsafe syntax.'''

    if not isinstance(value, str):
        raise ValueError(f'{context} must be a string')
    ticker = value.strip().upper()
    if not ticker or len(ticker) > 20 or _TICKER_RE.fullmatch(ticker) is None:
        raise ValueError(f'{context} has unsafe syntax: {value!r}')
    if any(_WINDOWS_DEVICE.fullmatch(part) for part in ticker.split('.')):
        raise ValueError(f'{context} uses a Windows-reserved device name: {value!r}')
    return ticker
