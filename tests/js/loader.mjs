// Resolves the absolute URLs the browser modules import: /static/* to the repo, /kit/ui.js to agentpg-kit, the rest to stubs.
import { pathToFileURL } from "node:url";
import path from "node:path";

let cfg;
export function initialize(data) { cfg = data; }

export async function resolve(specifier, context, next) {
  if (specifier.startsWith("/static/")) return { url: pathToFileURL(path.join(cfg.static, specifier.slice(8))).href, shortCircuit: true };
  if (specifier === "/kit/ui.js") return { url: pathToFileURL(path.join(cfg.kit, "ui.js")).href, shortCircuit: true };
  if (specifier.startsWith("/kit/")) return { url: pathToFileURL(path.join(cfg.stubs, specifier.slice(5))).href, shortCircuit: true };
  return next(specifier, context);
}
