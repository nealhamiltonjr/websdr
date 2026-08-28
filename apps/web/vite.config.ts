/// <reference types="vite/client" />
// Vitest 3.x has first-class vite 6 type support, so the `test:` field
// below is properly typed and the `// @ts-expect-error` workaround
// that vitest 2.x required (its augmentation was built against vite 5
// and conflicted with vite 6's Plugin type) is no longer needed.
// (slice-12 cleanup — was carried as a TODO since slice-5.6.)
import { defineConfig } from 'vite';
import solid from 'vite-plugin-solid';
import { resolve } from 'node:path';

// Vite config for the OpenWebRX+ SolidJS frontend.
// - SolidJS plugin for .tsx JSX
// - Worker config: SharedWorker entry bundled separately
// - Tailwind 4 via @tailwindcss/postcss in postcss.config.js
export default defineConfig({
  plugins: [solid()],
  resolve: {
    alias: {
      '~': resolve(__dirname, 'src'),
      '@openwebrx-plus/shared-types': resolve(__dirname, '../../packages/shared-types/src/index.ts'),
    },
  },
  // dockview-solid ships UNCOMPILED Solid JSX in its ESM output
  // (dist/esm/**/*.jsx). Vite's dep optimizer (esbuild) would compile that
  // JSX with the React classic transform → "React is not defined". Excluding
  // it from pre-bundling routes the raw files through the normal transform
  // pipeline where vite-plugin-solid (enforce: 'pre') compiles them with
  // babel-preset-solid. dockview-core (pure .js) stays optimized.
  optimizeDeps: {
    exclude: ['dockview-solid'],
  },
  // Vitest config — slice-12 cleanup: vitest 3.x has first-class vite 6
  // type support, so the `test:` field below is properly typed (no
  // ts-expect-error needed anymore). The node-environment default +
  // jsdom-exclusion belt-and-suspenders are retained because all our
  // tests are pure-logic (no DOM) and explicitly annotate with
  // `// @vitest-environment node`; we don't want the deps optimizer
  // trying to resolve jsdom (an optional peer dep of vitest) only to
  // emit "MISSING DEPENDENCY  Cannot find dependency 'jsdom'" and exit 1.
  test: {
    environment: 'node',
    deps: {
      optimizer: {
        ssr: { exclude: ['jsdom'] },
        web: { exclude: ['jsdom'] },
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    // Backend runs at :8073; dev proxy for /ws and /api
    proxy: {
      '/api': {
        target: 'http://localhost:8073',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8073',
        ws: true,
        changeOrigin: true,
      },
    },
  },
  worker: {
    format: 'es',
    plugins: () => [],
  },
  build: {
    target: 'esnext',
    sourcemap: true,
  },
});
