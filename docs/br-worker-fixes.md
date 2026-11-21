# Blast Radius: three miss types found on validation, and the fixes

Scored on 45 labelled validation commits (made with `tools/mutate/gen.py`,
labelled by running the full suite on each patch). The v2 selector missed 3
of them. Each miss costs 300 s.

| Miss | Cause | Fix |
|---|---|---|
| `lambda *_: "CURRENT_TIMESTAMP"` edited in a generator's TRANSFORMS | A lambda whose body is one constant raises no LINE event, so the coverage map showed nobody running it | The tracer also records PY_START for lambdas (`tools/covmap/trace_plugin.py`, `sitecustomize.py`) |
| `YearOfWeek._sql_names` changed | Functions are parsed by name through a registry built at import, so no sqlglot line mentions the class | For classes under `sqlglot/expressions/`, also select the tests whose SQL (test source or fixture files they open) names the class (`Analyzer.sql_name_tests`) |
| `Select.arg_types["from_"]` made required | A parse that runs when `tests/dialects/test_athena.py` is imported now fails, so the file cannot be collected and all its tests fail | New per-test-file import map (`tools/covmap/import_trace.py` -> `import_files` in the map): a change to code that a file's import runs selects the whole file |

Also: multi-line statements only report some of their lines as run, so every
line of a statement inside a function now carries the whole statement's tests
(done once in `tools/covmap/build_map.py`), and the no-op check compares
classes and functions statement by statement (CPU on 45 commits 5 s -> 1.3 s
on the host).

## Scores

| Set | Simulated | Misses | Oracle |
|---|---|---|---|
| 45 validation, v2 (9824295) | ~1255 s | 3 | 131.5 |
| 45 validation, this commit | 318.7 s (+ ~7 s CPU, arm64) | 0 | 131.5 |
| sample 30, this commit | 163.6 s (+ ~3 s CPU, arm64) | 0 | 94.35 |

Sample cost rose from 156.4 s because the candidate sets are wider; that is
the price of the 0 misses above.
