(() => {
  'use strict';
  const labels = Object.freeze({ extracting: 'Extração', downloading: 'Transferência da fonte', converting: 'Conversão e ajustes', delivering: 'Entrega' });
  const nf = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 2 });
  function validate(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const entries = Object.entries(value);
    if (!entries.length || entries.length > 4 || entries.some(([key, n]) => !Object.hasOwn(labels, key) || typeof n !== 'number' || !Number.isFinite(n) || n < 0 || n > Number.MAX_SAFE_INTEGER)) return null;
    return Object.fromEntries(entries);
  }
  function format(value) { return `${nf.format(value)} s`; }
  function render(value) {
    const panel = document.getElementById('download-stage-times');
    const times = validate(value);
    if (!panel || !times) return;
    panel.hidden = false;
    for (const key of Object.keys(labels)) {
      const field = panel.querySelector(`[data-stage-time="${key}"]`);
      if (field) field.textContent = Object.hasOwn(times, key) ? format(times[key]) : 'Ainda não medido';
    }
  }
  function reset() {
    const panel = document.getElementById('download-stage-times');
    if (!panel) return;
    panel.hidden = true; panel.open = false;
    panel.querySelectorAll('[data-stage-time]').forEach(field => { field.textContent = 'Ainda não medido'; });
  }
  window.OndaStageTimes = Object.freeze({ validate, format, render, reset, labels });
})();
