-- A provider's stored key is bound to the origin (scheme, host, port, path prefix) it was saved with, and is only sent there.
-- Existing rows are bound to their current base_url, so nothing changes for them; a later URL change needs the key again.
ALTER TABLE provider ADD COLUMN IF NOT EXISTS key_origin text;
UPDATE provider SET key_origin = base_url WHERE api_key_enc IS NOT NULL AND key_origin IS NULL AND base_url IS NOT NULL;
