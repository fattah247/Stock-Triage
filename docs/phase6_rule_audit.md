# Phase 6 Rule Audit

Ruleset version: `phase6-v2`

Current baseline rules:
- `profit_drop`: triggers when latest extracted `net_profit < 0`
- `cash_flow_deterioration`: triggers when latest extracted `operating_cash_flow < 0`
- `debt_surge`: triggers when `total_liabilities / equity > 0.75`, or when equity is zero or negative while liabilities are outstanding (`phase6-v2`; under `phase6-v1` a negative-equity issuer produced a negative ratio and never triggered)
- `auditor_change`: triggers from extracted `auditor_change` events
- `management_change`: triggers from extracted `governance_appointment` events
- `funding_event`: triggers from extracted `debt_facility`, `debt_refinancing`, or `bond_issuance` events
- `suspension_notice`: triggers from extracted `suspension_related_notice` events
- `material_transaction`: triggers from extracted `material_transaction` events

Observed live outputs on the current pilot corpus:
- `idx_financial_reports`: 1 analyzed annual report, 6 triggered flags
- `idx_disclosures`: 4 analyzed disclosure documents, 7 triggered flags

Known limitations and false-positive patterns:
- Funding-related disclosure documents can trigger both `funding_event` and `management_change` when one filing covers several AGM agenda items. This is expected for now because the pipeline operates at document scope, not per-agenda scope.
- The IDX “ESG submission” wrapper document under the financial-report stream is intentionally excluded from rule analysis because Phase 5 classifies it as `unsupported_document_kind:attachment_notice`.
- `profit_drop` currently uses a negative-profit fallback because prior-period deltas are not yet materialized into snapshots. A later ruleset revision should replace this with true period-over-period comparisons.
- `debt_surge` currently uses a simple leverage threshold and does not yet compare against issuer history.

Next improvement targets:
- add period-over-period metric snapshots for true delta rules
- split multi-topic disclosures into finer event scopes
- add audit sampling over a larger issuer set before widening thresholds
