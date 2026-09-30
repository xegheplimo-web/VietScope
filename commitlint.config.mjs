// Conventional Commits — the commit-msg lefthook enforces this and
// cliff.toml generates the changelog from it.
export default {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum': [
      2,
      'always',
      [
        'feat', 'fix', 'perf',
        'docs', 'style', 'refactor',
        'test', 'ci', 'build', 'chore',
        'revert', 'security', 'deps',
      ],
    ],
    'subject-case': [2, 'never', ['start-case', 'pascal-case', 'upper-case']],
    'header-max-length': [2, 'always', 100],
  },
};
