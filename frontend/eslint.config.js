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

// The patterns that break on an iPhone are banned across all of src/
// (REDESIGN_PLAN.md, section 4, "Lint bans"; the scope grew PR by PR and
// covers everything from PR9). Core rules only, no extra dependency.
const MOBILE_SCOPE = ['src/**/*.{ts,tsx}']

// A class-name pattern banned in string literals and template text.
const ban = (re, message) => [
  { selector: `Literal[value=${re}]`, message },
  { selector: `TemplateElement[value.raw=${re}]`, message },
]

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
  {
    files: MOBILE_SCOPE,
    rules: {
      'no-restricted-syntax': ['error',
        { selector: "JSXOpeningElement[name.name='datalist']", message: 'iOS Safari never shows a datalist. Use CategoryPicker or a select.' },
        ...ban('/\\btext-\\[(?:[0-9]|10)px\\]/', 'Text under 11px is unreadable on a phone. Use text-[11px] or text-ios-caption.'),
        ...ban('/\\bmin-h-\\[(?:[0-9]|[1-3][0-9]|4[0-3])px\\]/', 'Tap targets need min-h-[44px].'),
        ...ban('/\\b(?:min-|max-)?h-screen\\b|\\d+vh\\]/', 'vh overshoots on iOS Safari. Use dvh or the height:100% chain.'),
        { selector: "CallExpression[callee.property.name='addEventListener'][arguments.0.value='mousedown']", message: 'iOS sends no mousedown for taps on empty space. Use pointerdown.' },
        { selector: 'JSXText[value=/\\\\u[0-9a-fA-F]{4}/]', message: 'JSX text does not process \\u escapes. Type the character itself.' },
      ],
      'no-restricted-globals': ['error', 'prompt', 'confirm', 'alert'],
      'no-restricted-properties': ['error',
        { object: 'window', property: 'prompt' },
        { object: 'window', property: 'confirm' },
        { object: 'window', property: 'alert' },
      ],
      'no-eval': 'error',
      'no-implied-eval': 'error',
      'no-new-func': 'error',
    },
  },
)
