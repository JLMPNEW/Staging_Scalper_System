"""Shared, cache-first SEC filing parser.

The package is sector-neutral. Sector-specific extraction and acceptance policy
are loaded through adapter entry points supplied by each sector pipeline.
"""

from dedicated_parser.contracts import (
    DOCUMENT_PARSER_RELEASE,
    PARSER_SCHEMA_VERSION,
)

__all__ = ["DOCUMENT_PARSER_RELEASE", "PARSER_SCHEMA_VERSION"]
