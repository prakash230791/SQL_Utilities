# SQL_Utilities — CLR Retirement Toolkit

Inventories SQL Server CLR call sites (Phase 1) and rewrites them to native T-SQL (Phase 2), for
a migration from Azure SQL Managed Instance to Amazon RDS for SQL Server. Full spec:
[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md).

Nothing is ever deployed by the tool itself. Phase 2 writes converted SQL files and a
`sqlcmd`-ready deploy bundle for a human to run; the only database action the tool ever takes is
an optional compile check on the target, inside a transaction that is always rolled back.

## Install

```bash
pip install -r requirements.txt
```

## Two ways to feed Phase 1

Phase 2 is identical either way — it only ever reads `schema`/`name`/`type`/`definition_path`/
`sha256`/`uses_ansi_nulls`/`uses_quoted_identifier` off the run folder's `inventory.json` and
re-analyzes the SQL itself. The two modes only differ in how that run folder gets built.

| | **Live** (`phase1_inventory.py --config ...`) | **Offline folder** (`--from-folder`) |
|---|---|---|
| Finding impacted procs | Automatic — scans the catalog (`sys.sql_expression_dependencies` + name fallback + a text scan of `sys.sql_modules`), unioned for recall | Manual — you point it at a folder; only what's there gets processed |
| CLR type / arity / synonyms | Read from the catalog | You supply `source_type` / `source_param_count` / `source_synonyms` in config |
| Renamed objects (`sp_rename`) | Detected and fixed (stale header vs. catalog name) | Not detectable — the file's own `CREATE` header name is trusted as current |
| Computed columns / constraints / encrypted objects | Found and reported | Not representable as files — skipped |
| Needs | A live Azure SQL MI connection (Entra service principal) | Nothing — fully offline |

Use live mode when you want the tool to find every impacted object for you. Use `--from-folder`
when you already know which procs are impacted (pulled from source control, handed to you as
files, etc.) and just want the mechanical rewrite + deploy bundle, air-gapped.

---

## Offline folder mode — worked example

This is a complete, runnable demo: three real objects that call a retired CLR scalar function,
rewritten to call its native T-SQL replacement, entirely offline. The commands below are exactly
what was run to produce the output shown — copy-paste them to reproduce it yourself.

### The scenario

- **Retired CLR**: `dbo.clr_ToTitleCase(@s NVARCHAR(MAX))` — a CLR scalar function
- **Replacement**: `dbo.fn_ToTitleCase`, a native T-SQL function with the same signature
  (`demo/offline_folder_sample/wrapper/01_fn_ToTitleCase.sql`)
- **Impacted procs** (`demo/offline_folder_sample/impacted_procs/`), scripted out of SSMS in the
  ordinary way — `SET ANSI_NULLS ON; GO SET QUOTED_IDENTIFIER ON; GO` ahead of the `CREATE`, CRLF
  line endings, a trailing `GO`:
  - `dbo.usp_GetCustomerDisplayName.sql` — one call site
  - `dbo.usp_FormatAddressLabel.sql` — three call sites in one statement
  - `dbo.vw_CustomerSummary.sql` — a view, two call sites

### The config (`demo/offline_folder_sample/config.sample.yaml`)

```yaml
project: OFFLINE_SAMPLE
output_root: ./output
source:
  server: not-used-in-from-folder-mode
  databases: [RETAILDB]
  auth: {method: service_principal_secret, tenant_id_env: AZURE_TENANT_ID, client_id_env: AZURE_CLIENT_ID, client_secret_env: AZURE_CLIENT_SECRET}
target:
  platform: aws_rds_sqlserver
  engine_major_version: 15
  compat_level: 150
prerequisite_scripts:
  - wrapper/01_fn_ToTitleCase.sql
clr_objects:
  - name: dbo.clr_ToTitleCase
    strategy: rename
    replacement_object: dbo.fn_ToTitleCase
    source_type: FS              # offline-only: CLR type the catalog would otherwise supply
    source_param_count: 1        # offline-only: arity, ditto
```

`source.*` is required by config validation but unused in `--from-folder` mode (no connection is
made). `source_type`/`source_param_count` are the offline stand-ins for what a live scan reads
off `sys.objects`/`sys.parameters` — see [Config reference](#config-reference).

### Run it

```bash
python phase1_inventory.py --config demo/offline_folder_sample/config.sample.yaml \
    --from-folder demo/offline_folder_sample/impacted_procs \
    --clr dbo.clr_ToTitleCase --database RETAILDB
```

```
INFO    Run OFFLINE_SAMPLE_<ts> | CLR scope: dbo.clr_ToTitleCase | folder: demo/offline_folder_sample/impacted_procs
INFO    [RETAILDB] 3 object(s) ingested from folder
INFO    Folder inventory complete -> demo/offline_folder_sample/output/OFFLINE_SAMPLE_<ts>
INFO    Objects: 3 | actionable sites: 6 | auto-convertible: 6 | by complexity: {"LOW": 3}
```

```bash
python phase2_convert.py --config demo/offline_folder_sample/config.sample.yaml \
    --inventory demo/offline_folder_sample/output/OFFLINE_SAMPLE_<ts>
```

```
INFO    [RETAILDB] dbo.clr_ToTitleCase -> strategy rename
INFO    [RETAILDB] dbo.usp_FormatAddressLabel                                   READY
INFO    [RETAILDB] dbo.usp_GetCustomerDisplayName                               READY
INFO    [RETAILDB] dbo.vw_CustomerSummary                                       READY
INFO    Done -> .../conversion_<ts>
INFO    Status: {'READY': 3} | edits audited: 9
```

All three go straight to `READY` — no warnings, no manual sites: a pure rename with matching
arity needs no human judgment (contrast with the corner-case demo below, where `STRING_AGG`
rewrites and ambiguous shapes land in `MANUAL_REVIEW`/`READY_WITH_WARNINGS`).

### What actually changed

`conversion_<ts>/diffs/RETAILDB/P/dbo.usp_FormatAddressLabel.diff` — all three call sites in one
statement renamed, plus the `CREATE` → `CREATE OR ALTER` upgrade:

```diff
-CREATE PROCEDURE dbo.usp_FormatAddressLabel
+CREATE OR ALTER PROCEDURE dbo.usp_FormatAddressLabel
     @AddressId INT
 AS
 BEGIN
     SET NOCOUNT ON;
     SELECT  a.address_id,
-            dbo.clr_ToTitleCase(a.city) AS city_display,
-            dbo.clr_ToTitleCase(a.street_line1) + N', ' + dbo.clr_ToTitleCase(a.city) AS full_label
+            dbo.fn_ToTitleCase(a.city) AS city_display,
+            dbo.fn_ToTitleCase(a.street_line1) + N', ' + dbo.fn_ToTitleCase(a.city) AS full_label
     FROM    dbo.addresses a
     WHERE   a.address_id = @AddressId;
 END
```

`conversion_<ts>/deploy/RETAILDB/deploy_bundle.sql` is ready to run as-is: the prerequisite
wrapper function first, then the three converted objects in dependency order
(`deploy/deploy_order.csv`), each wrapped in its own `SET ANSI_NULLS`/`SET QUOTED_IDENTIFIER`/`GO`
batch reconstructed from the settings the tool detected — nothing is deployed by the tool itself;
you run this bundle yourself with `sqlcmd -b` or SSMS in SQLCMD mode.

`conversion_<ts>/validation/validation_findings.csv` — every object passes V01 (no residual CLR
reference) and V02 (tokenizes cleanly); `V09 SKIP` just means `--live-validate` wasn't passed (no
target DB configured for this sample).

### The preamble/CRLF handling this proves

The three impacted-proc files start with `SET ANSI_NULLS ON; GO SET QUOTED_IDENTIFIER ON; GO`
before `CREATE` — the normal shape for anything scripted out of SSMS — and use CRLF line endings.
`--from-folder` strips that leading preamble and any trailing `GO` before storing the object's
definition (`demo/offline_folder_sample/output/.../definitions/RETAILDB/P/dbo.usp_FormatAddressLabel.sql`
starts exactly at `CREATE PROCEDURE`), matching the shape `sys.sql_modules.definition` has in a
live scan — the settings aren't lost, they're captured in `uses_ansi_nulls`/`uses_quoted_identifier`
instead, same as a live run. This matters beyond tidiness: Phase 2's `CREATE` → `CREATE OR ALTER`
upgrade and stale-header-name fix both depend on the header being recognized, which only works if
the object's stored definition starts at `CREATE`/`ALTER` — visible above in the diff. CRLF line
endings are preserved byte-for-byte throughout (files are read/written with `newline=''`; never
reformatted).

### Re-running it

```bash
rm -rf demo/offline_folder_sample/output
python phase1_inventory.py --config demo/offline_folder_sample/config.sample.yaml \
    --from-folder demo/offline_folder_sample/impacted_procs \
    --clr dbo.clr_ToTitleCase --database RETAILDB
python phase2_convert.py --config demo/offline_folder_sample/config.sample.yaml \
    --inventory demo/offline_folder_sample/output/OFFLINE_SAMPLE_<ts from the phase1 log line>
```

(`demo/offline_folder_sample/output/` is git-ignored — each run produces a fresh timestamped
folder alongside any previous ones.)

### Known limitations of `--from-folder`, inherent to having no DB connection

- Computed columns, constraints and encrypted objects aren't representable as files — use a live
  Phase 1 run for those.
- An object renamed with `sp_rename` after the file was last extracted is invisible — the file's
  own `CREATE` header name is trusted as current, so the stale-header fix never triggers on it.
- No auto-discovery — if a proc that calls the CLR isn't in the folder, it's simply never seen.

---

## The other offline demo: corner cases (`demo/`)

`demo/build_demo_inventory.py` fabricates a Phase 1 run folder in-process (no files, no DB) that
exercises the harder cases — grouped/ungrouped `STRING_AGG` rewrites, `DISTINCT`, an `OVER()`
window, dynamic SQL, a synonym, a cross-database call, a stale header after `sp_rename`, an
encrypted object — see `docs/REQUIREMENTS.md` §4.2 for the full table. Run it:

```bash
python demo/build_demo_inventory.py
python phase1_inventory.py --config demo/config.demo.yaml --reanalyze demo/output/DEMO_RUN
python phase2_convert.py  --config demo/config.demo.yaml --inventory demo/output/DEMO_RUN
```

Expect `Status: {'READY_WITH_WARNINGS': 4, 'MANUAL_REVIEW': 5, 'NO_ACTION': 1}` with 24 edits
audited — this is the regression baseline; it must not change (`docs/REQUIREMENTS.md` §4.2).

---

## Live mode

Connects to Azure SQL Managed Instance with a Microsoft Entra service principal and, for only the
CLR objects given as input, finds every dependent module across the databases in
`source.databases`, extracts definitions, classifies every call site, and writes the same shape of
run folder `--from-folder` does — but fully populated (real catalog metadata, synonyms, renames,
non-module dependencies).

### Prerequisites

- `ODBC Driver 18 for SQL Server` installed
- A Microsoft Entra service principal with `VIEW DEFINITION` (and `VIEW DATABASE STATE` for some
  catalog views) on each database in scope
- Its tenant id / client id / client secret (or certificate) available as **environment
  variables** — never in the config file:

```bash
export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
```

### Config

```yaml
source:
  server: your-mi.public.<region>.database.windows.net
  port: 3342                       # public MI endpoint; 1433 for a private endpoint
  databases: [YourDatabase]
  auth:
    method: service_principal_secret   # | service_principal_certificate | odbc_native
    tenant_id_env: AZURE_TENANT_ID
    client_id_env: AZURE_CLIENT_ID
    client_secret_env: AZURE_CLIENT_SECRET
```

### Run it

```bash
# Scope from --clr / --clr-file, or fall back to clr_objects[].name in config:
python phase1_inventory.py --config config.yaml \
    --clr dbo.Get_concatenate --clr dbo.clr_RegexIsMatch
# or:
python phase1_inventory.py --config config.yaml --clr-file clr_scope.txt

python phase2_convert.py --config config.yaml --inventory output/<run_id>
```

Tweaking a `clr_objects` mapping afterwards doesn't need a re-scan — Phase 2 re-analyzes the
extracted definitions with whatever config you give it, and `--reanalyze` refreshes Phase 1's own
report the same way:

```bash
python phase1_inventory.py --config config.yaml --reanalyze output/<run_id>   # no DB connection
```

### Optional: live compile check on the target

```bash
python phase2_convert.py --config config.yaml --inventory output/<run_id> --live-validate
```

Compiles every converted object on the target RDS instance **inside a transaction that is always
rolled back** — nothing is committed. Needs `target.live_validation` configured:

```yaml
target:
  live_validation:
    enabled: false            # or pass --live-validate
    server: your-rds-endpoint.rds.amazonaws.com
    database: YourDatabase
    auth:
      method: sql_password_env        # | aws_secrets_manager
      username_env: RDS_USER
      password_env: RDS_PASSWORD
```

RDS doesn't accept Entra tokens, so this side always uses SQL authentication — from environment
variables, or an RDS-managed secret via `aws_secrets_manager` (`secret_id` + `region`, secret JSON
holds `username`/`password`). Either way, no credential is ever written to the config file itself.

---

## Config reference

Full field-by-field table: `docs/REQUIREMENTS.md` §6. The `clr_objects[]` entry:

| Key | Applies to | Description |
|---|---|---|
| `name` | all | `schema.name` of the retired CLR (required, unique) |
| `strategy` | all | `rename` \| `template` \| `string_agg` \| `manual` (default `manual`) |
| `replacement_object` | `rename` (required) | e.g. `dbo.fn_SplitString` |
| `call_template` | `template` (required) | `{0}`, `{1}`, … and `{args}` placeholders |
| `expected_arg_count` | `template` | Default = CLR parameter count |
| `string_agg.*` | `string_agg` | Delimiter, NVARCHAR(MAX) cast, empty-result semantics, ordering — see §6 |
| `source_type` | **offline `--from-folder` only** | CLR type code (`AF`\|`FS`\|`FT`\|`PC`\|`TA`); a live run reads this from the catalog instead |
| `source_param_count` | **offline only** | Parameter count, ditto |
| `source_synonyms` | **offline only** | `[[schema, name], ...]` for any synonym pointing at this CLR — no catalog to discover them from offline |

## Output folder legend

```
<run_id>/
  logs/                          timestamped log file per run
  definitions/<db>/<TYPE>/       extracted (or ingested) object source, verbatim
  inventory/                     inventory.json (contract, §11.4), clr_targets.csv,
                                  call_sites.csv, conversion_plan.csv, inventory_report.html
  conversion_<ts>/
    converted/<db>/<TYPE>/       rewritten objects, ready to review
    manual_review/<db>/<TYPE>/   objects with at least one manual site (TODO markers inserted)
    diffs/<db>/<TYPE>/           unified diffs, original vs. converted
    audit/audit_log.csv          every edit, by rule and character span
    validation/                  validation_findings.csv, conversion_report.html
    deploy/<db>/deploy_bundle.sql, deploy_order.csv   sqlcmd-ready; nothing runs it but you
    manifest.json
```

## Hard rules

See `docs/REQUIREMENTS.md` §16 / `CLAUDE.md`. The short version: never write to a database (the
rolled-back compile check is the sole exception); never reformat SQL outside the identified call
sites; when unsure, classify as manual rather than risk a false auto-conversion; no SQL-parser
dependency.
