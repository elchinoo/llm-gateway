// Records every call; answers from globalThis.__api (a function path, opts) -> data.
export async function api(url, opts = {}) {
  globalThis.__calls.push({ url, opts });
  return globalThis.__api(url, opts);
}
