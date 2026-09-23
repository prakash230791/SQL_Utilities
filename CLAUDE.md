# CLAUDE.md — CLR Retirement Toolkit

Python toolkit that inventories SQL Server CLR call sites on Azure SQL MI (Phase 1) and rewrites them to native T-SQL for Amazon RDS for SQL Server (Phase 2).
**Full spec: `docs/REQUIREMENTS.md` — read it before any task. Work the backlog in §15 in order, one task per session.**

## Commands
```bash
pip install -r requirements.txt -r requirements-dev.txt   # requirements-dev.txt created in T01
pytest -q
python demo/build_demo_inventory.py
python phase1_inventory.py --config demo/config.demo.yaml --reanalyze demo/output/DEMO_RUN
python phase2_convert.py  --config demo/config.demo.yaml --inventory demo/output/DEMO_RUN
```
Run all of the above after every change. The demo statuses in REQUIREMENTS §4.2 are the baseline and must not regress.

## Layout
- `clr_migrator/tsql_lexer.py` tokenizer · `analyzer.py` site detection + classes · `rewriter.py` span edits · `validators.py` checks + RDS lint + live compile · `config.py` · `db.py` connections · `common.py` · `reporting.py`
- `phase1_inventory.py`, `phase2_convert.py` — CLIs
- `demo/` — offline fixture (no DB needed)

## Hard rules (REQUIREMENTS §16)
1. Never remove the `ISNULL(..., N'')` wrapper or the `CAST(... AS NVARCHAR(MAX))` from STRING_AGG rewrites.
2. Never auto-add `WITHIN GROUP`. Ordering comes from config only.
3. When a heuristic is unsure, classify the site as C (manual). A false AUTO is the worst possible bug.
4. Edits are by character offset against the original text. Never reformat, re-indent or normalize whitespace/newlines. Read and write SQL with `newline=''`.
5. Never write to any database. The only DB action allowed is the live compile inside a transaction that is always rolled back.
6. Never put secrets in config, CLI args or logs. Use env-var names or AWS Secrets Manager only.
7. The `inventory.json` schema (§11.4) is a contract: additive changes only.
8. Do not add a SQL parser dependency (sqlglot, sqlparse, etc.).

## Conventions
- Python 3.10+, type hints on public functions, ruff-clean, no bare `except` unless commented.
- New behavior needs a unit test. Bug fixes need a regression test that fails before the fix.
- If a change alters demo output, update `tests/golden/` and explain why in the commit message.
- Commit message format: `T0x: <summary>`.
- If a task appears to conflict with a hard rule, stop and ask instead of working around it.
