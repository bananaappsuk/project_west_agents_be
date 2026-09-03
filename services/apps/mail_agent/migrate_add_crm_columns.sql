-- crm_status/crm_reference have existed on the Email model for a while but
-- never had a migration written for them (found while wiring up the production
-- CRM integration — production's `emails` table never had them at all).
-- Same manual-migration convention as migrate_add_intent_column.sql.
-- Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE emails ADD COLUMN IF NOT EXISTS crm_status VARCHAR NOT NULL DEFAULT 'none';
ALTER TABLE emails ADD COLUMN IF NOT EXISTS crm_reference VARCHAR;
