/* charts.js - shared chart helpers for Legba Console */

const COLORS = {
  bg: '#1f2937',        // gray-800
  bgCard: '#111827',    // gray-900
  border: '#374151',    // gray-700
  text: '#9ca3af',      // gray-400
  textLight: '#d1d5db', // gray-300
  cyan: '#22d3ee',      // cyan-400
  green: '#4ade80',     // green-400
  red: '#f87171',       // red-400
  yellow: '#facc15',    // yellow-400
  orange: '#fb923c',    // orange-400
  violet: '#a78bfa',    // violet-400
  blue: '#60a5fa',      // blue-400
  teal: '#2dd4bf',      // teal-400
  indigo: '#818cf8',    // indigo-400
  // Category colors (event types)
  conflict: '#f87171',
  political: '#60a5fa',
  economic: '#facc15',
  technology: '#a78bfa',
  health: '#4ade80',
  environment: '#2dd4bf',
  social: '#fb923c',
  disaster: '#f472b6',
  other: '#9ca3af',
};

function uPlotDefaults(width, height) {
  const ax = { stroke: COLORS.text, grid: { stroke: COLORS.border }, ticks: { stroke: COLORS.border } };
  return {
    width,
    height,
    axes: [{ ...ax }, { ...ax }],
    cursor: { show: true },
    legend: { show: false },
  };
}

function initSparklines() {
  document.querySelectorAll('[data-sparkline]').forEach(el => {
    if (el._sparklineInit) return;
    const values = JSON.parse(el.dataset.sparkline);
    const color = el.dataset.sparklineColor || COLORS.cyan;
    sparkline(el, values, { strokeColor: color, fillColor: 'none', strokeWidth: 2 });
    el._sparklineInit = true;
  });
}

function formatNumber(n) {
  if (n == null) return '--';
  const abs = Math.abs(n);
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/* htmx integration - reinitialize sparklines after swaps */
document.addEventListener('DOMContentLoaded', initSparklines);
document.addEventListener('htmx:afterSwap', initSparklines);
