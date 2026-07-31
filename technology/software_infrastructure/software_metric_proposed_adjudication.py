from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


PROPOSAL_REVIEWER = "CODEX_PROPOSAL_PENDING_USER_APPROVAL"
PROPOSAL_NOTICE = (
    "PROPOSAL ONLY. Human approval is required before release or policy sealing."
)


@dataclass(frozen=True)
class ProposalSpec:
    decision: str
    decision_reason: str
    effective_metric: str = ""
    effective_value: str = ""
    effective_unit: str = ""
    effective_period_start: str = ""
    effective_period_end: str = ""
    effective_scope: str = ""
    period_kind: str = ""
    definition_variant: str = ""
    calibration_eligible_flag: int = 0
    review_notes: str = ""
    proposal_confidence: str = "high"
    proposal_review_priority: str = "standard"


def _reject(
    reason: str,
    *,
    notes: str = "",
    confidence: str = "high",
    priority: str = "standard",
) -> ProposalSpec:
    return ProposalSpec(
        decision="REJECTED_POLICY",
        decision_reason=reason,
        review_notes=notes,
        proposal_confidence=confidence,
        proposal_review_priority=priority,
    )


def _value(
    decision: str,
    reason: str,
    *,
    metric: str,
    value: float,
    unit: str,
    period_end: str,
    scope: str,
    period_kind: str,
    variant: str,
    calibration: int,
    period_start: str = "",
    notes: str = "",
    confidence: str = "high",
    priority: str = "standard",
) -> ProposalSpec:
    return ProposalSpec(
        decision=decision,
        decision_reason=reason,
        effective_metric=metric,
        effective_value=str(value),
        effective_unit=unit,
        effective_period_start=period_start,
        effective_period_end=period_end,
        effective_scope=scope,
        period_kind=period_kind,
        definition_variant=variant,
        calibration_eligible_flag=calibration,
        review_notes=notes,
        proposal_confidence=confidence,
        proposal_review_priority=priority,
    )


def _customer(
    *,
    value: float,
    period_end: str,
    variant: str,
    scope: str,
    lower_bound: bool = False,
) -> ProposalSpec:
    qualifier = "lower-bound " if lower_bound else ""
    return _value(
        "ACCEPTED",
        f"actual_{qualifier}customer_count_disclosure",
        metric="customer_count_threshold",
        value=value,
        unit="count",
        period_end=period_end,
        scope=scope,
        period_kind="instant",
        variant=variant,
        calibration=0,
        notes=(
            "Retain as a disclosure/event feature only; customer-count "
            "definitions are not cross-company calibration comparable."
        ),
        priority="review_definition",
    )


PROPOSALS: dict[str, ProposalSpec] = {
    # RPO and cRPO.
    "7e7bd3b3d65933170f4248445c4d11eb016cf935aea293491881e0970dc761a1": _value(
        "CORRECTED",
        "resolved_explicit_millions_scale",
        metric="remaining_performance_obligation",
        value=775_800_000,
        unit="USD",
        period_end="2024-01-31",
        scope="consolidated",
        period_kind="instant",
        variant="total_rpo",
        calibration=1,
    ),
    "f1ddc29e31e7a99de54293ecc3a5097c421c2a0b613d3ed2408bd8494c554d17": _reject(
        "non_gaap_modified_rpo_not_calibration_comparable",
        priority="review_exclusion",
    ),
    "d1f0b97e8da7194ee54718648a52ccf0254b4918171aeff3d1c5ffdb37dd4656": _value(
        "CORRECTED",
        "corrected_contract_liability_to_current_deferred_revenue",
        metric="deferred_revenue_current",
        value=767_244_000,
        unit="USD",
        period_end="2023-12-31",
        scope="consolidated",
        period_kind="instant",
        variant="current_deferred_revenue",
        calibration=1,
        notes=(
            "Informatica reports this balance as current contract "
            "liabilities, not RPO. Exclude it from the RPO and cRPO series."
        ),
        priority="review_correction",
    ),
    "124417c4d43d4365ff36c3a0772b305226a3b9c89eae06d30e15acd0001891ee": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="remaining_performance_obligation",
        value=1_982_024_000,
        unit="USD",
        period_end="2026-04-30",
        scope="consolidated",
        period_kind="instant",
        variant="total_rpo",
        calibration=1,
    ),
    "db9684769c49a378e5fcf75ff0e4c4e8887105a784ff5ce6663f9b36ae2227fc": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="current_remaining_performance_obligation",
        value=1_202_761_000,
        unit="USD",
        period_end="2026-04-30",
        scope="consolidated",
        period_kind="instant",
        variant="current_rpo",
        calibration=1,
    ),
    "228d5bc3280b5df9a44ca0490fdf63b2bbd5ee70c5807f2651b22e3abf35a541": _value(
        "ACCEPTED",
        "actual_reported_current_rpo_not_guidance",
        metric="current_remaining_performance_obligation",
        value=2_499_000_000,
        unit="USD",
        period_end="2026-04-30",
        scope="consolidated",
        period_kind="instant",
        variant="current_12m_rpo",
        calibration=1,
    ),
    "a8353c12e3d39f442fb2f78876bff2ef72eb81b7a4ac10d25eac8b8a15684e26": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="remaining_performance_obligation",
        value=1_545_412_000,
        unit="USD",
        period_end="2025-04-30",
        scope="consolidated",
        period_kind="instant",
        variant="total_rpo",
        calibration=1,
    ),
    "47fefbfe1d55615c14316c7534d74a3d8781430b2289d7502de696b085cfa6d9": _value(
        "CORRECTED",
        "corrected_subscription_subset_to_disclosed_total_rpo",
        metric="remaining_performance_obligation",
        value=790_349_000,
        unit="USD",
        period_end="2025-03-31",
        scope="consolidated",
        period_kind="instant",
        variant="total_rpo",
        calibration=1,
        notes=(
            "Retain as a filing-level reconciled total RPO. The evidence "
            "excerpt lists the RPO components excluding deferred revenue; "
            "the effective value includes deferred revenue and must retain "
            "accession-level provenance."
        ),
        priority="review_correction",
    ),
    # Deferred revenue.
    "1e815ec3a5fb5d8b08a91b4aed9647557933776173c5e3005a58e583af6b01bf": _value(
        "CORRECTED",
        "corrected_total_label_to_current_contract_liability_and_thousands_scale",
        metric="deferred_revenue_current",
        value=819_367_000,
        unit="USD",
        period_end="2024-12-31",
        scope="consolidated",
        period_kind="instant",
        variant="current_deferred_revenue",
        calibration=1,
        notes=(
            "The table line is current contract liabilities. Do not treat "
            "it as total deferred revenue without the noncurrent balance."
        ),
        priority="review_correction",
    ),
    "97b738b03b7bd2a662952199f47d043387c1a08d46ca71289920e5269476dfe0": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="deferred_revenue_current",
        value=34_861_000,
        unit="USD",
        period_end="2026-04-30",
        scope="consolidated",
        period_kind="instant",
        variant="current_deferred_revenue",
        calibration=1,
    ),
    "f2082d1b6f810ff929bac3025822b8b4eb001d7c6c062f7a2b7d18a06b73deab": _reject(
        "cash_flow_change_not_deferred_revenue_balance"
    ),
    "8a761cee72b520fa08974690950a93be31cb53ba82231be80cc7235aa2d34300": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="deferred_revenue_noncurrent",
        value=1_351_960_000,
        unit="USD",
        period_end="2026-04-30",
        scope="consolidated",
        period_kind="instant",
        variant="noncurrent_deferred_revenue",
        calibration=1,
    ),
    "a8aca7d55f6d6340ca97c2ce0a5c4307fb1ea24de032325ff056bfaaf9098a1d": _reject(
        "cash_flow_change_not_deferred_revenue_balance"
    ),
    "ab50ec9dfed40ba67c2095a690d778ce2a3c17b99fcb8661496afd6db6681d05": _value(
        "CORRECTED",
        "corrected_total_label_to_current_balance_and_thousands_scale",
        metric="deferred_revenue_current",
        value=3_370_233_000,
        unit="USD",
        period_end="2026-04-30",
        scope="consolidated",
        period_kind="instant",
        variant="current_deferred_revenue",
        calibration=1,
        priority="review_correction",
    ),
    # ARR.
    "0d48912b5e50cb844ae6dac6e82432303a2b3cb4c5340d16663ce499614ef270": _reject(
        "year_over_year_impact_not_arr_level"
    ),
    "56b947f2f510c67722766367201f14195f64f4a72523068822b38b5c0df8ea38": _reject(
        "year_over_year_impact_not_arr_level"
    ),
    "a7dc90989dc8b17a8daf1af705022bc49b7f0e09eb84b3eca3817c0fb949e14a": _reject(
        "forward_arr_guidance_not_actual"
    ),
    # NRR.
    "941fad8a6fd10e60eda963a3d59d2ce55d5b1760e01dd9000f9c67674360e005": _value(
        "ACCEPTED",
        "actual_censored_lower_bound_nrr_disclosure",
        metric="net_revenue_retention",
        value=1.2,
        unit="ratio",
        period_end="2022-01-31",
        scope="consolidated",
        period_kind="instant",
        variant="dollar_based_net_retention_lower_bound",
        calibration=0,
        notes="The filing reports 'above 120%'; retain only as a censored event.",
        priority="review_definition",
    ),
    "e2daa7f7c358233fd2ed689f0c5301c708fa848673e5fd3e60c5a22492a03b56": _value(
        "ACCEPTED",
        "actual_dollar_based_net_retention",
        metric="net_revenue_retention",
        value=1.10,
        unit="ratio",
        period_end="2025-01-31",
        scope="consolidated",
        period_kind="instant",
        variant="dollar_based_net_retention",
        calibration=1,
    ),
    "cb8101102ed86c61d849e3cf8849d2f94aaa7b408a548c5596bb7c9a9c4fb3fc": _value(
        "ACCEPTED",
        "actual_dollar_based_net_retention",
        metric="net_revenue_retention",
        value=1.19,
        unit="ratio",
        period_end="2024-01-31",
        scope="consolidated",
        period_kind="instant",
        variant="dollar_based_net_retention",
        calibration=1,
    ),
    "8a13e533161454c524fd92ab7044da3b0a0d3cb77b8494802b6a892f2db6df52": _value(
        "ACCEPTED",
        "actual_dollar_based_net_retention",
        metric="net_revenue_retention",
        value=1.23,
        unit="ratio",
        period_end="2023-01-31",
        scope="consolidated",
        period_kind="instant",
        variant="dollar_based_net_retention",
        calibration=1,
    ),
    # Billings.
    "4b858d51c5c6f3b551d24f0044c0108b340b3dafcc9dd8c3dfcc598aa1b99593": _value(
        "CORRECTED",
        "resolved_scale_nonstandard_customer_deposit_reclassification_billings",
        metric="disclosed_billings",
        value=552_799_000,
        unit="USD",
        period_start="2022-02-01",
        period_end="2023-01-31",
        scope="consolidated",
        period_kind="annual",
        variant=(
            "deferred_revenue_billings_including_customer_deposit_"
            "reclassification"
        ),
        calibration=0,
        notes=(
            "Retain for disclosure history only. This issuer-specific "
            "reconciliation is not comparable to standard reported "
            "billings across companies."
        ),
        priority="review_definition",
    ),
    "770f88c604facc0c4d9d4111f27206b5f7894c01dbbbf51ef3ccf7d6d175471e": _reject(
        "semantic_duplicate_same_accession_and_period"
    ),
    "3d2a22c1cdd30a5897ebb273660a001e02012f55385e2e794750ddb9a017dcf6": _value(
        "ACCEPTED",
        "actual_billings_excluding_customer_deposits",
        metric="disclosed_billings",
        value=364_365_000,
        unit="USD",
        period_start="2021-02-01",
        period_end="2022-01-31",
        scope="subset",
        period_kind="annual",
        variant="reported_billings_excluding_customer_deposits",
        calibration=0,
        notes="Retain for disclosure history; exclude from comparable calibration.",
        priority="review_definition",
    ),
    "fb86ade2702cb0229fbaab0d4244956b9a9934b9ca321c15ae0ff31f75af1cb9": _value(
        "ACCEPTED",
        "actual_reported_quarterly_billings",
        metric="disclosed_billings",
        value=102_700_000,
        unit="USD",
        period_start="2026-02-01",
        period_end="2026-04-30",
        scope="consolidated",
        period_kind="quarterly",
        variant="reported_billings",
        calibration=1,
    ),
    "6e563e98fce672aea8c6128c2bfd5eea7868730a9c9998fcb50eb9f8b4bd5e04": _value(
        "CORRECTED",
        "resolved_explicit_millions_scale",
        metric="disclosed_billings",
        value=2_085_300_000,
        unit="USD",
        period_start="2026-01-01",
        period_end="2026-03-31",
        scope="consolidated",
        period_kind="quarterly",
        variant="reported_billings",
        calibration=1,
    ),
    "bffce80d89ac4dca21eea5f4021f4e1c30bc3428e6c77d0115b59a942bda57ad": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="disclosed_billings",
        value=485_090_000,
        unit="USD",
        period_start="2024-02-01",
        period_end="2025-01-31",
        scope="consolidated",
        period_kind="annual",
        variant="reported_billings",
        calibration=1,
    ),
    "f01900bfdcb6e0236e121a9bb5bedf71342878ad61d971166c258a9b4c6b73b7": _reject(
        "semantic_duplicate_same_accession_and_period"
    ),
    "1abbe348fdd7100002f6c726b96a5d3986b2d332e1c0c119ef8fcfd30ddbf89a": _reject(
        "forward_quarterly_billings_guidance_not_actual"
    ),
    "69d484e528eded01fd662380258a83877a994e3891ec88fff4a9b49a71b28364": _reject(
        "forward_annual_billings_guidance_not_actual"
    ),
    "535b108eb91fbfcfe7652468acf5c9bc332e6b7546b6bc07768122b589266813": _reject(
        "forward_segment_billings_guidance_not_actual"
    ),
    "49ffd02ebbc84feb4b662eca93ca074b555b99c8f6716c24301dd74f2816a4f1": _reject(
        "forward_annual_billings_guidance_not_actual"
    ),
    "e7d8865c93a2c3d604649e5ae61805726ca99b2bdbbec6403bcf9ceb37bd514d": _reject(
        "forward_quarterly_billings_guidance_not_actual"
    ),
    "3ab51211730ef84599f9fc8ff460a901986d5e6b86304803fab590eadf1113c2": _reject(
        "accounts_payable_change_misidentified_as_billings"
    ),
    "3f11691fc4e0a365f0b26ee1e942ab048390f57be78c31d5da592cbd2a3799e9": _reject(
        "semantic_duplicate_same_accession_and_period"
    ),
    "e59b6ffbea6361dd69d77206ddfb6877bdb6a342861c97e0c6980d18e9b0ccd7": _reject(
        "semantic_duplicate_same_accession_and_period"
    ),
    "ed17060cc2133ad0d71d43069b98f64f11cc5254f99a5286b55ae6bb359755b1": _reject(
        "semantic_duplicate_same_accession_and_period"
    ),
    "d1ec6d7eb6e688f7a37507cc5634a674995951dbc07f05f962fa801e4446f7f7": _reject(
        "stock_compensation_value_misidentified_as_billings"
    ),
    "0bbb2ad4148f623bf799550a4cbc5de7a3a369a90d24685411750f5f855acfdf": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="disclosed_billings",
        value=765_263_000,
        unit="USD",
        period_start="2019-02-01",
        period_end="2020-01-31",
        scope="consolidated",
        period_kind="annual",
        variant="reported_billings",
        calibration=1,
        notes="Company labels the actual metric as calculated billings.",
    ),
    # Subscription revenue.
    "e43801edf8409d0887af29b1229ee94ece8dcd6417d65a4859912604abc66b7b": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="subscription_revenue",
        value=297_449_000,
        unit="USD",
        period_start="2024-10-01",
        period_end="2024-12-31",
        scope="consolidated",
        period_kind="quarterly",
        variant="total_subscription_revenue",
        calibration=1,
    ),
    "bbdb42f44b1d983bb1123099c9eead439356c0a4422d825aa62ac59322481327": _reject(
        "subscription_cost_of_revenue_not_subscription_revenue"
    ),
    "f55b48dc65c3cc8e2801fa3c0f5dce38e725370de649df63f8ab97685b47888b": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale_and_quarterly_period",
        metric="subscription_revenue",
        value=150_004_000,
        unit="USD",
        period_start="2023-11-01",
        period_end="2024-01-31",
        scope="consolidated",
        period_kind="quarterly",
        variant="total_subscription_revenue",
        calibration=1,
    ),
    "bd916469390de7f1e8f75cdccf84c75664c99fd350e8ed09b07e59ff5e93c5a4": _value(
        "CORRECTED",
        "resolved_explicit_thousands_scale",
        metric="subscription_revenue",
        value=1_320_853_000,
        unit="USD",
        period_start="2026-02-01",
        period_end="2026-04-30",
        scope="consolidated",
        period_kind="quarterly",
        variant="total_subscription_revenue",
        calibration=1,
    ),
    # Customer counts. All remain event/disclosure features with zero
    # calibration eligibility because definitions differ materially.
    "f646f2d6f934ea0d6c331aa6976cfb5e40c073bb5a82e040ff98b04a64207593": _customer(
        value=38,
        period_end="2019-12-31",
        variant="arr_over_1m_customer_count",
        scope="subset",
    ),
    "0b0e77941df105a1d98845bd9d08c26deca406bac7278b4b0cf2329ab10d71dd": _customer(
        value=9_700,
        period_end="2024-12-31",
        variant="total_customer_count_lower_bound",
        scope="consolidated",
        lower_bound=True,
    ),
    "4be2f4ce39525dbac0cb6a88838e2c00b7e557de7d2ffc5eba175459a8a873ed": _customer(
        value=5_100,
        period_end="2024-12-31",
        variant="total_customer_count_lower_bound",
        scope="consolidated",
        lower_bound=True,
    ),
    "1786142aee3e6ad7345fbe797aa1f043bdc80b2d5959f8ab3320c03ee40ccf69": _customer(
        value=240,
        period_end="2026-04-30",
        variant="acv_over_1m_customer_count_lower_bound",
        scope="subset",
        lower_bound=True,
    ),
    "81a05386a56fcf5812497d9c3ca6220374f12a3085acbd9b7296703a3efd91cf": _customer(
        value=1_720,
        period_end="2026-04-30",
        variant="annual_spend_over_100k_customer_count_lower_bound",
        scope="subset",
        lower_bound=True,
    ),
    "db2010eee060e5ecabf329a193f31a05cd3d94834fb0a75b04f4432dd326013a": _customer(
        value=2_946,
        period_end="2026-04-30",
        variant="subscription_arr_100k_customer_count",
        scope="subset",
    ),
    "9583016e5ad1ef7c5e75e85ebebe22926f31bf0fc8812c026ce8b93b26db8270": _customer(
        value=1_519,
        period_end="2026-04-30",
        variant="arr_over_100k_customer_count",
        scope="subset",
    ),
    "aa87201e9f9b7a1e98d3062da5f790a00dc8479576e9464ba1f11f4f0729a630": _customer(
        value=779,
        period_end="2026-04-30",
        variant="ttm_product_revenue_over_1m_customer_count",
        scope="subset",
    ),
    "be7887fc200dd20bab178471fd6f7065f8f979f5b435ac03733f04d982388445": _customer(
        value=67_700,
        period_end="2026-04-30",
        variant="total_customer_count_lower_bound",
        scope="consolidated",
        lower_bound=True,
    ),
    "775d10043ca1d0df322579195ec6693adde113aec40431afb795b48d41fe9d19": _customer(
        value=1_702,
        period_end="2026-04-30",
        variant="arr_100k_customer_count",
        scope="subset",
    ),
    "57f9988fade3603dae9fa2441bf47ba4f8c8c6d64af5349651430709789c5c4c": _customer(
        value=80,
        period_end="2026-03-31",
        variant="arr_1m_customer_count",
        scope="subset",
    ),
    "b70ebee8cefe97602f683528f3e032ccd1f164ddc7a19133baf23c18a5af314e": _customer(
        value=11_500,
        period_end="2026-03-31",
        variant="total_customer_count_lower_bound",
        scope="consolidated",
        lower_bound=True,
    ),
    "d1b2ee178bd8dbb3a93c95bb3bbefc47d0b4e72aefe570226cf8ccaaa752b269": _customer(
        value=350_000,
        period_end="2026-03-31",
        variant="total_customer_count_lower_bound",
        scope="consolidated",
        lower_bound=True,
    ),
    "796a93a2ca24392b14a6df0c2bb5288a7fece50f8e19ee99226adcd7875fef55": _customer(
        value=3_000,
        period_end="2026-03-31",
        variant="total_customer_count_lower_bound",
        scope="consolidated",
        lower_bound=True,
    ),
    "60efd0a95a607ea4812060896eb32f23e52d6d1b5afaf4e241f087b0670d9819": _customer(
        value=4_733,
        period_end="2026-01-31",
        variant="total_customer_count",
        scope="consolidated",
    ),
    "135f4c214df74e3256c7b1589d540c582bde2b451e3e965c2d424641159b2ac1": _customer(
        value=2_565,
        period_end="2026-01-31",
        variant="arr_100k_customer_count",
        scope="subset",
    ),
    "a91d160dd62f105248415d0ae74310d51b93b6fc45a2be8789ea423ae64650de": _customer(
        value=733,
        period_end="2026-01-31",
        variant="ttm_product_revenue_over_1m_customer_count",
        scope="subset",
    ),
    "d19df9e07b0c9cc5c472e0cbbbd091cd787a8247f03ff6e501013370b4f85b68": _customer(
        value=2_805,
        period_end="2026-01-31",
        variant="subscription_arr_100k_customer_count",
        scope="subset",
    ),
    "3c7f6c1aeb8c62599b6d53caf11453ab50f95993b7ad2fa41d37e66bdfa4cc98": _customer(
        value=1_667,
        period_end="2026-01-31",
        variant="arr_100k_customer_count",
        scope="subset",
    ),
}


def build_proposed_rows(
    rows: Iterable[dict[str, Any]],
    *,
    proposed_at_utc: str,
) -> list[dict[str, Any]]:
    source_rows = [dict(row) for row in rows]
    source_keys = {
        str(row.get("source_evidence_key") or "") for row in source_rows
    }
    missing = sorted(source_keys - set(PROPOSALS))
    extra = sorted(set(PROPOSALS) - source_keys)
    if missing or extra:
        raise ValueError(
            f"Proposal registry mismatch: missing={missing[:5]}, extra={extra[:5]}"
        )
    output: list[dict[str, Any]] = []
    for source in source_rows:
        key = str(source["source_evidence_key"])
        spec = PROPOSALS[key]
        row = dict(source)
        row.update(
            {
                "reviewer": PROPOSAL_REVIEWER,
                "reviewed_at_utc": proposed_at_utc,
                "decision": spec.decision,
                "decision_reason": spec.decision_reason,
                "effective_metric": spec.effective_metric,
                "effective_value": spec.effective_value,
                "effective_unit": spec.effective_unit,
                "effective_period_start": spec.effective_period_start,
                "effective_period_end": spec.effective_period_end,
                "effective_scope": spec.effective_scope,
                "period_kind": spec.period_kind,
                "definition_variant": spec.definition_variant,
                "calibration_eligible_flag": str(
                    spec.calibration_eligible_flag
                ),
                "review_notes": " ".join(
                    value
                    for value in (PROPOSAL_NOTICE, spec.review_notes)
                    if value
                ),
                "proposal_status": "PENDING_HUMAN_APPROVAL",
                "proposal_confidence": spec.proposal_confidence,
                "proposal_review_priority": spec.proposal_review_priority,
            }
        )
        output.append(row)
    return output
