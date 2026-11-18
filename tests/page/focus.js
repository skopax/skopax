const Module = require('module');
const path = require('path');

const FOCUSING = path.join(__dirname, 'node_modules', 'jsdom', 'lib', 'jsdom', 'living', 'helpers', 'focusing.js');
const COMPUTED = path.join(__dirname, 'node_modules', 'jsdom', 'lib', 'jsdom', 'living', 'css', 'helpers', 'computed-style.js');
const load = Module.prototype.require;

Module.prototype.require = function (request) {
  const loaded = load.apply(this, arguments);
  if (this.filename !== FOCUSING || Module._resolveFilename(request, this) !== COMPUTED) return loaded;
  return {...loaded, getComputedStylePropertyValue: (el, prop) => prop === 'display' ? '' : loaded.getComputedStylePropertyValue(el, prop)};
};
