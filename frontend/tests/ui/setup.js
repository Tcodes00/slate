// Runs BEFORE React loads: builds the DOM the components will render into.
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost:3000/', pretendToBeVisual: true });
// jsdom's window.location is unforgeable (assigning href would try a real navigation). A stand-in lets tests set
// the fragment the app is opened with and observe where a "click" would have sent the browser.
const fakeLocation = { href: 'http://localhost:3000/', hash: '', pathname: '/', search: '', origin: 'http://localhost:3000' };
const win = new Proxy(dom.window, {
  get(t, k) {
    if (k === 'location') return fakeLocation;
    const v = t[k];
    return typeof v === 'function' && !/^[A-Z]/.test(String(k)) ? v.bind(t) : v;
  },
  set(t, k, v) { t[k] = v; return true; },
});
for (const k of Object.getOwnPropertyNames(dom.window)) {
  if (!(k in global)) Object.defineProperty(global, k, { configurable: true, get: () => dom.window[k] });
}
// the mirroring loop above installed getter-only globals; a plain assignment to them would be silently ignored
Object.defineProperty(global, 'window', { value: win, configurable: true, writable: true });
Object.defineProperty(global, 'location', { value: fakeLocation, configurable: true, writable: true });
global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.IS_REACT_ACT_ENVIRONMENT = true;
global.__fakeLocation = fakeLocation;
