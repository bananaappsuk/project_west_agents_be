-- crm_status/crm_reference have existed on the Recording model for a while but
-- never had a migration written for them (found while wiring up the production
-- CRM integration — production's `recordings` table never had them at all).
-- Same manual-migration convention as the other migrate_*.sql files here.
-- Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE recordings ADD COLUMN IF NOT EXISTS crm_status VARCHAR NOT NULL DEFAULT 'none';
ALTER TABLE recordings ADD COLUMN IF NOT EXISTS crm_reference VARCHAR;
