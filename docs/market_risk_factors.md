# Market Risk Factor Catalog — Indonesia

**Date:** 2026-10-20
**Status:** In use; past episodes checked against IDX data in phase 5. Companion to `docs/market_risk_plan.md`
**Purpose:** list every kind of shock that can move IHSG, what to watch for each, and which part of the market risk layer catches it

No list can name every future shock. This one aims for full coverage of
*categories*, so that a new event lands somewhere recognisable, and pairs it
with detectors for what it misses (see "Keeping it complete"). Phase 4 turns
the catalog into `data/risk_themes.yaml`. Phases 1–2 take the market signals
and calendar entries from it.

## How each factor is detected

Every factor is wired to one or more detectors from the plan:

| Detector | Plan component | Good for |
|---|---|---|
| **Signal** — a market data series with thresholds | 1–2 | Things that move prices, rates or flows daily |
| **Theme** — keywords (Indonesian and English) over watched sources | 5c | Things that appear in news and official notices |
| **Calendar** — a dated entry with a reminder | 3 | Things scheduled in advance |
| **Filings** — counts aggregated across company filings the pipeline already reads | 2, 4 | Fundamental stress spreading across listed companies |
| **Novelty** — bursts, new pairings and unclassified items | 5d | Everything not yet in this catalog |

Sources marked *(verify)* are candidates whose access, terms and history depth
must be confirmed in the phase 2 and phase 4 spikes. Several useful series
(CDS, EM bond spreads, LME prices) are paid and are listed with free proxies.

## A. Global money and risk appetite

| Factor | Why it matters for Indonesia | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| US interest rates | Higher US yields pull money out of emerging markets, including Indonesian stocks and bonds | Fed decisions, US 10-year yield | Fed calendar; FRED | Calendar, signal |
| Dollar strength | A strong dollar weakens the rupiah and lifts the cost of dollar debt | Trade-weighted dollar index | FRED | Signal |
| Global risk appetite | In panics, investors sell emerging markets first | VIX, US high-yield credit spreads | FRED | Signal |
| Global recession or financial crisis | 2008-type events hit every market at once | US yield curve, bank failures, credit events | FRED; news | Signal, theme |
| Emerging-market contagion | Trouble in one emerging market (e.g. Turkey, Argentina in 2018) spreads to others | Emerging-market currency moves, crisis news | FRED (FX); news | Signal, theme |

## B. China and world trade

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| China slowdown or property crisis | China is a top buyer of Indonesian coal, nickel and palm oil | China PMIs, credit data, property-developer defaults | China statistics bureau releases; news | Theme, calendar |
| Tariffs and trade wars | Tariffs cut export demand and spook foreign investors | Tariff announcements and deadlines | Official US and partner releases; news | Theme, calendar |
| Shipping chokepoints | Disruption at the Red Sea or the Malacca Strait delays trade and lifts freight costs | Shipping-disruption news, freight indices | News | Theme |

## C. Commodities

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Coal | Coal miners carry real index weight and export revenue | Newcastle coal price | World Bank Pink Sheet (monthly) | Signal, theme |
| Nickel | The nickel and battery chain is a growth story and an export pillar | LME nickel price (paid); monthly proxy | World Bank Pink Sheet | Signal, theme |
| Palm oil | A major export; levy and export policy changes move plantation stocks | CPO price, export levy changes | World Bank Pink Sheet; trade ministry notices | Signal, theme |
| Oil | Raises the fuel-subsidy bill and inflation; Indonesia is a net importer | Brent price | FRED | Signal |
| Gold | A safe-haven signal; matters for gold miners | Gold price | World Bank Pink Sheet | Signal |
| Food prices and rice | Food inflation hurts consumers and can force policy responses | Rice and food CPI items | BPS | Signal, theme |

## D. Domestic economy and the rupiah

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Rupiah | A sharp fall raises import costs and dollar-debt burdens and pushes foreigners out | JISDOR rate | Bank Indonesia | Signal |
| FX reserves and current account | Thin buffers make the rupiah vulnerable | Monthly reserves, quarterly balance of payments | Bank Indonesia | Signal, calendar |
| Bank Indonesia policy | Surprise rate moves or heavy intervention signal stress | BI board decisions, BI statements | Bank Indonesia | Calendar, theme |
| Inflation | Erodes spending and can force rate hikes | Monthly CPI | BPS | Signal, calendar |
| Growth and demand | Weak growth hits banks, retail and property | Quarterly GDP, consumer confidence, retail sales, manufacturing PMI | BPS; Bank Indonesia; PMI press releases | Signal, calendar |
| Fiscal health | A widening deficit (Indonesia has a legal 3%-of-GDP cap) or policy surprises hit bonds and stocks | Monthly budget report, government bond yields, bond-auction demand | Ministry of Finance; debt office (DJPPR) | Signal, theme, calendar |
| Tax and subsidy changes | VAT, fuel-price and subsidy changes shift consumer spending and margins | Policy announcements | Ministry of Finance; news | Theme, calendar |
| Sovereign rating | A downgrade or negative outlook can force some funds to sell | Rating actions and outlooks | S&P, Moody's, Fitch, R&I and JCR press pages | Theme, calendar |
| Foreign ownership | Falling foreign holdings of bonds and stocks shows money leaving | Foreign share of government bonds; foreign share of listed equity | DJPPR; KSEI | Signal |

## E. Market structure and index providers

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| MSCI and FTSE decisions | Reclassification or weight changes force passive funds to buy or sell | MSCI quarterly index reviews, annual market classification review and consultations; FTSE Russell country classification | MSCI and FTSE Russell announcement pages | Calendar, theme |
| Index inclusion of large caps | A big stock entering or leaving an index moves billions | Review results, free-float changes | Index-provider announcements | Theme, calendar |
| Exchange and OJK rules | Trading-halt limits, short-selling, special boards and free-float rules change how the market trades | Rule changes, trading halts, suspensions | IDX; OJK | Theme; phase 2b counts |
| Liquidity and flows | Thin trading and persistent foreign selling amplify falls | Turnover, foreign net flow, margin financing, fund redemptions | IDX; OJK statistics | Signal |
| Concentration | A few mega caps (banks and conglomerate groups) dominate IHSG, so one group's trouble moves the index | Top-10 weights, moves in the largest stocks | IDX index data | Signal |
| IPO and lock-up waves | Large lock-up expiries add selling pressure | Lock-up schedules | IDX prospectus data | Calendar |

## F. Politics, policy and governance

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Elections and transitions | Uncertainty before votes; policy shifts after | Election calendar, cabinet reshuffles, finance-minister and central-bank changes | Election commission; news | Calendar, theme |
| Policy shocks | Sudden export bans (coal in January 2022, palm oil in 2022) or nationalisation hit whole sectors | Ministerial announcements | Ministries; news | Theme, novelty |
| State funds and state-owned enterprises | Restructurings, sovereign-fund moves and SOE debt stress move large caps | Announcements, SOE bond events | Ministry of SOEs; news | Theme |
| Central bank independence and fiscal discipline | Doubts about either drive foreign investors out | Laws, statements, rating-agency comments | News | Theme |
| Social unrest | Protests and riots raise the risk premium | Protest and unrest news | News; GDELT | Theme, novelty |
| Fraud and governance scandals | Large frauds (e.g. the Jiwasraya and Asabri cases, around 2019–2021) damage confidence | Enforcement actions, auditor issues | OJK; Attorney General press; news | Theme; filings (auditor changes, going concern) |

## G. Banks and credit

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Bank health | Banks are IHSG's heaviest sector, and were the main drag on 13 October 2026 | Bad loans, loan-to-deposit ratio, liquidity | OJK banking statistics; bank filings already in the pipeline | Signal, filings |
| Credit growth | Slowing credit signals a weakening economy | Monthly credit growth | Bank Indonesia | Signal |
| Corporate and bond defaults | Defaults spread to lenders and investors | Rating downgrades, missed coupons | PEFINDO; KSEI; news | Theme |
| Fintech lending stress | Rising online-lending defaults hit households and some lenders | Default rates for online lending | OJK statistics | Signal, theme |
| Property and construction stress | Big employers with heavy debt; trouble spreads to banks | Developer and contractor filings, defaults | Pipeline filings; news | Filings, theme |

## H. War, geopolitics and security

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Middle East conflict | Moves oil, and through it inflation and the subsidy bill | Conflict news, oil price | News; GDELT; FRED (oil) | Theme, signal |
| Russia–Ukraine and sanctions | Affects energy, grain and fertiliser prices | Conflict and sanction news | News; GDELT | Theme |
| South China Sea, Natuna and Taiwan Strait | Directly involves Indonesian waters and regional trade | Incident and military-activity news | News; GDELT | Theme, novelty |
| Korean peninsula and other flashpoints | Regional risk-off episodes | Escalation news | News; GDELT | Theme |
| Terrorism and domestic security | Short sharp shocks to confidence | Incident news | News | Theme |

## I. Pandemics and disease

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Pandemics | COVID-19 crashed IHSG in March 2020; a new outbreak could repeat it | Outbreak reports, WHO public-health emergency declarations, travel restrictions | WHO Disease Outbreak News; Ministry of Health; news | Theme, novelty, signal (volatility) |
| Regional outbreaks | An outbreak in China or ASEAN reaches Indonesia through travel and trade | Outbreaks naming China, ASEAN or Indonesia | WHO; news | Theme |
| Animal and crop disease | Foot-and-mouth (2022), avian flu or crop blights hit food prices and specific sectors | Animal-health and agriculture notices | Agriculture ministry; news | Theme |

## J. Natural disasters and climate

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Earthquakes and tsunamis | Indonesia sits on the Ring of Fire; a quake near Jakarta or a major industrial area would be market-wide | Large quakes, especially near economic centres | BMKG feeds; USGS earthquake feed | Signal (event feed), theme |
| Volcanoes | Eruptions disrupt flights, tourism and local economies | Volcano alert levels | MAGMA Indonesia (PVMBG) | Theme |
| Floods | Jakarta floods disrupt business and logistics | Flood reports | BMKG; BNPB; news | Theme |
| El Niño and La Niña | Drought or excess rain cuts palm oil and rice output and lifts food prices | ENSO outlook | NOAA; BMKG | Calendar, signal |
| Haze and forest fires | Hit plantation stocks and regional relations | Hotspot reports | BMKG; news | Theme |
| Climate policy | Coal phase-out plans, carbon pricing and EU deforestation rules shift coal and palm oil | Policy announcements, EU regulation dates | Government; EU; news | Theme, calendar |

## K. Technology and operations

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Cyber attacks | Attacks on banks or national systems damage trust | Incident news, national cyber agency notices | BSSN; news | Theme, novelty |
| Exchange or payment outages | Trading or payment disruptions stop the market | IDX notices, payment-system incidents | IDX; Bank Indonesia; news | Theme; phase 2b counts |
| Power blackouts | Large blackouts halt industry | Grid incident news | PLN; news | Theme |

## L. Fundamental stress across listed companies (from the pipeline itself)

The pipeline already extracts these per company. Counted across the market
or a sector, they show stress spreading even when no headline names it.

| Factor | Watch | Detected as |
|---|---|---|
| Going-concern warnings | Share of companies with going-concern events this period | Filings |
| Balance-sheet stress | Share with negative equity or high leverage (xlsx-first figures) | Filings |
| Cash burn | Share with negative operating cash flow | Filings |
| Late filings | Overdue reports rising (filing expectations) | Filings |
| Auditor changes and qualified opinions | Counts per period | Filings |
| Dilution waves | Rights issues and private placements per month | Filings |

## M. Sentiment and valuation

| Factor | Why it matters | Watch | Source (verify) | Detected as |
|---|---|---|---|---|
| Valuation | Expensive markets fall harder on bad news | IHSG price-to-earnings vs its history | IDX statistics | Signal |
| Retail activity | Retail surges and sudden retail exits both precede volatility | Number of investor accounts, retail share of turnover | KSEI; IDX | Signal |
| Public attention | Searches for "IHSG anjlok" spike in panics | Search-trend series | Search-trend service (verify) | Signal, novelty |

## Past episodes: does the catalog cover them?

Each major IHSG drawdown should map to catalog categories. If one doesn't,
the catalog has a gap. Episodes from 2020 on are checked against IDX's own
closes (phase 5, `docs/market_calibration.md`); IDX's endpoints go back only
to January 2020, so earlier ones remain from memory.

| Episode | IHSG (IDX closes) | Main drivers (summary) | Categories |
|---|---|---|---|
| 1997–1998 Asian financial crisis | before IDX data | Currency collapse, banking crisis, political transition | A, D, F, G |
| 2008 global financial crisis | before IDX data | Global credit crisis, commodity crash | A, C |
| 2013 taper tantrum | before IDX data | Fed signalling an end to stimulus, rupiah and current-account worries | A, D |
| 2015 | before IDX data | China slowdown and yuan devaluation, commodity slump | B, C |
| 2018 | before IDX data | Fed hikes, strong dollar, rupiah slide, contagion from Turkey and Argentina | A, D |
| 2020 COVID-19 | 6,325.4 (14 Jan) to 3,937.6 (24 Mar), −37.7%; high regained 11 Jan 2021. Rupiah 13,895 to 16,608 | Pandemic and global lockdowns | I, A |
| 2022–2023 stretches | three falls of 8.8–10.3%, none deeper | Fed hikes, rupiah, commodity swings | A, C, D |
| 2024–2025 sell-off | 7,905.4 (19 Sep 2024) to 5,968.0 (9 Apr 2025), −24.5%; −7.9% on 8 Apr 2025 with a trading halt; high regained 14 Aug 2025 | Domestic fiscal and policy worries, state-fund (Danantara) doubts, then US tariffs | D, F, B, E |
| 2026 decline | 9,134.7 (20 Jan) to 4,996.6 (19 Oct), −45.3%; rupiah 16,981 to 18,050 | MSCI reclassification consultation, oil and Middle East, US inflation, rupiah, foreign selling | E, H, A, D |

Every episode so far maps to existing categories. New episodes are added
here after each one closes (plan component 9); `episode-note` records the
cause and themes on the episode itself.

## Scenario playbooks

For the categories with past crashes. Each says what came first, what
confirmed it, which sectors fell hardest, and the pre-committed rule to
apply. The thesis rules go in `theses.yaml`; see the plan's component 7.

### A. Global money and risk appetite (2020, 2022)

- **First signs:** Fed and global risk-off news (themes `us_rates_fed`, `global_risk_off`); rupiah weakening and foreigners selling for days in a row.
- **Confirmation:** `vol_ratio` stressed with `foreign_sell_streak` at 10 or more sessions.
- **Hit hardest:** property, banks and foreign-owned large caps.
- **Rule:** `market_signal: foreign_sell_streak, operator: gte, value: 10` as a warning; `market_regime: at_least: stressed` as a stop for the watchlist.

### D. Domestic economy and the rupiah (2024–2025, 2026)

- **First signs:** themes `fiscal_deficit`, `bi_independence`, `rupiah_weakness`; JISDOR's 20-session change above 2%.
- **Confirmation:** sectors falling broadly (`sector_breadth` stressed) while IHSG is 10% off its high. In 2024–25 the state was stressed 40 sessions before the April 2025 bottom.
- **Hit hardest:** banks, consumer cyclicals, and companies with dollar costs or debt (the exposure card's reporting-currency flag).
- **Rule:** `exposure_score` of 70 or more as a warning on each watchlist company, plus the market stop rule above.

### E. Market structure and index providers (2026)

- **First signs:** themes `msci_ftse_classification` and `exchange_rules_halts`; burst findings on new phrases such as "standalone market"; the MSCI review dates in the risk calendar.
- **Confirmation:** heavy foreign selling around index dates, and `exchange_notices` rising when the exchange steps in.
- **Hit hardest:** the large caps in the MSCI Indonesia index (`config/index_membership.example.yaml` lists them once you fill it in).
- **Rule:** review index members before each MSCI date; the calendar reminds three days ahead.

### I. Pandemics and disease (2020)

- **First signs:** theme `pandemic_outbreak`; news of outbreaks abroad (WHO, regional health ministries) weeks before Indonesian cases.
- **Confirmation:** a market anomaly (IHSG beyond 2.5 standard deviations) with no calendar date to explain it. In 2020 the bottom came only 18 sessions after the first stressed day.
- **Hit hardest:** transport, tourism, retail and property; healthcare held up.
- **Rule:** with `pandemic_outbreak` active, treat `market_regime: at_least: elevated` as the stop rather than stressed: these falls are fast.

## Keeping it complete

1. **History check:** each closed episode's causes are recorded and mapped here. A cause with no category becomes a new row before the episode is closed.
2. **Novelty feeds the catalog:** promoted findings from the novelty detectors (plan 5d) are added as rows, with keywords and a detector.
3. **Quarterly review:** a review queue item every quarter re-reads this catalog against the quarter's emerging findings and unclassified items, and retires factors that never fired.
4. **Sector sensitivity:** each category lists the sectors it tends to hit (for example, pandemics: transport, tourism, retail and property; oil spikes: airlines, chemicals and subsidised fuel retail). When a theme activates, the exposure card names the watchlist companies in those sectors.
5. **Scenario playbooks:** for the categories with past crashes (A, D, I, E), a short written playbook covers what happened before, which signals confirmed it, which sectors fell most, and the pre-committed thesis rule to apply. They are above, drafted in phase 5 from the backtest; revise them after each episode.
