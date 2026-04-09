ALTER TABLE management_claims ADD COLUMN claim_type TEXT;
ALTER TABLE management_claims ADD COLUMN claim_subject TEXT;
ALTER TABLE management_claims ADD COLUMN claim_time_horizon TEXT;
ALTER TABLE management_claims ADD COLUMN claim_confidence REAL;
ALTER TABLE management_claims ADD COLUMN payload_json TEXT;

CREATE INDEX IF NOT EXISTS idx_management_claims_type
    ON management_claims(claim_type, status);
