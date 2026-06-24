#!/usr/bin/env python3
from __future__ import annotations

from macro_http import HttpClient

from .sdmx_csv import SdmxCsvConnector


class ImfSdmxConnector(SdmxCsvConnector):
    source_name = "imf_sdmx"

    def __init__(self, http_client: HttpClient, base_url: str) -> None:
        super().__init__(http_client, base_url=base_url, default_agency=None)
