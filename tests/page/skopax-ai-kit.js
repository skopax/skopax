const pageHtml = require('./skopax-page');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const ADMIN_USER = 'dora.lane@example.com';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const GROUPS = {Anthropic: ['claude-opus-5-5', 'claude-sonnet-5-5'], OpenAI: ['gpt-6.2-astra', 'gpt-6.2-sol'], 'deepseek (compatible)': ['deepseek/deepseek-chat']};
const MODELS = [].concat(GROUPS.Anthropic, GROUPS.OpenAI, GROUPS['deepseek (compatible)']);
const PRICES = {'claude-opus-5-5': {input: 5, output: 25}, 'claude-sonnet-5-5': {input: 3, output: 15}, 'gpt-6.2-astra': {input: 1.25, output: 10}, 'gpt-6.2-sol': {input: 0.4, output: 1.6}, 'deepseek/deepseek-chat': {input: 0.27, output: 1.1}};

function item(key, label, extra) {
  return Object.assign({key, group: 'Investigator', label, help: label + ' help', kind: 'choice', live: true, value: 'claude-opus-5-5', base: 'claude-opus-5-5', default: 'claude-opus-5-5', source: 'default', by: null, at: null, choices: MODELS, choiceGroups: GROUPS}, extra || {});
}

function effort(key, label) {
  return item(key, label, {value: 'high', base: 'high', default: 'high', choices: ['low', 'medium', 'high'], choiceGroups: undefined});
}

function settings() {
  return [
    item('investigate.provider', 'Default provider', {value: 'anthropic', base: 'anthropic', default: 'anthropic', choices: ['anthropic', 'openai'], choiceGroups: undefined}),
    item('investigate.model', 'Card model'), effort('investigate.effort', 'Card effort'),
    item('investigate.model-critical', 'Model for Critical cards', {value: '', base: '', default: '', choices: [''].concat(MODELS), choiceLabels: {'': 'Same as Card runs'}}),
    item('investigate.model-high', 'Model for High cards', {value: 'deepseek/deepseek-chat', base: '', default: '', choices: [''].concat(MODELS), choiceLabels: {'': 'Same as Card runs'}, source: 'admin', by: ADMIN_USER, at: iso(60)}),
    item('investigate.slack-model', 'Slack model', {value: 'gpt-6.2-astra'}), effort('investigate.slack-effort', 'Slack effort'),
    item('chat.model', 'Chat model', {value: 'gpt-6.2-sol'}), effort('chat.effort', 'Chat effort'),
    item('investigate.gate-model', 'Topic gate model', {value: 'deepseek/deepseek-chat'}), effort('investigate.gate-effort', 'Topic gate effort'),
    item('self-review.model', 'Model', {group: 'Self-review', value: 'gpt-6.2-astra', help: 'Model of the daily self-review'}),
    item('self-review.hour', 'Hour', {group: 'Self-review', kind: 'int', value: '7', base: '7', default: '7', low: 0, high: 23, choices: undefined, choiceGroups: undefined}),
    item('core.allow-private-ai', 'Private AI addresses', {kind: 'bool', value: 'false', base: 'false', default: 'false', choices: undefined, choiceGroups: undefined}),
    item('investigate.rooms', 'Parallel rooms', {kind: 'int', value: '2', base: '2', default: '2', low: 1, high: 6, choices: undefined, choiceGroups: undefined}),
  ];
}

function company(model) {
  return model.indexOf('deepseek/') === 0 ? 'api.deepseek.example.com' : model.indexOf('gpt-') === 0 ? 'OpenAI' : 'Anthropic';
}

const SHAPES = JSON.parse(fs.readFileSync(path.join(__dirname, 'skopax-ai-shapes.json'), 'utf8'));

function shaped(kind, obj) {
  const keys = Object.keys(obj).sort().join(',');
  if (keys !== SHAPES[kind].join(',')) throw new Error('the kit ' + kind + ' has keys ' + keys + ', the server sends ' + SHAPES[kind].join(','));
  return obj;
}

function provider(model) {
  return model.indexOf('deepseek/') === 0 ? 'compat:deepseek' : model.indexOf('gpt-') === 0 ? 'openai' : 'anthropic';
}

const USES = [['card', 'Card runs', 'investigate.model'], ['critical', 'Critical cards', 'investigate.model-critical'], ['high', 'High cards', 'investigate.model-high'], ['slack', 'Slack answers', 'investigate.slack-model'],
  ['chat', 'Chat', 'chat.model'], ['gate', 'Gate and thread summaries', 'investigate.gate-model'], ['review', 'Daily self-review', 'self-review.model'], ['plugin', 'Plug-in AI', 'plugai.model']];
const COSTS = {card: 0.42, critical: 1.234, high: 0.0123, slack: 0.05, chat: 0.004, gate: 0.001, review: 0.3, plugin: null};

function uses(list, extra) {
  const value = (key) => { const i = list.find((x) => x.key === key); return i ? i.value : ''; };
  const card = value('investigate.model');
  return USES.map(([use, label, setting]) => {
    const own = setting === 'plugai.model' ? 'claude-sonnet-5-5' : value(setting) || card;
    const cheap = provider(own).indexOf('compat:') === 0;
    return Object.assign(shaped('uses', {use, label, setting, model: own, provider: provider(own), company: company(own), price: setting === 'plugai.model' ? null : PRICES[own] || null, avgCost7d: COSTS[use], problem: null, fit: cheap ? 'cheap' : 'ok'}), (extra || {})[use] || {});
  });
}

function connection(name, extra) {
  const base = shaped('connection', {name, provider: 'compat:' + name, baseUrl: 'https://api.' + name + '.example.com/v1', key: {set: true, unreadable: false},
    models: [shaped('model', {id: name + '-chat', ref: name + '/' + name + '-chat', input: 0.27, output: 1.1})], tools: true, images: false, company: 'api.' + name + '.example.com', updatedBy: ADMIN_USER,
    updatedAt: iso(60), lastTest: null});
  return Object.assign(base, extra || {});
}


function pack(template, lang) {
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(template), '..', 'locale', lang, 'page.json'), 'utf8'));
  return Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers});
}

async function boot(template, opts) {
  const o = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const srv = {items: o.items || settings(), extra: o.uses || {}, conns: o.connections || [], allowPrivate: !!o.allowPrivate, refuse: null, test: shaped('test', {ok: true, tools: true, summary: 'Answers, uses tools', ms: 300}), hold: null, rows: o.rows || null,
    usesFail: o.usesFail || 0, putHold: null, inflight: 0, most: 0, lastTest: null};
  const keyStatus = {using: 'admin', admin: true, config: false, updatedBy: ADMIN_USER, updatedAt: iso(30), note: null};
  const dom = new JSDOM(pageHtml(template).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + (o.hash || '#t=ai'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (o.lang) { w.SKOPAX_I18N = {[o.lang]: pack(template, o.lang)}; w.document.cookie = 'skopax_lang=' + o.lang + '; path=/'; }
      w.fetch = async (p0, init) => {
        const p = String(p0), method = (init && init.method) || 'GET', body = init && init.body !== undefined ? JSON.parse(init.body) : undefined;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ADMIN_USER, userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p === '/api/admin/anthropic-key' || p === '/api/admin/openai-key') return ok(keyStatus);
          if (p === '/api/admin/ai-uses') return srv.usesFail ? bad(srv.usesFail, 'down') : ok({uses: srv.rows || uses(srv.items, srv.extra)});
          if (p === '/api/admin/ai-connections') {
            const snap = JSON.parse(JSON.stringify(shaped('connections', {items: srv.conns, allowPrivate: srv.allowPrivate})));
            if (srv.connHold) { const h = srv.connHold; srv.connHold = null; await h; }
            return ok(snap);
          }
          const conn = /^\/api\/admin\/ai-connections\/([^/]+)(\/test)?$/.exec(p);
          if (conn) {
            const name = decodeURIComponent(conn[1]);
            if (conn[2]) {
              if (srv.hold) await srv.hold;
              if (srv.lastTest) srv.conns = srv.conns.map((c) => c.name === name ? Object.assign({}, c, {lastTest: srv.lastTest}) : c);
              return ok(srv.test);
            }
            if (method === 'DELETE') { srv.conns = srv.conns.filter((c) => c.name !== name); return {ok: true, status: 204, json: async () => { throw new Error('no body'); }}; }
            if (srv.refuse) return bad(422, srv.refuse);
            const was = srv.conns.find((c) => c.name === name);
            const next = connection(name, {baseUrl: body.baseUrl, key: {set: !!body.key || !!(was && was.key.set)}, tools: body.tools, images: body.images,
              models: body.models.map((m) => ({id: m.id, ref: name + '/' + m.id, input: m.input === undefined ? null : m.input, output: m.output === undefined ? null : m.output}))});
            srv.conns = was ? srv.conns.map((c) => c.name === name ? next : c) : srv.conns.concat([next]);
            return ok(next);
          }
          if (p.startsWith('/api/admin/settings?key=') && method === 'DELETE') {
            const key = decodeURIComponent(p.split('key=')[1]);
            const was = srv.items.find((i) => i.key === key);
            const next = Object.assign({}, was, {value: was.base, source: 'default', by: null, at: null});
            srv.items = srv.items.map((i) => i.key === key ? next : i);
            return ok({item: next, change: {}});
          }
          if (p === '/api/admin/settings' && method === 'PUT') {
            srv.inflight++;
            srv.most = Math.max(srv.most, srv.inflight);
            if (srv.putHold) await srv.putHold;
            srv.inflight--;
            if (srv.refuse) return bad(422, srv.refuse);
            const was = srv.items.find((i) => i.key === body.key);
            const next = Object.assign({}, was, {value: body.value, source: 'admin', by: ADMIN_USER, at: iso(0)});
            srv.items = srv.items.map((i) => i.key === body.key ? next : i);
            return ok({item: next, change: {}});
          }
          if (p === '/api/admin/settings') return ok({items: srv.items, rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/plugins') return ok({plugins: []});
          return bad(404, 'none');
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    }});
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls, srv};
}

function nameOf(doc, c) {
  const textOf = (ids) => ids.split(/\s+/).map((id) => doc.getElementById(id)).filter(Boolean).map((e) => e.textContent.trim()).join(' ').trim();
  return (c.getAttribute('aria-labelledby') && textOf(c.getAttribute('aria-labelledby'))) || (c.getAttribute('aria-label') || '').trim() ||
    (c.id && [...doc.querySelectorAll('label[for="' + c.id + '"]')].map((l) => l.textContent.trim()).join(' ')) || (c.closest('label') ? c.closest('label').textContent.trim() : '') ||
    (c.tagName === 'BUTTON' ? c.textContent.trim() : '') || (c.tagName === 'SUMMARY' ? c.textContent.trim() : '');
}

const typeIn = (w, inp, v) => { inp.value = v; inp.dispatchEvent(new w.Event('input', {bubbles: true})); };

module.exports = {boot, settings, uses, connection, nameOf, typeIn, wait, iso, GROUPS, MODELS, ADMIN_USER};
