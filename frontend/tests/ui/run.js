#!/usr/bin/env node
// Bundles tests/ui/oauth.behavior.test.jsx with esbuild (JSX, automatic runtime) and runs it under jsdom.
//   cd frontend && npm install --no-save jsdom esbuild && node tests/ui/run.js
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

let esbuild;
try {
  esbuild = require('esbuild');
  require.resolve('jsdom');
} catch {
  console.error('This needs jsdom and esbuild (not app dependencies): npm install --no-save jsdom esbuild');
  process.exit(2);
}

const root = path.resolve(__dirname, '..', '..');
const out = path.join(os.tmpdir(), 'slate-ui-tests', 'oauth.behavior.cjs');
esbuild.buildSync({
  entryPoints: [path.join(__dirname, 'oauth.behavior.test.jsx')],
  bundle: true, platform: 'node', format: 'cjs', outfile: out,
  loader: { '.js': 'jsx', '.jsx': 'jsx' }, jsx: 'automatic',
  nodePaths: [path.join(root, 'node_modules')],
  define: { 'process.env.NEXT_PUBLIC_API_URL': '"http://api.test"', 'process.env.NODE_ENV': '"test"' },
  external: ['jsdom'], logLevel: 'error',
});
const result = spawnSync(process.execPath, ['-r', path.join(__dirname, 'setup.js'), out], { stdio: 'inherit' });
process.exit(result.status === null ? 1 : result.status);
