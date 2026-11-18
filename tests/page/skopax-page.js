const fs = require('fs');
const path = require('path');

const TEMPLATE = path.join(__dirname, '..', '..', 'skopax', 'web', 'template.html');
const FOOTER = /^FOOTER = '(.+)'$/m.exec(fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'brand.py'), 'utf8'))[1];

module.exports = function pageHtml(template, opts) {
  template = template || TEMPLATE;
  const dir = path.join(path.dirname(template), 'static');
  const view = (opts && opts.view) || 'list';
  let first = true;
  const shift = Number(process.env.PAGE_CLOCK_SHIFT_MS || 0);
  const clock = shift ? `<script>window.Date = (${require('./clock').shiftedDate.toString()})(window.Date, ${shift});</script>\n` : '';
  const brand = (opts && opts.brand) || {name: 'Skopax', slug: 'skopax', version: '0.1.0', attribution: {text: 'Skopax 0.1.0 \u00b7 skopax.io', href: 'https://skopax.io'}};
  const escapeHtml = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
  const line = brand.attribution;
  const fill = (markup) => line ? markup.replace(/\{(href|text)\}/g, (m, key) => escapeHtml(line[key])) : '';
  const footer = fill(FOOTER), tail = fill(FOOTER.replace(' id="attribution"', ''));
  const text = fs.readFileSync(template, 'utf8').replace(/\[\[brand\]\]/g, escapeHtml(brand.name)).replace(/\[\[mark\]\]/g, '').replace(/\[\[wordmark\]\]/g, `<span class="wordmark" role="img" aria-label="${escapeHtml(brand.name)}">${escapeHtml(brand.name)}</span>`)
    .replace(/\[\[attribution_html\]\]/g, () => footer).replace(/\[\[attribution_tail_html\]\]/g, () => tail).replace('/*__BRAND_DATA__*/', JSON.stringify(brand).replace(/</g, '\\u003c'));
  return text.replace(/<script src="\/static\/([\w.-]+?)(?:\?v=\w+)?"><\/script>/g, (tag, file) => {
    const shim = (first ? clock : '') + (first && view !== 'default' ? `<script>try { if (!localStorage.getItem('skopax-card-view')) localStorage.setItem('skopax-card-view', '${view}'); } catch (e) {}</script>\n` : '');
    first = false;
    return shim + '<script>\n' + fs.readFileSync(path.join(dir, file), 'utf8').replace(/<\/script/gi, '<\\/script') + '</script>';
  });
};
