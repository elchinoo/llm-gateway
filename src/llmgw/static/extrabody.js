// The per-target "Extra request fields" editor: presets and a validator that mirrors the server's (aliases.validate_extra_body).
export const RESERVED = ["model", "messages", "input", "stream", "user", "n"];
export const MAX_BYTES = 4096;
export const MAX_DEPTH = 4;

export const PRESETS = [
  { label: "Turn thinking off: OpenRouter", value: { reasoning: { enabled: false } } },
  { label: "Turn thinking off: vLLM/SGLang (Qwen3)", value: { chat_template_kwargs: { enable_thinking: false } } },
  { label: "Limit reasoning: OpenAI style", value: { reasoning_effort: "low" } },
];

// Depth counted in objects/lists: the top-level object is 1, {"a": {"b": 1}} is 2 (same rule as the server).
const depth = (v, level = 1) => (v && typeof v === "object"
  ? Math.max(level, ...Object.values(v).map((x) => depth(x, level + 1)))
  : level - 1);

// Returns {value} (an object; {} for an empty box) or {error} (a sentence for the operator).
export function parseExtraBody(text) {
  const raw = String(text ?? "").trim();
  if (!raw) return { value: {} };
  let v;
  try { v = JSON.parse(raw); } catch (e) { return { error: `Not valid JSON: ${e.message}` }; }
  if (v === null || typeof v !== "object" || Array.isArray(v)) return { error: "Must be a JSON object, for example {\"reasoning\": {\"enabled\": false}}." };
  const bad = RESERVED.filter((k) => Object.hasOwn(v, k));
  if (bad.length) return { error: `Cannot set ${bad.join(", ")}: the gateway owns those fields.` };
  if (depth(v) > MAX_DEPTH) return { error: `Nested too deeply (at most ${MAX_DEPTH} levels).` };
  if (new TextEncoder().encode(JSON.stringify(v)).length > MAX_BYTES) return { error: `Larger than ${MAX_BYTES / 1024} KB.` };
  return { value: v };
}

export const pretty = (obj) => (obj && Object.keys(obj).length ? JSON.stringify(obj, null, 2) : "");
