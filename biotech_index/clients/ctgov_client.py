from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from biotech_index.core.http_cache import CachedHttpClient
from biotech_index.core.text_norm import normalize_org_name


DEFAULT_CTGV_STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"


def get_nested(obj: Any, path: Iterable[str], default: Any = None) -> Any:
    cur = obj
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def as_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]


def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def raw_hash(obj: Any) -> str:
    return hashlib.sha1(safe_json(obj).encode("utf-8")).hexdigest()


def parse_date_struct(study: dict[str, Any], path: list[str]) -> str:
    value = get_nested(study, path, "")
    if isinstance(value, dict):
        return str(value.get("date") or "").strip()
    return str(value or "").strip()


def parse_enrollment_count(study: dict[str, Any]) -> Optional[int]:
    raw = get_nested(study, ["protocolSection", "designModule", "enrollmentInfo", "count"], None)
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def parse_has_results(study: dict[str, Any]) -> bool:
    raw = study.get("hasResults")
    if isinstance(raw, bool):
        return raw
    if str(raw or "").strip().lower() in {"true", "1", "yes"}:
        return True
    return isinstance(study.get("resultsSection"), dict)


@dataclass(frozen=True)
class ParsedStudy:
    nct_id: str
    brief_title: str
    study_type: str
    phase_text: str
    overall_status: str
    lead_sponsor: str
    last_update_post_date: str
    has_results: bool
    primary_completion_date: str
    enrollment_count: Optional[int]
    raw_hash: str
    raw_json: str


@dataclass(frozen=True)
class ParsedSponsor:
    nct_id: str
    sponsor_name: str
    sponsor_name_norm: str
    sponsor_role: str


def parse_study(study: dict[str, Any]) -> Optional[ParsedStudy]:
    nct_id = str(get_nested(study, ["protocolSection", "identificationModule", "nctId"], "") or "").strip()
    if not nct_id:
        return None
    phases = get_nested(study, ["protocolSection", "designModule", "phases"], [])
    phase_text = ";".join(str(value) for value in as_list(phases) if str(value).strip())
    lead_sponsor = str(
        get_nested(study, ["protocolSection", "sponsorCollaboratorsModule", "leadSponsor", "name"], "") or ""
    ).strip()
    return ParsedStudy(
        nct_id=nct_id,
        brief_title=str(get_nested(study, ["protocolSection", "identificationModule", "briefTitle"], "") or "").strip(),
        study_type=str(get_nested(study, ["protocolSection", "designModule", "studyType"], "") or "").strip(),
        phase_text=phase_text,
        overall_status=str(get_nested(study, ["protocolSection", "statusModule", "overallStatus"], "") or "").strip(),
        lead_sponsor=lead_sponsor,
        last_update_post_date=parse_date_struct(
            study, ["protocolSection", "statusModule", "lastUpdatePostDateStruct"]
        ),
        has_results=parse_has_results(study),
        primary_completion_date=parse_date_struct(
            study, ["protocolSection", "statusModule", "primaryCompletionDateStruct"]
        ),
        enrollment_count=parse_enrollment_count(study),
        raw_hash=raw_hash(study),
        raw_json=safe_json(study),
    )


def parse_sponsors(study: dict[str, Any]) -> list[ParsedSponsor]:
    nct_id = str(get_nested(study, ["protocolSection", "identificationModule", "nctId"], "") or "").strip()
    if not nct_id:
        return []
    sponsors: list[ParsedSponsor] = []
    lead_name = str(
        get_nested(study, ["protocolSection", "sponsorCollaboratorsModule", "leadSponsor", "name"], "") or ""
    ).strip()
    if lead_name:
        sponsors.append(
            ParsedSponsor(
                nct_id=nct_id,
                sponsor_name=lead_name,
                sponsor_name_norm=normalize_org_name(lead_name),
                sponsor_role="lead",
            )
        )
    collaborators = get_nested(study, ["protocolSection", "sponsorCollaboratorsModule", "collaborators"], [])
    for item in as_list(collaborators):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        sponsors.append(
            ParsedSponsor(
                nct_id=nct_id,
                sponsor_name=name,
                sponsor_name_norm=normalize_org_name(name),
                sponsor_role="collaborator",
            )
        )
    return sponsors


class CtgovClient:
    def __init__(
        self,
        *,
        http: CachedHttpClient,
        studies_url: str = DEFAULT_CTGV_STUDIES_URL,
        page_size: int = 100,
        max_pages: int = 25,
        ttl_hours: float = 168.0,
    ) -> None:
        self.http = http
        self.studies_url = studies_url
        self.page_size = max(1, min(int(page_size), 1000))
        self.max_pages = int(max_pages)
        self.ttl_hours = float(ttl_hours)

    def search_studies(
        self,
        *,
        alias: str,
        query_fields: Iterable[str],
        interventional_only: bool = True,
    ) -> dict[str, dict[str, Any]]:
        alias = str(alias or "").strip()
        if not alias:
            return {}
        out: dict[str, dict[str, Any]] = {}
        headers = {"Accept": "application/json"}
        for query_field in query_fields:
            query_field = str(query_field or "").strip()
            if not query_field:
                continue
            page_token: Optional[str] = None
            page_count = 0
            while True:
                params: dict[str, Any] = {query_field: alias, "pageSize": self.page_size}
                if page_token:
                    params["pageToken"] = page_token
                payload = self.http.fetch_json(
                    namespace="ctgov_v2",
                    url=self.studies_url,
                    params=params,
                    headers=headers,
                    ttl_hours=self.ttl_hours,
                )
                studies = payload.get("studies", []) if isinstance(payload, dict) else []
                if not isinstance(studies, list):
                    studies = []
                for study in studies:
                    if not isinstance(study, dict):
                        continue
                    if interventional_only:
                        study_type = str(get_nested(study, ["protocolSection", "designModule", "studyType"], "") or "")
                        if study_type.upper() != "INTERVENTIONAL":
                            continue
                    nct_id = str(get_nested(study, ["protocolSection", "identificationModule", "nctId"], "") or "").strip()
                    if nct_id:
                        out[nct_id] = study
                page_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
                page_count += 1
                if not page_token or (self.max_pages > 0 and page_count >= self.max_pages):
                    break
        return out
