# Agent Instructions

All Python code must be formatted with `black` and `isort`.

## PR Quality Gate (Mandatory)

Before running `gh pr create`, you MUST run all relevant linters and tests and fix all issues:

If lint fails, do not create the PR until it passes.

Additionally, you MUST run backend tests.
```bash
npm run test:python
```

If tests fail, do not create the PR until they pass.  Tests should be presumed to be correct; do not "fix" tests
by altering assertions or otherwise changing the underlying behavior of the test without getting confirmation.

## Main Branch Safeguard (Mandatory)

- All agent work MUST be done on feature branches.  Name the feature branch `feature/<short-description>` 
- For feature branches, the agent may create branches, add files, commit, push, and open PRs without repeated approval.
- If the current branch is `main`, the agent MUST ask for explicit user approval before running `git add`, `git commit`, or `git push`.
- A repo-managed `pre-push` hook blocks direct pushes to `main` by default.
- Activate hooks locally with:
  - `git config core.hooksPath .githooks`
- To intentionally bypass once:
  - `ALLOW_MAIN_PUSH=1 git push <remote> <source>:main`
