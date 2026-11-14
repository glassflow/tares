// Config for `npm run lint` (CI job `console`). typescript-eslint's recommended set, the two
// classic React hooks rules (the code already carries `react-hooks/exhaustive-deps` disable
// comments for them), and `no-explicit-any` at error, which the tree passes today. Nothing else
// for now. The design and token gates run as their own configs so each fails on its own line in CI.
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'tests/**', '*.config.*'] },
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      '@typescript-eslint/no-explicit-any': 'error',
      // A leading underscore marks an argument kept for its position (an event, a callback's index).
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' }],
      // `has ? set.delete(x) : set.add(x)` is how the console toggles a Set in a state updater.
      '@typescript-eslint/no-unused-expressions': ['error', { allowTernary: true, allowShortCircuit: true }],
    },
  },
)
