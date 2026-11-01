-- Row MACs: an HMAC under a subkey of the vault data key over the columns that grant access or carry trust (see rowmac.py).
-- Null on rows written before this migration: while the vault is unlocked they are ignored until `llmgw adopt-rowmacs` signs them.
ALTER TABLE client ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE client_key ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE provider ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE model_alias ADD COLUMN IF NOT EXISTS mac text;
ALTER TABLE alias_target ADD COLUMN IF NOT EXISTS mac text;
