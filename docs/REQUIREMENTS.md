# CLR Retirement Toolkit — Requirements Specification

| Item | Value |
|---|---|
| Document version | 1.0 (2026-09-22) |
| Owner | Prakash (Data/Solutions Architect) |
| Code version at hand-off | `clr_migrator` 1.0.0 — core engine written and demo-verified; hardening, tests and docs outstanding |
| Source platform | Azure SQL Managed Instance (database `SAMPLEDB` and others) |
| Target platform | Amazon RDS for SQL Server (2019 / 2022 expected) |
| Implementation agent | Claude Code (Sonnet) — see §0 |

---

## 0. How to use this document with Claude Code

1. `CLAUDE.md` at the repo root holds the standing rules; this file holds the full specification. Read both before any task.
2. Work the backlog in §15 **one task per session**, in order. Each task lists files, requirements satisfied (IDs) and a Definition of Done (DoD).
3. For any task touching `analyzer.py`, `rewriter.py` or `validators.py`, start in **plan mode**, state the plan, then implement.
4. After every task run:
   ```bash
   pytest -q
   python demo/build_demo_inventory.py
   python phase1_inventory.py --config demo/config.demo.yaml --reanalyze demo/output/DEMO_RUN
   python phase2_convert.py  --config demo/config.demo.yaml --inventory demo/output/DEMO_RUN
   ```
   Once T03 exists, the golden-file test must stay green. If a change legitimately alters output, regenerate goldens **and** explain the diff in the commit message.
5. The rules in §16 (Invariants) are deliberate domain decisions. Do not "simplify" them. If a task seems to require breaking one, stop and ask.
6. Commit per task: `T0x: <summary>`.

---

## 1. Background

The estate is migrating from Azure SQL MI to Amazon RDS for SQL Server. SQL CLR assemblies are being retired and replaced by native T-SQL. About 350+ procedures, functions, views and triggers call the retired CLR objects. The fixes fall into three groups:

| Fix type | Example | Automation |
|---|---|---|
| Name only | `dbo.clr_SplitString(...)` → `dbo.fn_SplitString(...)` | Full |
| Name + signature | `dbo.clr_RegexIsMatch(a,b)` → `dbo.fn_RegexIsMatch(a,b,0)` | Full (template) |
| Query rewrite | CLR aggregate `dbo.Get_concatenate(x)` → `STRING_AGG(...)` | Full for standard shapes; manual for corner cases |

The reference CLR is `dbo.Get_concatenate`, a user-defined aggregate: C# struct `getConcatenate`, `Format.UserDefined`, `MaxByteSize=-1`, comma delimiter, returns `NVARCHAR(MAX)`. Its replacements already exist in the repo (`demo/prereqs/`):
- `01_ConcatValueList_type.sql` — TVP type `dbo.ConcatValueList (Val NVARCHAR(MAX))`
- `02_fn_Get_Concatenate.sql` — wrapper `dbo.fn_Get_Concatenate(@Values ConcatValueList READONLY, @Delimiter NVARCHAR(10) = ',')` using `STRING_AGG`

Inline `STRING_AGG` is the preferred rewrite at call sites. The TVP wrapper is the fallback for contexts where an inline aggregate is impossible.

## 2. Goals and non-goals

**Goals**
- G1 — Phase 1: inventory only the CLR objects given as input, across one or more MI databases, using Microsoft Entra service principal authentication. Extract dependent object definitions to an output folder and suggest a conversion per call site.
- G2 — Phase 2: apply config-driven replacements to the extracted definitions, validate them, and produce an audit trail and an RDS-ready deploy bundle.
- G3 — Every change is traceable (who, when, which rule, before/after, hashes) and reproducible from config + inventory.
- G4 — Zero silent failures. Anything not provably safe is routed to `MANUAL_REVIEW` with a TODO marker.

**Non-goals**
- Deploying to RDS. The tool only produces bundles; deployment belongs to the release pipeline.
- Converting application code, SSIS, SSRS, Agent jobs or ad hoc scripts.
- A full T-SQL parser. A loss-less tokenizer plus contextual heuristics is the chosen design (ScriptDom is .NET-only). Wherever a heuristic is unsure, the site must go to manual review.
- Data parity testing between MI and RDS (future Phase 3, §17).

## 3. Environment and constraints

| Area | Requirement |
|---|---|
| Python | 3.10+ (dataclass `slots=True` is used) |
| Driver | Microsoft ODBC Driver 18 for SQL Server |
| Packages | `pyodbc>=5`, `azure-identity>=1.15`, `PyYAML>=6`; `boto3` optional (Secrets Manager); `pytest` (dev) |
| OS | Windows (primary, VS Code) and Linux. Paths must be Windows-safe (§11.3). |
| MI endpoint | Public `<mi>.public.<dns-zone>.database.windows.net,3342` or private `<mi>.<dns-zone>.database.windows.net,1433` |
| Source auth | Entra service principal only (secret, certificate, or ODBC-native) |
| Target auth | RDS has no Entra: SQL login via environment variables or AWS Secrets Manager |
| Target feature floor | STRING_AGG needs SQL Server 2017+ (major 14) **and** DB compat ≥ 140. `WITHIN GROUP` has the same floor. CREATE OR ALTER needs 2016 SP1+. |
| Source DB permissions | Read-only: `VIEW DEFINITION` per database (§12.2) |

## 4. Current state (hand-off baseline)

### 4.1 Implemented

| File | Status | Notes |
|---|---|---|
| `clr_migrator/tsql_lexer.py` | Done | Loss-less tokenizer; nested comments, `N''`, `[]]`, QUOTED_IDENTIFIER-aware `"..."`; `paren_structure`, `case_blocks` |
| `clr_migrator/analyzer.py` | Done | Multi-part name parsing, target/synonym matching, call-site context, classification, suggestions |
| `clr_migrator/rewriter.py` | Done | Span edits, nested rendering, zero-width inserts, header fixes, STRING_AGG / template rendering |
| `clr_migrator/validators.py` | Done | Static V01–V08/V10 checks, RDS lint, LiveValidator (V09/V11) — **live path untested** |
| `clr_migrator/config.py` | Done | Loader, defaults, validation |
| `clr_migrator/db.py` | Done | Entra token / ODBC-native source; SQL / Secrets Manager target — **untested against real servers** |
| `clr_migrator/common.py`, `reporting.py` | Done | Helpers; CSV (utf-8-sig), JSON, self-contained HTML |
| `phase1_inventory.py` | Done | Live collection **untested**; `--reanalyze` offline path verified |
| `phase2_convert.py` | Done | Verified on demo |
| `demo/` | Done | Fabricated Phase 1 run with 10 corner-case objects, demo config, prereq scripts |

### 4.2 Verified demo results (baseline for the golden test)

| Object | Scenario | Expected status |
|---|---|---|
| dbo.USP_GET_QUALIFIED_BUNDLES | GROUP BY; CASE…END before the call; CLR scalar nested inside the aggregate arg; grouped subquery | READY_WITH_WARNINGS (V06) |
| dbo.usp_BundleSummary | Scalar aggregate (auto) + `DISTINCT` arg (manual) | MANUAL_REVIEW |
| dbo.usp_DynamicReport | CLR name inside dynamic SQL string | MANUAL_REVIEW |
| dbo.vw_ContractCombos | Call via synonym `dbo.GetConcat`; header without schema | READY_WITH_WARNINGS (V06, V07) |
| dbo.fn_Get_Concatenate | Comment-only mention | NO_ACTION |
| dbo.usp_WindowAgg | Aggregate with `OVER (PARTITION BY …)` | MANUAL_REVIEW |
| dbo.usp_SplitAndMatch | TVF rename + template (ok) + wrong-arity call | MANUAL_REVIEW |
| dbo.usp_Renamed | Stale header `usp_OldName` after sp_rename | READY_WITH_WARNINGS (V06, V07) |
| dbo.fn_NeedsRdsReview | Cross-DB three-part name | READY_WITH_WARNINGS (V05, V06) |
| dbo.usp_Encrypted | Encrypted, no definition | MANUAL_REVIEW |

Deploy order produced: `fn_NeedsRdsReview` → `vw_ContractCombos` → `USP_GET_QUALIFIED_BUNDLES` → `usp_Renamed`.

### 4.3 Known gaps (addressed by the backlog)
- No automated tests (T02, T03).
- No `README.md`, `config.example.yaml` or packaging (T01, T12).
- Synonyms and table-level dependencies are reported but no scripts are generated for them (T05, T06).
- V08 "replacement present" uses substring matching; it should parse the prerequisite DDL (T07).
- Phase 1 live DB path and the Phase 2 live-validate path have never run against real servers (T08, T09).
- No connection self-test (T08).

## 5. Architecture

```
config.yaml ──┐
              ▼
      phase1_inventory.py ──(Entra SP)──► Azure SQL MI (read-only catalog queries)
              │
              ▼
 output/<project>_<ts>/                     ◄── Phase 1 run folder (immutable after creation)
   definitions/<db>/<type>/<schema>.<name>.sql
   inventory/inventory.json + CSV + HTML
              │
              ▼
      phase2_convert.py ──(optional, SQL auth)──► RDS (compile in txn, ROLLBACK)
              │
              ▼
 output/<project>_<ts>/conversion_<ts>/      ◄── one folder per Phase 2 run
   converted/ manual_review/ diffs/ deploy/ audit/ validation/
```

| Module | Responsibility | Depends on |
|---|---|---|
| `tsql_lexer` | Tokens with offsets/lines; paren depth/match; CASE/END pairing | — |
| `analyzer` | Find sites for targets and synonyms; context; class; suggestion | lexer |
| `rewriter` | Build edits from sites; apply by offset; nested render; audit entries | analyzer |
| `validators` | Static checks, RDS lint, live compile | analyzer, db |
| `config` | YAML load/validate/defaults; `split_name` | — |
| `db` | Source/target connections; `fetch` | pyodbc, azure-identity, boto3 |
| `common` | Hashing, logging, filenames, `build_targets`, `mapping_for`, read/write preserving newlines | analyzer, config |
| `reporting` | CSV/JSON/HTML | — |

**Design principle:** Phase 2 re-analyzes definitions with the *current* config. Changing a mapping never requires re-running Phase 1. The Phase 1 folder is read-only input to Phase 2.

## 6. Configuration specification

One YAML file drives both phases. Secrets **never** appear in it — only the *names* of environment variables or secret ids.

| Key | Type | Default | Used by | Description |
|---|---|---|---|---|
| `project` | str | `clr-retirement` | P1 | Prefix of run folder name |
| `output_root` | path | `./output` | P1 | Relative to the config file's folder |
| `source.server` | str | required | P1 | MI host (public or private endpoint) |
| `source.port` | int | 1433 | P1 | 3342 for the MI public endpoint |
| `source.databases` | list | required | P1 | Databases to inventory |
| `source.driver` | str | ODBC Driver 18 for SQL Server | P1 | |
| `source.encrypt` / `trust_server_certificate` / `login_timeout` | bool/bool/int | true/false/30 | P1 | |
| `source.auth.method` | enum | `service_principal_secret` | P1 | `service_principal_secret` \| `service_principal_certificate` \| `odbc_native` |
| `source.auth.tenant_id_env` / `client_id_env` / `client_secret_env` | env-var names | — | P1 | e.g. `AZURE_TENANT_ID` … |
| `source.auth.certificate_path_env` / `certificate_password_env` | env-var names | — | P1 | Certificate method |
| `target.platform` | str | `aws_rds_sqlserver` | P2 | Informational |
| `target.engine_major_version` | int | 15 | P2 | 14=2017, 15=2019, 16=2022; drives V04 |
| `target.compat_level` | int | 150 | P2 | Drives V04 |
| `target.database_map` | map | `{}` | P2 | Source DB → target DB name for `USE` in the bundle |
| `target.required_types` | list | `[]` | P2 | Types the preflight must find, e.g. `dbo.ConcatValueList` |
| `target.live_validation.enabled` | bool | false | P2 | Same as `--live-validate` |
| `target.live_validation.server/port/database/driver/encrypt/trust_server_certificate` | | | P2 | RDS endpoint |
| `target.live_validation.auth.method` | enum | `sql_password_env` | P2 | `sql_password_env` \| `aws_secrets_manager` |
| `…auth.username_env` / `password_env` | env-var names | | P2 | |
| `…auth.secret_id` / `region` | str | | P2 | RDS-managed secret JSON (`username`, `password`) |
| `prerequisite_scripts` | list[path] | `[]` | P2 | Deployed first in every bundle, in the listed order |
| `options.case_sensitive` | bool | false | both | Name matching |
| `options.include_object_types` | list | `[P, FN, IF, TF, V, TR]` | P1 | |
| `options.exclude_schemas` / `exclude_objects` | list | `[]` | P1 | |
| `options.create_or_alter` | bool | true | P2 | Rewrite `CREATE` → `CREATE OR ALTER` |
| `clr_objects[]` | list | | both | Default inventory scope + conversion mappings |

**`clr_objects[]` entry**

| Key | Applies to | Description |
|---|---|---|
| `name` | all | `schema.name` of the retired CLR (required, unique) |
| `strategy` | all | `rename` \| `template` \| `string_agg` \| `manual` (default `manual`) |
| `replacement_object` | rename (required), others (guidance) | e.g. `dbo.fn_SplitString` |
| `call_template` | template (required) | Placeholders `{0}`, `{1}` … and `{args}` (all args, comma-joined) |
| `expected_arg_count` | template | Default = CLR parameter count from the catalog |
| `tvp_type` | string_agg (guidance) | Named in fallback suggestions |
| `string_agg.delimiter` | string_agg | Default `,` |
| `string_agg.delimiter_from_arg` | string_agg | Arg index used as separator (must be a literal or variable at the call site) |
| `string_agg.cast_to_nvarchar_max` | string_agg | Default true (see INV-2) |
| `string_agg.empty_result` | string_agg | `empty_string` (default, CLR parity) \| `null` |
| `string_agg.within_group_order_by` | string_agg | e.g. `"{0}"` or `"{0} DESC"`; null = unordered |

Validation rules (already implemented; keep them): duplicate names rejected; rename requires `replacement_object`; template requires `call_template`; enum values checked; `source.databases` must be a list.

## 7. Phase 1 — functional requirements

| ID | Requirement |
|---|---|
| FR-1.1 | Scope = `--clr` (repeatable) ∪ `--clr-file` (one per line, `#` comments). If neither is given, scope = `clr_objects[].name`. De-duplicated case-insensitively. Anything outside the scope is never inventoried. |
| FR-1.2 | For each scope entry and database, resolve via `OBJECT_ID`: type, assembly, class, method, permission set, parameters (signature string, param count). Not found → recorded `found: false` + WARN log. Found but not CLR → inventoried with WARN. |
| FR-1.3 | Resolve synonyms whose `base_object_name` points at the target (same DB or no DB part). Treat them as aliases for matching. |
| FR-1.4 | Discovery = union of (a) `sys.sql_expression_dependencies` by referenced_id, (b) its name fallback for unresolved references, (c) `UPPER(definition) LIKE` text scan of `sys.sql_modules` (with `%`, `_`, `[` escaped). Run for the target and for every synonym. Record `discovered_by` per object. |
| FR-1.5 | Extract each discovered module with `ansi_nulls`, `quoted_identifier`, `is_schema_bound`, `modify_date`, and encryption state (module exists but definition NULL). Apply the include/exclude filters. |
| FR-1.6 | Write each definition **byte-exact** (UTF-8, `newline=''`) to `definitions/<db>/<type>/<schema>.<name>.sql`. Store its SHA-256 in `inventory.json`. On case-insensitive filename collisions, append `__<object_id>`. |
| FR-1.7 | Non-module dependencies: computed columns, CHECK and DEFAULT constraints (text scan, then lexer-confirmed) and synonyms. Report them in `non_module_dependencies.csv` with suggestions. |
| FR-1.8 | Analyze every definition with the shared analyzer (§8). Produce one row per site with context, class, suggestion and rewrite preview. |
| FR-1.9 | Object overall class = highest-rank site class (D > C > V > B > A > N). `V-VERIFY` when the catalog reports a dependency but the lexer finds no actionable site. Encrypted → `C-ENCRYPTED`. Complexity: C/D = HIGH, V/B = MEDIUM, A = LOW, N = NONE. SCHEMABINDING adds a note. |
| FR-1.10 | `--reanalyze RUN_DIR` regenerates all analysis outputs from `inventory.json` + definitions with no DB connection. |
| FR-1.11 | Outputs per §11.1, including an HTML report sorted hardest-first. |
| FR-1.12 | Catalog queries are read-only: no temp tables, no writes, and no `sys.dm_*` requiring VIEW SERVER STATE. |

## 8. Call-site analysis and classification

### 8.1 Matching rules
- Names are matched as multi-part identifiers (`[a].[b]`, `"a"."b"`, `a . b`, `db..b`) outside strings and comments.
- Empty schema (`db..x`) defaults to `dbo`.
- Unqualified names are accepted only for CLR procedures after `EXEC`/`EXECUTE` (including `EXEC @rc = name`) and for TVFs followed by `(`. Scalar functions and aggregates must be schema-qualified in T-SQL, so an unqualified hit for those is never a call.
- String literals and comments are scanned by regex for target/synonym names → sites of kind `dynamic_sql` / `comment`.

### 8.2 Context captured per site

| Field | Meaning |
|---|---|
| `kind` | `call` (followed by `(`), `reference`, `dynamic_sql`, `comment` |
| `args`, `arg_spans` | Top-level comma split, offsets into the original text |
| `distinct` | First token inside the parentheses is `DISTINCT` |
| `over` | Token after `)` is `OVER` |
| `select_scope`, `clause` | Owning SELECT found by walking back at the same paren depth; stops at statement keywords, skipping CASE-owned END/ELSE |
| `group_by` | Forward scan from the owning SELECT at the same depth finds `GROUP BY` before the block ends |
| `nested_in` | Name of the enclosing function, if any |
| `db_part`, `server_part` | Three- and four-part names |

### 8.3 Classes

| Class | Trigger | Auto | Phase 2 action |
|---|---|---|---|
| A-RENAME | strategy rename; arity ≤ param count | Yes | Replace the name span only |
| A-SIGNATURE | strategy template; arity = expected; placeholders satisfied | Yes | Replace the whole call with the rendered template |
| B-STRING_AGG-GROUPED | string_agg; call in SELECT block with GROUP BY | Yes | Inline STRING_AGG (§9.2) |
| B-STRING_AGG-SCALAR | string_agg; call in SELECT block without GROUP BY | Yes | Inline STRING_AGG |
| C-DISTINCT | `DISTINCT` argument | No | TODO: de-duplicate in a derived table |
| C-WINDOW | `OVER (…)` | No | TODO: OUTER APPLY / pre-aggregate |
| C-CONTEXT | Aggregate outside a SELECT block, template on a non-call, malformed call | No | TODO: TVP wrapper pattern |
| C-ARITY | Argument count mismatch | No | TODO |
| C-DELIMITER | `delimiter_from_arg` is not a literal or variable | No | TODO |
| C-CROSSDB | DB part ≠ current DB, or a four-part name | No | TODO |
| C-NO-MAPPING | strategy manual | No | TODO |
| D-DYNAMIC-SQL | Name inside a string literal | No | TODO inserted before the literal |
| N-COMMENT | Name inside a comment | — | None |
| V-VERIFY (object) | Catalog dependency, no actionable site | — | Human inspection |
| C-ENCRYPTED (object) | No definition | — | Get source from source control |

A same-database three-part name (`SAMPLEDB.dbo.x` while inventorying SAMPLEDB) is auto-converted; the DB qualifier is dropped and noted in the audit.

## 9. Phase 2 — functional requirements

### 9.1 Processing

| ID | Requirement |
|---|---|
| FR-2.1 | Inputs: `--config`, `--inventory RUN_DIR`, optional `--database`, `--live-validate`. The output folder is `RUN_DIR/conversion_<UTC ts>/`. Never modify the Phase 1 folder's existing files. |
| FR-2.2 | Load prerequisite scripts; a missing file is exit code 2. |
| FR-2.3 | Per object: hash check (V03), analyze, build edits, apply, re-analyze the result, validate, assign a status, write outputs. |
| FR-2.4 | Edits are span-based on the original text. Text outside edit spans must be byte-identical (including CRLF). |
| FR-2.5 | Nested sites render inner-first. Arguments of an outer call are rebuilt from the original text with inner edits applied. |
| FR-2.6 | An edit overlapping another without containment → object FAILED (OverlapError). No partial output. |
| FR-2.7 | Header: `CREATE` → `CREATE OR ALTER` (if enabled and not already). If the header name ≠ catalog name or lacks a schema, replace it with `[schema].[name]` from the catalog (V07). |
| FR-2.8 | Non-auto sites (except N) get a zero-width insert immediately before the site: `/* CLR-MIGRATION TODO [<class>] <clr>: <short suggestion> */ `. `*/` and `/*` inside the suggestion are neutralized. |
| FR-2.9 | Any object with at least one manual site → `MANUAL_REVIEW` (auto edits still applied; file goes to `manual_review/`). |
| FR-2.10 | Status precedence: MANUAL_REVIEW > FAILED (any FAIL) > READY_WITH_WARNINGS (any WARN) > READY. No actionable sites → NO_ACTION (no output file). Encrypted → MANUAL_REVIEW. |
| FR-2.11 | Output files are wrapped as `SET ANSI_NULLS <orig>; GO / SET QUOTED_IDENTIFIER <orig>; GO / <body> / GO`, using the original newline style. |
| FR-2.12 | A unified diff per converted object, relative to the Phase 1 definition. |
| FR-2.13 | The deploy bundle per DB contains only READY and READY_WITH_WARNINGS objects, in topological order of intra-set references (ties: FN/IF/TF < V < P < TR, then name). Cycles are appended with a V12 WARN. |
| FR-2.14 | Bundle format: comment header (run ids, counts, run instructions), `:on error exit`, `USE [target db]`, `SET NOCOUNT ON`, prerequisites verbatim (terminated with GO), then per object `PRINT` progress + wrapped body. UTF-8 **with BOM**, CRLF. |
| FR-2.15 | With `--live-validate`: preflight (version, compat, required objects/types), then compile every deployable object in its own transaction with the original SET options and ROLLBACK. Failure → status FAILED, removed from the bundle. |
| FR-2.16 | Exit code 1 if any object is FAILED; 2 on config/input errors; else 0. |

### 9.2 Rewrite rendering (exact forms)

| Strategy | Output |
|---|---|
| rename | `<replacement_object>` replaces the name span; args untouched |
| template | `call_template` with `{n}` → arg n (trimmed), `{args}` → all args joined by `, ` |
| string_agg | `ISNULL(STRING_AGG(CAST(<arg0> AS NVARCHAR(MAX)), N'<delim>')[ WITHIN GROUP (ORDER BY <order>)], N'')` |

`string_agg` switches: `cast_to_nvarchar_max: false` drops the CAST; `delimiter_from_arg: k` uses arg k verbatim as the separator; `empty_result: null` drops the outer ISNULL. The delimiter literal escapes `'` as `''`.

Verified example:
```sql
-- before
dbo.Get_concatenate(CASE WHEN dbo.clr_RegexIsMatch(b.code, N'^PR') = 1 THEN b.code END)
-- after
ISNULL(STRING_AGG(CAST(CASE WHEN dbo.fn_RegexIsMatch(b.code, N'^PR', 0) = 1 THEN b.code END AS NVARCHAR(MAX)), N','), N'')
```

## 10. Validation checks

| ID | Check | Severity | Rule |
|---|---|---|---|
| V01 | Residual CLR reference | FAIL / INFO | Any call/reference site left in the output. INFO in manual-review objects. |
| V02 | Lexical integrity | FAIL | Lexer errors or unbalanced parens in the output |
| V03 | Source drift | WARN | Definition SHA-256 ≠ inventory |
| V04 | Target features | FAIL | STRING_AGG used and (engine < 14 or compat < 140); CREATE OR ALTER and engine < 13 |
| V05 | RDS compatibility lint | FAIL/WARN | xp_cmdshell (FAIL); sp_OA*, CREATE ASSEMBLY / EXTERNAL NAME, OPENROWSET/OPENDATASOURCE, BULK INSERT, sp_configure/RECONFIGURE, FILESTREAM/FileTable, TRUSTWORTHY, sp_addlinkedserver, 4-part names, master/msdb refs, cross-DB refs (WARN). Strings/comments masked. |
| V06 | Order parity | WARN | STRING_AGG without WITHIN GROUP |
| V07 | Header name fixed | WARN | Auto-fixed header name |
| V08 | Replacement present | WARN | Replacement object not defined in the prerequisite scripts |
| V09 | Live compile | FAIL/PASS/SKIP | §9.1 FR-2.15 |
| V10 | Dynamic SQL residue | FAIL / INFO | Target name inside a string literal |
| V11 | Target preflight | FAIL/INFO | Version, compat, required objects/types |
| V12 | Dependency cycle | WARN | Bundle ordering |

## 11. Output specification

### 11.1 Phase 1 (`output/<project>_<UTCts>/`)

| Path | Content |
|---|---|
| `definitions/<db>/<type>/<schema>.<name>.sql` | Byte-exact definitions |
| `inventory/inventory.json` | Machine contract for Phase 2 (§11.4) |
| `inventory/clr_targets.csv` | database, clr_object, found, type, type_desc, signature, assembly, assembly_class, synonyms, strategy, replacement, impacted_objects |
| `inventory/call_sites.csv` | database, object, object_type, clr_object, called_via, line, col, kind, class, auto, clause, group_by, nested_in, arg_count, snippet, suggestion, rewrite_preview |
| `inventory/conversion_plan.csv` | database, object, object_type, overall_class, complexity, total_sites, auto_sites, manual_sites, classes, is_schema_bound, recommended_action, discovered_by, definition_path |
| `inventory/non_module_dependencies.csv` | database, kind, object, item, clr_object, is_persisted, definition, suggestion |
| `inventory/summary.json`, `inventory_report.html` | Counts and report |
| `logs/phase1_<ts>.log` | |

### 11.2 Phase 2 (`<run>/conversion_<UTCts>/`)

| Path | Content |
|---|---|
| `converted/<db>/<type>/*.sql` | Deployable objects |
| `manual_review/<db>/<type>/*.sql` | Partially converted objects with TODO markers; FAILED objects |
| `diffs/<db>/<type>/*.diff` | Unified diffs |
| `deploy/<db>/deploy_bundle.sql` | sqlcmd bundle |
| `deploy/deploy_order.csv` | database, target_database, seq, object, object_type, status, file |
| `audit/audit_log.csv` | conversion_id, inventory_run, timestamp_utc, operator, host, database, object, object_type, rule, edit_kind, clr_object, line, before, after, object_sha256_before, object_sha256_after |
| `validation/validation_findings.csv` | database, object, check_id, severity, message, line |
| `validation/conversion_report.html` | Cards, status table, findings, deploy order, audit trail, "read before deploying" notes |
| `conversion_summary.csv` | database, object, object_type, status, auto_edits, manual_sites, fail, warn, output_path, diff_path, notes |
| `manifest.json` | Versions, config hashes (current and inventory-time), target, prerequisite hashes, status counts |

### 11.3 File conventions
- CSV: UTF-8 with BOM (Excel). SQL definitions: UTF-8, no BOM, original newlines. Bundle: UTF-8 with BOM, CRLF.
- Filenames: replace `<>:"/\|?*` and control chars with `_`; strip trailing dots/spaces.
- Timestamps: UTC, `YYYYMMDDTHHMMSSZ` in paths, ISO-8601 in content.

### 11.4 `inventory.json` contract (do not break; Phase 2 depends on it)
```json
{ "tool_version", "run_id", "created_utc", "config_path", "config_sha256", "source_server",
  "auth_method", "auth_client_id", "runtime": {...}, "clr_scope": [...],
  "databases": { "<db>": {
      "targets": [{ "schema","name","found","object_id","type","type_desc","is_clr","assembly",
                    "assembly_class","assembly_method","permission_set","param_count",
                    "signature","synonyms": [["schema","name"]] }],
      "objects": [{ "object_id","schema","name","type","type_desc","uses_ansi_nulls",
                    "uses_quoted_identifier","is_schema_bound","is_encrypted","modify_date",
                    "discovered_by","targets","definition_path","sha256" }],
      "non_module_dependencies": [{ "kind","schema_name","parent_name","item_name",
                                    "definition","is_persisted","target" }] } } }
```
Additive fields are allowed. Renames or removals require a `tool_version` bump and a Phase 2 compatibility shim.

## 12. Security

### 12.1 Secrets
- SEC-1: Credentials only from environment variables or AWS Secrets Manager. Never from config, CLI args or logs.
- SEC-2: Never log connection strings, tokens, passwords or secret payloads. Logging the client id and username is allowed.
- SEC-3: The ODBC `PWD` value is brace-escaped (`}` → `}}`).
- SEC-4: `Encrypt=yes` by default; `TrustServerCertificate=no` by default. For RDS, import the RDS CA bundle into the OS trust store instead of disabling validation.

### 12.2 Source permissions (run per database on MI; requires an Entra admin on the MI and Directory Readers for the MI identity)
```sql
CREATE USER [sp-clr-inventory] FROM EXTERNAL PROVIDER;
GRANT VIEW DEFINITION TO [sp-clr-inventory];
```
No other permission is needed. The tool issues SELECTs against catalog views only.

### 12.3 Target (live validation only)
A SQL login with `db_ddladmin` (or `db_owner`) on a **non-production** RDS database. The compile check takes brief schema-modification locks on existing objects.

## 13. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | 500 objects / 20 MB of definitions: Phase 2 under 60 s on a laptop; Phase 1 dominated by catalog queries (one text scan per target/alias) |
| NFR-2 | Deterministic: same inputs + config → identical converted files, diffs and bundle (except timestamps/ids in headers and paths) |
| NFR-3 | Idempotent: running Phase 2 on its own converted output yields no further auto edits (no CLR names left) |
| NFR-4 | No third-party SQL parser dependency |
| NFR-5 | Clear errors: config errors name the key; DB errors name the database and step |
| NFR-6 | Access tokens are acquired per database connection (tokens last ~60–90 min) |
| NFR-7 | Type hints on public functions; ruff-clean; no bare `except` except where commented |

## 14. Testing and acceptance

### 14.1 Unit tests (`tests/`, pytest)

| Area | Minimum cases |
|---|---|
| Lexer | nested `/* /* */ */`; `N'it''s'`; `[a]]b]`; `"x"` with QI ON (ident) and OFF (string); unterminated string/comment reports an error; `--` comment at EOF without newline; token offsets reconstruct the input exactly |
| Analyzer | Each class in §8.3 has at least one positive test; CASE…END before a call does not break scope; `WITH (NOLOCK)` does not stop the GROUP BY scan; `UNION` branch GROUP BY is not attributed across; unqualified scalar ≠ match; `EXEC clrproc` unqualified = match; synonym match; three-part same-DB auto, other-DB C-CROSSDB; four-part C-CROSSDB |
| Rewriter | CRLF preserved outside spans; nested template-inside-string_agg; zero-width TODO inside an auto call's arg; header OR ALTER insert; header name fix with `]` escaping; overlap raises; `delimiter_from_arg`; `within_group_order_by` placeholder; `empty_result: null`; quote in delimiter escaped |
| Validators | V04 with engine 13/14, compat 130/140; V05 each rule fires and is masked inside strings/comments; V06 count; V01 INFO vs FAIL paths |
| Config | Each validation error message; defaults applied |

### 14.2 Golden test (T03)
`tests/test_demo_golden.py` builds the demo, runs both phases in a temp dir via `subprocess`, and asserts:
- statuses exactly as in §4.2
- converted/manual SQL bodies equal `tests/golden/**` (strip the timestamp lines in the bundle header)
- `deploy_order.csv` order as in §4.2
- audit row count stable
- Phase 2 exit code 0

### 14.3 Acceptance criteria (sign-off)

| # | Criterion |
|---|---|
| AC-1 | Phase 1 against a real MI with SP auth inventories only the given CLRs; every object in `conversion_plan.csv` opens from `definitions/` and its hash matches |
| AC-2 | Spot-check 20 objects across classes: classification matches manual judgment in ≥ 19; any mismatch errs toward manual (never a false AUTO) |
| AC-3 | Every READY object's diff touches only CLR call sites and the header |
| AC-4 | The deploy bundle runs clean on a non-prod RDS (`sqlcmd -b`) after the prerequisites |
| AC-5 | `--live-validate` reports PASS for every READY object on the same RDS |
| AC-6 | Audit log reconciles: the sum of edits per object equals the diff hunks' changed call sites |
| AC-7 | `pytest -q` green; golden test green |

## 15. Backlog for Claude Code (in order)

| Task | Scope | Files | DoD |
|---|---|---|---|
| **T01** Packaging | `requirements.txt` (runtime), `requirements-dev.txt` (pytest, ruff), `pyproject.toml` (ruff config, py310), `.gitignore` (`output/`, `demo/output/`, `__pycache__`, `.venv`), `config.example.yaml` fully commented per §6 with the three SAMPLEDB mappings | root | `pip install -r requirements.txt` works; config.example loads with `load_config` |
| **T02** Unit tests | All §14.1 cases | `tests/test_lexer.py`, `test_analyzer.py`, `test_rewriter.py`, `test_validators.py`, `test_config.py` | `pytest -q` green; no production code change unless a test exposes a real bug (document it) |
| **T03** Golden test | §14.2 | `tests/test_demo_golden.py`, `tests/golden/` | Green; regenerating goldens is a documented script flag |
| **T04** Phase 1 self-test | `phase1_inventory.py --self-test`: connect per DB, print `SUSER_SNAME()`, `DB_NAME()`, `@@VERSION`, `HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','VIEW DEFINITION')`; exit non-zero on failure | phase1 | Fails fast with an actionable message when the SP user or grant is missing |
| **T05** Synonym scripts | Phase 2 writes `deploy/<db>/synonyms.sql`: for rename/template targets `DROP SYNONYM` + `CREATE SYNONYM … FOR <replacement>`; for string_agg targets `DROP SYNONYM` (only after all callers are converted). Emitted *after* objects, commented out by default with a header explaining why | phase2, new `clr_migrator/scripts.py` | Demo produces a GetConcat drop script; not included in `deploy_bundle.sql` |
| **T06** Table-level dependency scripts | For computed columns/constraints, generate `manual_review/<db>/TABLE_CHANGES.sql` templates: DROP/ADD with the rewritten expression (reuse the rewriter on `definition`), a warning for PERSISTED/indexed columns (needs a deterministic, SCHEMABINDING function), `WITH CHECK` for constraints | phase2, scripts.py | Demo computed column produces a template using `dbo.fn_RegexIsMatch(...,0)` |
| **T07** V08 via DDL parsing | Split prereq scripts on `GO` lines (regex `^\s*GO\s*(\d+)?\s*$`, multiline, outside strings), parse `CREATE [OR ALTER] FUNCTION/PROCEDURE/VIEW/TYPE <name>` with the lexer; V08 compares normalized names | validators | Tests: present, absent, commented-out definition does not count |
| **T08** Live-path hardening (source) | Run Phase 1 against a dev MI; fix issues found. Add retry (3x, backoff) for transient errors 40613/40197/40501/49918/4060 and token acquisition | db, phase1 | AC-1 met on dev |
| **T09** Live-path hardening (target) | Run `--live-validate` against a dev RDS; add `--preflight-only`; ensure rollback in `finally` even after a broken connection (reconnect) | validators, phase2 | AC-5 met on dev |
| **T10** Excel workbook (optional) | `--xlsx` flag writes `inventory.xlsx` / `conversion.xlsx` (one sheet per CSV, frozen header, autofilter) with openpyxl as an optional dependency | reporting | Opens in Excel; absent openpyxl → clear message |
| **T11** CLI polish | `--version`; `--log-level`; consistent exit codes; `--database` validation against the config | phase1, phase2 | `--help` accurate |
| **T12** README | Setup, SP creation, env vars, run order, output legend (classes, statuses, checks), deploy steps, limitations (§17), troubleshooting (login failures, cert trust, compat level) | README.md | A new engineer can run the demo from the README alone |

## 16. Invariants — do not change without owner approval

| ID | Rule | Why |
|---|---|---|
| INV-1 | Keep `ISNULL(STRING_AGG(...), N'')` even when the call is already inside ISNULL/COALESCE | CLR returned `''` on empty input; an outer `ISNULL(x,'none')` would change the result if the inner wrapper were removed |
| INV-2 | Keep `CAST(<arg> AS NVARCHAR(MAX))` by default | STRING_AGG over non-MAX input is capped at 8000 bytes and raises an error; the CLR returned NVARCHAR(MAX) |
| INV-3 | Never auto-add `WITHIN GROUP` | CLR UDA order was never guaranteed (`IsInvariantToOrder` is reserved and not honoured by the engine). Ordering is a business decision set via config |
| INV-4 | Unsure → manual. Any heuristic doubt produces a C-class site, never an AUTO | False AUTO is the only unacceptable error |
| INV-5 | Edits by offset against the original; no reformatting or whitespace normalization | Reviewable diffs; CRLF fidelity |
| INV-6 | Phase 1 outputs are immutable inputs to Phase 2 | Audit lineage |
| INV-7 | No DB writes except the rolled-back live compile | Tool is non-deploying by design |
| INV-8 | Scalar/aggregate names must be schema-qualified to match | T-SQL resolution rules; avoids column-name false positives |
| INV-9 | `inventory.json` schema is a contract (§11.4) | Cross-phase compatibility |

## 17. Limitations and future work
- Dynamic SQL is only detected, never rewritten. Runtime-generated SQL can be missed entirely if the name is assembled from fragments (e.g. `'Get_' + 'concatenate'`).
- DDL triggers (database scope) are not in `sys.objects` and are excluded.
- Live compile rolls back per object, so each object compiles against the target's *existing* state, not against other converted objects. Deferred name resolution means procedures referencing missing tables still compile.
- Prerequisite scripts containing `USE [SAMPLEDB]` will switch database even when `database_map` renames the target. Keep prerequisite scripts free of `USE`, or align the names.
- **Behavioral differences to test in Phase 3 (data parity):**
  - **NULLs:** STRING_AGG skips NULLs. The CLR `Accumulate` reads `Value.Value` *before* its `IsNull` check, so a NULL reaching it throws.
  - **Empty input:** CLR returns `''`, STRING_AGG returns NULL. This is handled by INV-1. Note that `dbo.fn_Get_Concatenate` as currently written returns NULL for an empty TVP.
  - **Order:** see INV-3.
- **Phase 3 (future):** a parity harness that executes selected functions/views/procedures with captured parameters on MI and RDS and compares result sets (EXCEPT both ways, hash per row).

## 18. Glossary
| Term | Meaning |
|---|---|
| Site | One occurrence of a target CLR (or synonym) in a definition |
| Target | A CLR object in the inventory scope |
| Auto site | A site Phase 2 rewrites without human input |
| Run folder | The Phase 1 output folder; Phase 2 writes `conversion_*` subfolders inside it |
| UDA | User-defined aggregate |
| TVP | Table-valued parameter |
