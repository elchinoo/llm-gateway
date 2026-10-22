-- Operator-set request fields per alias target (thinking switches and the like), merged into the upstream body.
-- The reserved keys and the size limits are enforced in the application (aliases.validate_extra_body), not here.
ALTER TABLE alias_target ADD COLUMN IF NOT EXISTS extra_body jsonb NOT NULL DEFAULT '{}';
-- Thinking tokens the upstream reports inside completion_tokens (usage.completion_tokens_details.reasoning_tokens); null = not reported.
ALTER TABLE usage_event ADD COLUMN IF NOT EXISTS reasoning_tokens integer;
