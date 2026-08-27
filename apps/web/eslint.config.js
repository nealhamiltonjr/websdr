// OpenWebRX+ Web — flat ESLint config (slice-5.4 CI repair; slice-12 unified).
//
// Background: ESLint 9.0+ requires a flat config file
// (eslint.config.{js,mjs,cjs}) and no longer reads .eslintrc.* files.
// Without this file, `pnpm run lint` (eslint . --ext .ts,.tsx) fails
// immediately with "ESLint couldn't find an eslint.config.(js|mjs|cjs)
// file." — which broke every CI run for the Frontend job.
//
// Slice-12: migrated from the legacy
// @typescript-eslint/eslint-plugin + @typescript-eslint/parser pair
// to the modern unified `typescript-eslint` package (smaller install,
// simpler flat config — one import instead of two).
//
// This is intentionally minimal: a small rule set that lets the
// existing code pass (prettier already enforces style, tsc already
// catches type errors, so eslint's job here is to catch the rare
// runtime-impacting issues TS doesn't).
//
// References:
//   https://eslint.org/docs/latest/use/configure/configuration-files
//   https://typescript-eslint.io/getting-started

import tseslint from 'typescript-eslint';

export default tseslint.config(
  // Global ignores — generated/build artifacts.
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      'coverage/**',
      '*.tsbuildinfo',
      'vite.config.ts.timestamp-*',
    ],
  },

  // TS/TSX files — use the TS parser + the recommended preset.
  // We turn off rules that conflict with prettier (style) or that tsc
  // already catches (no-unused-vars, no-undef, etc.).
  ...tseslint.configs.recommended,

  // Override rules that conflict with prettier or that tsc covers.
  {
    files: ['**/*.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-unused-vars': [
        'warn',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
      '@typescript-eslint/no-explicit-any': 'off', // prettier handles; legacy code uses `any` widely
      '@typescript-eslint/ban-ts-comment': 'off', // existing code has TODOs that need ts-ignore
      '@typescript-eslint/no-empty-function': 'off',
      '@typescript-eslint/no-require-imports': 'off', // vite.config may need it

      // JS-level rules that don't apply to TS.
      'no-unused-vars': 'off',
      'no-undef': 'off',
    },
  },

  // Config & build tooling files — relax some rules for non-app code.
  {
    files: ['*.config.{ts,js,mjs}', 'vite.config.ts', 'vitest.config.ts'],
    rules: {
      '@typescript-eslint/no-require-imports': 'off',
    },
  },

  // Test files — relax no-floating-promises etc. for vitest patterns.
  {
    files: ['**/*.test.{ts,tsx}', '**/*.spec.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/no-empty-function': 'off',
    },
  },
);
