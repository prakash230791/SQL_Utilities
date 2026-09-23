#!/usr/bin/env python3
"""Builds a fake Phase 1 run folder (no database needed) seeded with the corner
cases the tool must handle, so both phases can be exercised offline:

  python demo/build_demo_inventory.py
  python phase1_inventory.py --config demo/config.demo.yaml --reanalyze demo/output/DEMO_RUN
  python phase2_convert.py  --config demo/config.demo.yaml --inventory demo/output/DEMO_RUN
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clr_migrator.common import sha256_text, write_text  # noqa: E402

RUN = Path(__file__).resolve().parent / "output" / "DEMO_RUN"
CRLF = "\r\n"

DEFS = {
    ("dbo", "USP_GET_QUALIFIED_BUNDLES", "P"): """
-- Grouped aggregate, CASE...END before the call, nested CLR scalar inside the aggregate arg,
-- and a grouped subquery in the select list.
CREATE PROCEDURE dbo.USP_GET_QUALIFIED_BUNDLES
    @account_id INT
AS
BEGIN
    SET NOCOUNT ON;
    SELECT  b.bundle_id,
            CASE WHEN b.active = 1 THEN 'Y' ELSE 'N' END AS active_flag,
            ISNULL(dbo.Get_concatenate(b.contract_combo_id), '') AS combos,
            dbo.Get_concatenate(CASE WHEN dbo.clr_RegexIsMatch(b.code, N'^PR') = 1 THEN b.code END) AS promo_codes,
            (SELECT dbo.Get_concatenate(x.usoc) FROM dbo.bundle_usoc x
              WHERE x.bundle_id = b.bundle_id GROUP BY x.bundle_id) AS usocs
    FROM    dbo.bundles b WITH (NOLOCK)
    WHERE   b.account_id = @account_id
    GROUP BY b.bundle_id, b.active;
END
""",
    ("dbo", "usp_BundleSummary", "P"): """
CREATE PROCEDURE [dbo].[usp_BundleSummary] @acct INT AS
BEGIN
    DECLARE @list NVARCHAR(MAX);
    -- whole-set aggregation, no GROUP BY
    SELECT @list = [dbo].[Get_concatenate](c.name) FROM dbo.contracts c WHERE c.acct = @acct;
    -- DISTINCT argument: STRING_AGG cannot do this
    SELECT c.acct, dbo.Get_concatenate(DISTINCT c.region) FROM dbo.contracts c GROUP BY c.acct;
    SELECT @list AS list;
END
""",
    ("dbo", "usp_DynamicReport", "P"): """
CREATE PROCEDURE dbo.usp_DynamicReport @tbl SYSNAME AS
BEGIN
    DECLARE @sql NVARCHAR(MAX) = N'SELECT grp, dbo.Get_concatenate(val) FROM ' + QUOTENAME(@tbl) + N' GROUP BY grp';
    EXEC sys.sp_executesql @sql;
END
""",
    ("dbo", "vw_ContractCombos", "V"): """
/* header without schema: tool should pin it to [dbo] */
CREATE VIEW vw_ContractCombos AS
SELECT a.account_id, dbo.GetConcat(a.contract_combo_id) AS combos   -- via synonym
FROM dbo.accounts a
GROUP BY a.account_id
""",
    ("dbo", "fn_Get_Concatenate", "FN"): """
/* Replaces the retired CLR aggregate dbo.Get_concatenate (comment mention only) */
CREATE FUNCTION dbo.fn_Get_Concatenate (@Values dbo.ConcatValueList READONLY, @Delimiter NVARCHAR(10) = ',')
RETURNS NVARCHAR(MAX) AS
BEGIN
    DECLARE @Result NVARCHAR(MAX);
    SELECT @Result = STRING_AGG(Val, @Delimiter) FROM @Values;
    RETURN @Result;
END
""",
    ("dbo", "usp_WindowAgg", "P"): """
CREATE PROCEDURE dbo.usp_WindowAgg AS
SELECT o.order_id, dbo.Get_concatenate(o.sku) OVER (PARTITION BY o.customer_id) AS skus
FROM dbo.orders o;
""",
    ("dbo", "usp_SplitAndMatch", "P"): """
CREATE PROCEDURE dbo.usp_SplitAndMatch @csv NVARCHAR(MAX) AS
BEGIN
    SELECT s.value
    FROM   dbo.clr_SplitString(@csv, N',') AS s
    WHERE  dbo.clr_RegexIsMatch(s.value, N'^[0-9]+$') = 1;
    IF dbo.clr_RegexIsMatch(@csv, N'x', 1) = 1 PRINT 'bad arity';
END
""",
    ("dbo", "usp_Renamed", "P"): """
CREATE PROCEDURE dbo.usp_OldName AS
SELECT t.k, dbo.Get_concatenate(t.v) FROM dbo.t t GROUP BY t.k;
""",
    ("dbo", "fn_NeedsRdsReview", "FN"): """
CREATE FUNCTION dbo.fn_NeedsRdsReview (@p NVARCHAR(100)) RETURNS NVARCHAR(4000) AS
BEGIN
    RETURN (SELECT dbo.Get_concatenate(n.name) FROM OtherDb.dbo.names n WHERE n.p = @p);
END
""",
}

TARGETS = [
    {"schema": "dbo", "name": "Get_concatenate", "found": True, "object_id": 101, "type": "AF",
     "type_desc": "AGGREGATE_FUNCTION", "is_clr": True, "assembly": "getConcatenate",
     "assembly_class": "getConcatenate", "assembly_method": None, "permission_set": "SAFE_ACCESS",
     "param_count": 1, "signature": "(@Value nvarchar(max)) RETURNS nvarchar(max)",
     "synonyms": [["dbo", "GetConcat"]]},
    {"schema": "dbo", "name": "clr_RegexIsMatch", "found": True, "object_id": 102, "type": "FS",
     "type_desc": "CLR_SCALAR_FUNCTION", "is_clr": True, "assembly": "RegexLib",
     "assembly_class": "RegexLib.Funcs", "assembly_method": "IsMatch", "permission_set": "SAFE_ACCESS",
     "param_count": 2, "signature": "(@input nvarchar(max), @pattern nvarchar(4000)) RETURNS bit",
     "synonyms": []},
    {"schema": "dbo", "name": "clr_SplitString", "found": True, "object_id": 103, "type": "FT",
     "type_desc": "CLR_TABLE_VALUED_FUNCTION", "is_clr": True, "assembly": "StringLib",
     "assembly_class": "StringLib.Split", "assembly_method": "Split", "permission_set": "SAFE_ACCESS",
     "param_count": 2, "signature": "(@s nvarchar(max), @d nchar(1))", "synonyms": []},
    {"schema": "dbo", "name": "clr_NotInDb", "found": False},
]


def main():
    objects = []
    for n, ((sch, name, typ), body) in enumerate(DEFS.items(), start=1000):
        text = body.strip("\n").replace("\n", CRLF) + CRLF
        rel = Path("definitions") / "SAMPLEDB" / typ / f"{sch}.{name}.sql"
        write_text(RUN / rel, text)
        objects.append({"object_id": n, "schema": sch, "name": name, "type": typ, "type_desc": typ,
                        "uses_ansi_nulls": True, "uses_quoted_identifier": True,
                        "is_schema_bound": False, "is_encrypted": False, "modify_date": "2026-09-01",
                        "discovered_by": ["expression_dependencies", "sql_modules_text_scan"],
                        "targets": ["dbo.Get_concatenate"], "definition_path": rel.as_posix(),
                        "sha256": sha256_text(text)})
    objects.append({"object_id": 1999, "schema": "dbo", "name": "usp_Encrypted", "type": "P",
                    "type_desc": "SQL_STORED_PROCEDURE", "uses_ansi_nulls": True,
                    "uses_quoted_identifier": True, "is_schema_bound": False, "is_encrypted": True,
                    "modify_date": "2026-01-01", "discovered_by": ["expression_dependencies"],
                    "targets": ["dbo.Get_concatenate"], "definition_path": None, "sha256": None})
    nonmod = [{"kind": "COMPUTED_COLUMN", "schema_name": "dbo", "parent_name": "customers",
               "item_name": "is_valid_email", "definition": "([dbo].[clr_RegexIsMatch]([email],N'^.+@.+$'))",
               "is_persisted": 1, "target": "dbo.clr_RegexIsMatch"},
              {"kind": "SYNONYM", "schema_name": "dbo", "parent_name": "GetConcat", "item_name": "",
               "definition": "[dbo].[Get_concatenate]", "is_persisted": None, "target": "dbo.Get_concatenate"}]
    inv = {"tool_version": "1.0.0", "run_id": "DEMO_RUN", "created_utc": "2026-09-22T00:00:00+00:00",
           "config_sha256": "demo", "source_server": "demo-mi.public.xxxx.database.windows.net",
           "auth_method": "service_principal_secret", "auth_client_id": "00000000-demo",
           "clr_scope": [t["schema"] + "." + t["name"] for t in TARGETS],
           "databases": {"SAMPLEDB": {"targets": TARGETS, "objects": objects, "non_module_dependencies": nonmod}}}
    (RUN / "inventory").mkdir(parents=True, exist_ok=True)
    (RUN / "inventory" / "inventory.json").write_text(json.dumps(inv, indent=2), encoding="utf-8")
    print(f"Demo run written to {RUN}")


if __name__ == "__main__":
    main()
