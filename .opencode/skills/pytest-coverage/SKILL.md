---
name: pytest-coverage
description: 'Run pytest tests with coverage, discover lines missing coverage, and increase coverage to 100%.'
---

The goal is for the tests to cover all lines of code.

Generate a coverage report with:

pytest --cov --cov-report=annotate:cov_annotate

If you are checking for coverage of a specific module, you can specify it like this:

pytest --cov=your_module_name --cov-report=annotate:cov_annotate

You can also specify specific tests to run, for example:

pytest tests/test_your_module.py --cov=your_module_name --cov-report=annotate:cov_annotate

Open the cov_annotate directory to view the annotated source code.
There will be one file per source file. If a file has 100% source coverage, it means all lines are covered by tests, so you do not need to open the file.

For each file that has less than 100% test coverage, find the matching file in cov_annotate and review the file.

If a line starts with a ! (exclamation mark), it means that the line is not covered by tests.
Add tests to cover the missing lines.

Keep running the tests and improving coverage until all lines are covered.

## Stale-Test Classification

When tests fail during verification:

1. **Prove pre-existence** - Check out prior commits in a git worktree (`git worktree add <path> <commit>`); run the failing tests there. If they fail identically, the tests are stale, not broken by recent changes.
2. **Classify by cause** - Typical stale causes: pixel-convention changes, removed APIs, unreachable thresholds.
3. **Fix only verified defects** - Change production code only when the failure reproduces from a recent change; otherwise update the tests to match current expectations.
4. **Document** - Record stale tests in `Known_Issues.md` (or equivalent) instead of silently editing them.
