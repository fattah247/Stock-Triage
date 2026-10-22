-- Snoozes now expire; review items also gain the system-managed 'superseded'
-- status (signal no longer raised), which needs no schema change.
ALTER TABLE review_queue_items ADD COLUMN snoozed_until TEXT;
