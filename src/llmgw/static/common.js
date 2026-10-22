import { h } from "/kit/ui.js";

const NS = "http://www.w3.org/2000/svg";

export function svgEl(tag, attrs, text) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}

export const num = (n) => Number(n || 0).toLocaleString();
export const usd = (n) => `$${Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: n && n < 1 ? 4 : 2 })}`;
export const compact = (n) => Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(Number(n || 0));

// A small horizontal bar for a 0..1+ share (budget used). Over 100% turns red.
export function meter(share, label) {
  const pct = Math.min(Math.max(share || 0, 0), 1);
  const svg = svgEl("svg", { viewBox: "0 0 100 10", class: "chart meter", role: "img", "aria-label": label });
  svg.append(svgEl("rect", { class: "track", x: 0, y: 0, width: 100, height: 10, rx: 3 }));
  svg.append(svgEl("rect", { class: share >= 1 ? "high" : share >= 0.8 ? "warn" : "ok", x: 0, y: 0, width: Math.round(pct * 100), height: 10, rx: 3 }));
  return svg;
}

// Horizontal bars: rows of {label, value}. Pure SVG, text through textContent.
export function hbars(rows, aria) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  const svg = svgEl("svg", { viewBox: `0 0 500 ${rows.length * 22 + 6}`, class: "chart", role: "img", "aria-label": aria });
  rows.forEach((r, i) => {
    svg.append(svgEl("text", { x: 0, y: i * 22 + 15 }, r.label.length > 22 ? r.label.slice(0, 21) + "…" : r.label));
    svg.append(svgEl("rect", { class: "bar", x: 150, y: i * 22 + 4, width: Math.max(2, Math.round((r.value / max) * 280)), height: 14 }));
    svg.append(svgEl("text", { x: 150 + Math.max(2, Math.round((r.value / max) * 280)) + 6, y: i * 22 + 15 }, compact(r.value)));
  });
  return svg;
}

export function pills(items) {
  return h("div", { class: "pill-list" }, (items || []).map((i) => h("span", { class: "badge plain" }, i)));
}

export const stamp = (s) => (s ? new Date(s).toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
