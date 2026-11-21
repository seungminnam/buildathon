# Blast Radius entry

Test selection for the RWX Blast Radius track: given a patch to sqlglot at
`eb11aec`, print which of its tests to run and how to split them over up to 8
workers.

How it decides, per commit:

1. Parse the patch and work out what changed (`blast/patch.py`, `blast/impact.py`).
   Edits that leave the syntax tree unchanged are dropped. Edits inside a
   function body pick the tests that ran the edited statement; edits to class
   or module level pick the tests that read the changed name and used the
   class; fixture edits pick the tests that opened the file.
2. The coverage map behind this (`maps/coverage.json`) is built once from the
   pinned commit by tracing every test, including the work the tests hand to
   process pools (`tools/covmap/`).
3. When it is likely to pay off, run the short candidates against the patched
   tree inside the container and drop the ones that pass (`blast/verify.py`,
   `blast/overlay.py`). Long tests are listed rather than run.
4. Pack what is left onto the number of workers that costs least (`blast/pack.py`).

Local check with the starter kit unpacked into `sample/`:

    make sample

Notes on the approach and on what went wrong along the way are in `docs/`
(Korean).
