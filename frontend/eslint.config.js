// ESLint flat config (ESLint 9+ / typescript-eslint 8+).
//
// Scope: catch the class of bug this project has actually shipped —
// stale closures and missing effect dependencies. Several of those
// were found only by manual review (a keyboard-shortcut handler
// reading stale state, a useMemo with an incomplete dependency
// array) because nothing was catching them automatically; this is
// that safety net.
//
// We deliberately do NOT pull in eslint-plugin-react-hooks' full v7
// "recommended-latest" rule bundle (immutability / purity /
// set-state-in-render / etc.) — those are React Compiler-readiness
// rules aimed at codebases adopting the compiler. This app is on
// React 18 without the compiler, so most of that bundle would be
// noise unrelated to the bugs we're actually trying to catch. Just
// the two classic, well-understood rules instead.
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
      // A handful of intentionally-unused destructured values exist
      // for documentation/shape-matching purposes; warn (and allow
      // an explicit `_` prefix to opt out) rather than error.
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    },
  },
)
