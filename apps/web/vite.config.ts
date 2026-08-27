/// <reference types="vite/client" />
// Note: this file is the shared Vite config. The `test:` field at the
// bottom is read by vitest at runtime but is NOT type-checked (we
// intentionally don't load vitest's UserConfig augmentation here —
// vitest 2.1.x's augmentation was built against vite 5, and the project
// uses vite 6, so loading the augmentation causes a Plugin-type
// conflict). The `// @ts-expect-error` on the `test:` line suppresses
// the resulting "test does not exist in type UserConfig" error. To
// drop this workaround, upgrade vitest to 3.x in slice-5.7+ (which
// has first-class vite 6 support).
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
  // Vitest config — slice-5.6 CI repair.
  //
  // vitest 2.x ships with a deps optimizer that scans every dep's
  // peerDependencies and tries to resolve them. `jsdom` is declared
  // as an OPTIONAL peer dep of vitest itself (and of @vitest/browser
  // and a few others). Without jsdom installed, the optimizer emits:
  //
  //   MISSING DEPENDENCY  Cannot find dependency 'jsdom'
  //
  // and — critically — exits 1 even when every test passes. All our
  // test files are pure-logic (no DOM) and explicitly annotate with
  // `// @vitest-environment node`; we don't need jsdom. So:
  //   1. Set the default environment to `node` (belt-and-suspenders
  //      on top of the per-file annotations).
  //   2. Exclude `jsdom` from both the SSR and web dep optimizers so
  //      vite stops trying to resolve it.
  //
  // Note: the `test` field is read by vitest at runtime but not
  // type-checked here (see file header for why). The directive on
  // the next line suppresses the "test does not exist in type
  // UserConfig" error that would otherwise fire.
  // @ts-expect-error — see file header; vitest 2.x augmentation conflicts with vite 6
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
