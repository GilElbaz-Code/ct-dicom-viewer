-- Existing databases: permit searchable metadata before the analysis succeeds.
-- New installations already have this definition in schema.sql.
BEGIN;
ALTER TABLE series ALTER COLUMN sagittal DROP NOT NULL;
COMMIT;
