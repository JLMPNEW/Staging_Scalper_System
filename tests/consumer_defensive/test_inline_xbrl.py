from __future__ import annotations

from consumer_defensive.core.inline_xbrl import PARSER_VERSION, parse_inline_xbrl


def test_inline_parser_normalizes_context_unit_scale_and_continuation() -> None:
    payload = b'''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance"
        xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
      <xbrli:context id="c1"><xbrli:entity><xbrli:identifier>1</xbrli:identifier>
      </xbrli:entity><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate>
      <xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="segment"><xbrli:entity><xbrli:identifier>1</xbrli:identifier>
      <xbrli:segment><xbrldi:explicitMember dimension="ifrs:SegmentsAxis">
      issuer:NorthMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
      <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <ix:nonFraction name="ifrs-full:Revenue" contextRef="c1" unitRef="usd"
          format="ixt:num-dot-decimal" scale="3" continuedAt="tail">1,234</ix:nonFraction>
      <ix:continuation id="tail"></ix:continuation>
      <ix:nonFraction name="ifrs-full:Assets" contextRef="segment" unitRef="usd"
          format="ixt:fixed-zero">-</ix:nonFraction>
    </html>'''
    result = parse_inline_xbrl(payload)
    assert PARSER_VERSION == "consumer_defensive_inline_xbrl_v1"
    assert len(result.facts) == 2
    revenue = next(fact for fact in result.facts if fact.concept == "Revenue")
    assert revenue.numeric_value == 1_234_000.0
    assert revenue.unit == "USD"
    assert revenue.period_start == "2025-01-01"
    assert revenue.period_end == "2025-12-31"
    assets = next(fact for fact in result.facts if fact.concept == "Assets")
    assert assets.numeric_value == 0.0
    assert "SegmentsAxis" in assets.dimensions_json
    assert "NorthMember" in assets.dimensions_json


def test_inline_parser_skips_nil_and_unsupported_transformations() -> None:
    payload = b'''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance">
      <xbrli:context id="c"><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant>
      </xbrli:period></xbrli:context>
      <xbrli:unit id="u"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
      <ix:nonFraction name="ifrs-full:Ratio" contextRef="c" unitRef="u"
          format="ixt:unsupported">twelve</ix:nonFraction>
      <ix:nonFraction name="ifrs-full:Assets" contextRef="c" unitRef="u"
          xsi:nil="true"></ix:nonFraction>
    </html>'''
    result = parse_inline_xbrl(payload)
    assert result.facts == ()
    assert result.skipped_facts == 2
    assert result.unsupported_transformations == ("unsupported",)
