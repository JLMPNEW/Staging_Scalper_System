# Celadon Group lifecycle verification

Status date: 2026-07-31  
Canonical transportation ticker: `CGI`  
Provider price symbol: `CGIP`  
CIK: `0000865941`

## Result

Celadon Group is included as a historical-delisted transportation issuer without substituting
CGI Inc. (`GIB`). The database uses genuine Norgate `CGIP` prices through a reviewed economic and
investability terminal date of 2019-12-09. The provider's sparse post-bankruptcy OTC prints are
not admitted to the investable history and are not represented as a provider terminal date.

## Verified chronology

| Boundary | Date | Contract treatment |
| --- | --- | --- |
| Last pre-suspension NYSE bar | 2018-04-02 | Retained in the continuous Celadon price lineage |
| NYSE trading suspension | 2018-04-03 | Identity/lifecycle evidence; OTC history continues under `CGIP` |
| NYSE removal from listing | 2018-04-30 | Exchange transition, not the economic terminal event |
| Chapter 11 petition | 2019-12-08 | Legal filing date |
| Shutdown of all business operations | 2019-12-09 | Reviewed eligibility and economic-terminal cutoff |
| Final admitted provider bar | 2019-12-09 | Close `0.025`; volume `21,584,794` |
| Terminal common-equity recovery | `0` | Separate terminal-event value used by the survivorship contract |

Primary evidence:

- [NYSE suspension announcement](https://ir.theice.com/press/news-details/2018/NYSE-to-Suspend-Trading-in-Celadon-Group-Inc-CGI-and-Commence-Delisting-Proceedings/default.aspx)
- [SEC-filed NYSE removal notice](https://www.sec.gov/Archives/edgar/data/876661/000087666118000387/ruleprovisionnotice.htm)
- [Celadon Chapter 11 and shutdown announcement](https://www.prnewswire.com/news-releases/celadon-group-inc-and-affiliates-commence-voluntary-chapter-11-cases-300971115.html)
- [SEC filing identifying `CGIP` on OTC Pink](https://www.sec.gov/Archives/edgar/data/865941/000100888619000116/form8k.htm)

## Provider reconciliation

The local Norgate record identifies `CGIP` as `Celadon Group Inc Common`, begins on 1994-01-21,
and is still classified in `US Equities`. Its `last_quoted_date` API value is blank because sparse
OTC trades continue after the operating company failed. A local audit found prints through July
2026. Consequently:

- `last_quoted_date` remains blank in the provider mapping;
- `eligibility_end_date` is 2019-12-09;
- `eligibility_basis` is `reviewed_economic_terminal_event`;
- no later OTC print is used to keep a defunct operating issuer investable;
- no price from `GIB`, CGI Inc., or another security is used.

The terminal value of zero is an economic-recovery event, not a rewrite of the final quoted close
and not the OTC tick value `0.0001`. The exported price row retains the observable 2019-12-09 close
of `0.025`; the separate delisting-event row carries terminal value `0`.

## Loaded and published artifacts

- 6,489 Norgate price rows loaded for `CGI`/`CGIP`, 1994-01-21 through 2019-12-09.
- 159 of 160 transportation mappings are calibration-usable.
- 47 of 48 delisted seeds have usable price histories; RRTS is the sole approved exclusion.
- Historical raw-load validation passes for all 160 ticker rows.
- Portfolio delisted export contains 47 issuers and no missing price ticker.
- Celadon event row: `CGI,2019-12-09,bankrupt,0`.

## Regression gates

The transportation market-foundation tests assert that:

1. canonical `CGI` maps to `CGIP` and never `GIB`;
2. provider `last_quoted_date` stays blank while `eligibility_end_date` is 2019-12-09;
3. the reviewed economic-terminal membership is loaded;
4. the importer includes Celadon and excludes only RRTS;
5. the portfolio event date is 2019-12-09 with terminal value zero.

