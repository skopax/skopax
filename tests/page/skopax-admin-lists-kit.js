const pageHtml = require('./skopax-page');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const ADMIN_USER = 'nina.white@example.com';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function item(key, group, label, extra) {
  return Object.assign({key, group, label, help: label + ' help', kind: 'text', live: true, value: '', base: '', default: '', source: 'default', by: null, at: null, limit: 1000}, extra || {});
}

const RULES = {count: 'at most 10 other workspaces can be listed', pair: 'write each entry as name=url', name: 'a name is 1 to 40 letters, digits, spaces, dots, dashes or underscores',
  url: 'the address must start with http:// or https:// and name a host', userinfo: 'the address must not hold a user name or password', repeat: 'the name is already used by an earlier entry'};

function links(extra) {
  return item('core.workspace-links', 'Workspaces', 'Other workspaces', Object.assign({card: 'Other workspaces', order: 0, links: {max: 10, nameMax: 40, rules: RULES}}, extra || {}));
}

function pack(template, lang) {
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(template), '..', 'locale', lang, 'page.json'), 'utf8'));
  return Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers});
}

async function boot(template, hash, items, opts) {
  const errors = [], calls = [], o = opts || {};
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  let list = items;
  const instance = o.instance || {name: 'production', label: 'production', from: 'environment'};
  const dom = new JSDOM(pageHtml(template).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (o.lang) { w.SKOPAX_I18N = {[o.lang]: pack(template, o.lang)}; w.document.cookie = 'skopax_lang=' + o.lang + '; path=/'; }
      w.fetch = async (p0, opts2) => {
        const p = String(p0), method = (opts2 && opts2.method) || 'GET', body = opts2 && opts2.body ? JSON.parse(opts2.body) : null;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/plugins') return ok({plugins: []});
          if (p.startsWith('/api/admin/small-talk') || p.startsWith('/api/admin/tags') || p.startsWith('/api/admin/teams') || p.startsWith('/api/admin/users') || p.startsWith('/api/admin/sso') || p.startsWith('/api/admin/mcp')) return {ok: false, status: 404, json: async () => ({})};
          if (p.startsWith('/api/admin/settings')) {
            if (method === 'GET') return ok({items: list, rev: 3, refreshSeconds: 15, instance});
            const key = method === 'DELETE' ? new URL('https://x.example.com' + p).searchParams.get('key') : body.key;
            const old = list.find((i) => i.key === key);
            if (o.refuse && method === 'PUT' && o.refuse(body)) return {ok: false, status: 422, json: async () => ({detail: o.refuse(body)})};
            const next = method === 'DELETE' ? Object.assign({}, old, {value: old.base, source: old.base ? 'config' : 'default', by: null, at: null})
              : Object.assign({}, old, {value: body.value, source: 'admin', by: ADMIN_USER, at: iso(0)});
            list = list.map((i) => i.key === key ? next : i);
            return ok({item: next, change: {}});
          }
          return {ok: false, status: 404, json: async () => ({})};
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ADMIN_USER, operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors, calls, stored: () => list};
}

const typeIn = (w, inp, value) => { inp.value = value; inp.dispatchEvent(new w.Event('input', {bubbles: true})); };
const key = (w, inp, k) => { const e = new w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}); inp.dispatchEvent(e); return e; };
const puts = (calls) => calls.filter((c) => c[0] === 'PUT').map((c) => c[2]);

module.exports = {pack, boot, item, links, typeIn, key, puts, wait, RULES, ADMIN_USER};
