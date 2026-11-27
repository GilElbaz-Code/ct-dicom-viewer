-- For a database created from schema.sql before sagittal planes were stored as float32.
-- Stop the workers first, then run once (running it again changes nothing):
--   docker compose exec -T db psql -v ON_ERROR_STOP=1 -U ctviewer -d ctviewer < backend/migrations/001_sagittal_encoding.sql
-- Existing planes are int16 HU in the little-endian byte order of the amd64/arm64 workers that wrote them.
-- They stay readable as they are. Their fractional HU were truncated when written and come back only by
-- indexing the series again from its DICOM files.
BEGIN;
ALTER TABLE series ADD COLUMN IF NOT EXISTS sagittal_encoding text;
UPDATE series SET sagittal_encoding = 'int16le' WHERE sagittal IS NOT NULL AND sagittal_encoding IS NULL;
ALTER TABLE series DROP CONSTRAINT IF EXISTS series_sagittal_encoding_check;
ALTER TABLE series ADD CONSTRAINT series_sagittal_encoding_check CHECK (
    (sagittal IS NULL) = (sagittal_encoding IS NULL) AND sagittal_encoding IN ('float32le', 'int16le'));
COMMIT;
