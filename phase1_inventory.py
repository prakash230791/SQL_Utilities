#!/usr/bin/env python3
"""Phase 1 - CLR impact inventory and conversion suggestions.

Connects to Azure SQL Managed Instance with a Microsoft Entra service
principal, and - for ONLY the CLR objects given as input - finds every
dependent module, extracts its definition to the output folder, classifies
every call site and suggests how to convert it.

Discovery uses three independent signals, unioned for recall:
  1. sys.sql_expression_dependencies (resolved ids)
  2. sys.sql_expression_dependencies name fallback (deferred name resolution)
  3. text scan of sys.sql_modules
...then the tokenizer-based analyzer confirms or rejects each hit, so text
matches in comments are reported as NO-ACTION instead of being missed or
over-counted. Synonyms pointing at a target are followed automatically.

Usage:
  python phase1_inventory.py --config config.yaml
  python phase1_inventory.py --config config.yaml --clr dbo.Get_concatenate --clr dbo.clr_Split
  python phase1_inventory.py --config config.yaml --clr-file clr_scope.txt
  python phase1_inventory.py --config config.yaml --reanalyze output/<run_id>   # offline, no DB
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from clr_migrator import __version__, reporting
from clr_migrator.analyzer import (CLR_TYPE_DESC, COMPLEXITY, Analyzer, name_parts,
                                   overall_class)
from clr_migrator.common import (build_targets, mapping_for, read_sql, runtime_identity,
                                 safe_filename, sha256_text, setup_logging, stamp, utc_now,
                                 write_text)
from clr_migrator.config import ConfigError, load_config, resolve_path, split_name
from clr_migrator.rewriter import preview, quote_name

log = logging.getLogger("phase1")

Q_TARGET = """
SELECT o.object_id, s.name AS schema_name, o.name AS object_name, o.type, o.type_desc,
       a.name AS assembly_name, am.assembly_class, am.assembly_method, a.permission_set_desc
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
LEFT JOIN sys.assembly_modules am ON am.object_id = o.object_id
LEFT JOIN sys.assemblies a ON a.assembly_id = am.assembly_id
WHERE o.object_id = OBJECT_ID(?);
"""
Q_PARAMS = """
SELECT p.parameter_id, p.name, TYPE_NAME(p.user_type_id) AS type_name,
       p.max_length, p.precision, p.scale, p.is_output
FROM sys.parameters p WHERE p.object_id = ? ORDER BY p.parameter_id;
"""
Q_SYNONYMS = """
SELECT s.name AS schema_name, sy.name AS synonym_name, sy.base_object_name
FROM sys.synonyms sy JOIN sys.schemas s ON s.schema_id = sy.schema_id;
"""
Q_DEPS = """
SELECT DISTINCT d.referencing_id AS object_id,
       CASE WHEN d.referenced_id IS NOT NULL THEN 'expression_dependencies'
            ELSE 'expression_dependencies:name_fallback' END AS method
FROM sys.sql_expression_dependencies d
WHERE d.referencing_class = 1
  AND (d.referenced_id = OBJECT_ID(?)
       OR (d.referenced_id IS NULL AND d.referenced_entity_name = ?
           AND ISNULL(d.referenced_schema_name, N'dbo') = ?));
"""
Q_TEXT = """
SELECT m.object_id FROM sys.sql_modules m
WHERE UPPER(m.definition) LIKE UPPER(?) ESCAPE N'\\';
"""
Q_DEFS = """
SELECT o.object_id, s.name AS schema_name, o.name AS object_name, o.type, o.type_desc,
       o.create_date, o.modify_date, m.definition,
       CASE WHEN m.object_id IS NULL THEN 0 ELSE 1 END AS has_module,
       m.uses_ansi_nulls, m.uses_quoted_identifier, m.is_schema_bound
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
WHERE o.object_id IN ({ids});
"""
Q_NONMODULE = """
SELECT N'COMPUTED_COLUMN' AS kind, s.name AS schema_name, t.name AS parent_name, c.name AS item_name,
       c.definition, CAST(c.is_persisted AS int) AS is_persisted
FROM sys.computed_columns c JOIN sys.objects t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE UPPER(c.definition) LIKE UPPER(?) ESCAPE N'\\'
UNION ALL
SELECT N'CHECK_CONSTRAINT', s.name, t.name, k.name, k.definition, NULL
FROM sys.check_constraints k JOIN sys.objects t ON t.object_id = k.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE UPPER(k.definition) LIKE UPPER(?) ESCAPE N'\\'
UNION ALL
SELECT N'DEFAULT_CONSTRAINT', s.name, t.name, k.name, k.definition, NULL
FROM sys.default_constraints k JOIN sys.objects t ON t.object_id = k.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE UPPER(k.definition) LIKE UPPER(?) ESCAPE N'\\';
"""

NONMODULE_SUGGESTION = {
    "COMPUTED_COLUMN": "Computed columns cannot be ALTERed in place: DROP and re-ADD the column with the "
                       "T-SQL expression in the table migration script. If PERSISTED or indexed, the "
                       "replacement function must be deterministic and SCHEMABINDING.",
    "CHECK_CONSTRAINT": "DROP and re-create the constraint using the replacement function "
                        "(WITH CHECK to revalidate existing rows).",
    "DEFAULT_CONSTRAINT": "DROP and re-create the default using the replacement expression.",
    "SYNONYM": "Retarget the synonym to the replacement object (or drop it once all callers are converted).",
}

OBJECT_ACTION = {
    "A": "AUTO - Phase 2 renames / re-signs the calls; review the diff.",
    "B": "AUTO - Phase 2 inlines STRING_AGG; confirm ordering needs and NULL/empty-set semantics.",
    "C": "MANUAL - follow call-site suggestions; Phase 2 applies the safe edits and leaves TODO markers.",
    "D": "MANUAL - dynamic SQL builds the call at runtime; rewrite the string builder by hand.",
    "V": "VERIFY - catalog reports a dependency but no call site was found lexically; inspect.",
    "N": "NONE - text match is a comment only; no change required.",
}


def like_pattern(name: str) -> str:
    esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")
    return f"%{esc}%"


def fmt_type(p: dict) -> str:
    t = p["type_name"] or "?"
    tl = t.lower()
    if tl in ("nvarchar", "nchar", "varchar", "char", "varbinary", "binary"):
        n = p["max_length"]
        size = "max" if n == -1 else (n // 2 if tl in ("nvarchar", "nchar") else n)
        return f"{t}({size})"
    if tl in ("decimal", "numeric"):
        return f"{t}({p['precision']},{p['scale']})"
    return t


def fmt_signature(params: list[dict]) -> str:
    args = [f"{p['name']} {fmt_type(p)}{' OUTPUT' if p['is_output'] else ''}"
            for p in params if p["parameter_id"] > 0]
    ret = [fmt_type(p) for p in params if p["parameter_id"] == 0]
    return f"({', '.join(args)})" + (f" RETURNS {ret[0]}" if ret else "")


# ----------------------------------------------------------------- collection
def collect_database(cur, db: str, scope: list[str], cfg: dict, run_dir: Path, used: set) -> dict:
    from clr_migrator.db import fetch

    opts = cfg["options"]
    targets = []
    synonyms_all = fetch(cur, Q_SYNONYMS)

    for entry in scope:
        sch, nm = split_name(entry)
        rows = fetch(cur, Q_TARGET, (f"{quote_name(sch)}.{quote_name(nm)}",))
        if not rows:
            log.warning("[%s] %s.%s not found - skipped", db, sch, nm)
            targets.append({"schema": sch, "name": nm, "found": False})
            continue
        r = rows[0]
        typ = r["type"].strip()
        params = fetch(cur, Q_PARAMS, (r["object_id"],))
        syns = []
        for s in synonyms_all:
            parts = name_parts(s["base_object_name"])
            if not parts:
                continue
            bs = (parts[-2] if len(parts) >= 2 and parts[-2] else "dbo").casefold()
            bdb = parts[-3].casefold() if len(parts) >= 3 and parts[-3] else None
            if parts[-1].casefold() == r["object_name"].casefold() and bs == r["schema_name"].casefold() \
                    and (bdb is None or bdb == db.casefold()):
                syns.append([s["schema_name"], s["synonym_name"]])
        if typ not in CLR_TYPE_DESC:
            log.warning("[%s] %s.%s is %s, not a CLR object - inventoried anyway",
                        db, r["schema_name"], r["object_name"], r["type_desc"])
        targets.append({
            "schema": r["schema_name"], "name": r["object_name"], "found": True,
            "object_id": r["object_id"], "type": typ, "type_desc": r["type_desc"],
            "is_clr": typ in CLR_TYPE_DESC, "assembly": r["assembly_name"],
            "assembly_class": r["assembly_class"], "assembly_method": r["assembly_method"],
            "permission_set": r["permission_set_desc"],
            "param_count": sum(1 for p in params if p["parameter_id"] > 0),
            "signature": fmt_signature(params), "synonyms": syns})
        log.info("[%s] target %s.%s (%s) %s; synonyms: %s", db, r["schema_name"], r["object_name"],
                 typ, fmt_signature(params), syns or "none")

    found: dict[int, dict] = {}

    def add(oid, method, key):
        e = found.setdefault(int(oid), {"methods": set(), "targets": set()})
        e["methods"].add(method)
        e["targets"].add(key)

    nonmodule = []
    for t in targets:
        if not t["found"]:
            continue
        key = f"{t['schema']}.{t['name']}"
        for sch, nm in [(t["schema"], t["name"])] + [tuple(x) for x in t["synonyms"]]:
            for row in fetch(cur, Q_DEPS, (f"{quote_name(sch)}.{quote_name(nm)}", nm, sch)):
                add(row["object_id"], row["method"], key)
            for row in fetch(cur, Q_TEXT, (like_pattern(nm),)):
                add(row["object_id"], "sql_modules_text_scan", key)
            lp = like_pattern(nm)
            for row in fetch(cur, Q_NONMODULE, (lp, lp, lp)):
                nonmodule.append({**row, "target": key})
        for sch, nm in t["synonyms"]:
            nonmodule.append({"kind": "SYNONYM", "schema_name": sch, "parent_name": nm, "item_name": "",
                              "definition": f"{quote_name(t['schema'])}.{quote_name(t['name'])}",
                              "is_persisted": None, "target": key})

    objects = []
    include = {x.upper() for x in opts["include_object_types"]}
    excl_schemas = {x.casefold() for x in opts["exclude_schemas"]}
    excl_objects = {"{}.{}".format(*split_name(x)).casefold() for x in opts["exclude_objects"]}
    ids = sorted(found)
    for i in range(0, len(ids), 500):
        chunk = ",".join(str(int(x)) for x in ids[i:i + 500])
        for r in fetch(cur, Q_DEFS.format(ids=chunk)):
            typ = r["type"].strip()
            full = f"{r['schema_name']}.{r['object_name']}"
            if not r["has_module"]:
                continue  # tables etc. - covered by the non-module query
            if typ not in include or r["schema_name"].casefold() in excl_schemas \
                    or full.casefold() in excl_objects:
                log.info("[%s] excluded by options: %s (%s)", db, full, typ)
                continue
            definition = r["definition"]
            meta = found[int(r["object_id"])]
            rec = {"object_id": r["object_id"], "schema": r["schema_name"], "name": r["object_name"],
                   "type": typ, "type_desc": r["type_desc"],
                   "uses_ansi_nulls": bool(r["uses_ansi_nulls"]),
                   "uses_quoted_identifier": bool(r["uses_quoted_identifier"]),
                   "is_schema_bound": bool(r["is_schema_bound"]),
                   "is_encrypted": definition is None, "modify_date": str(r["modify_date"]),
                   "discovered_by": sorted(meta["methods"]), "targets": sorted(meta["targets"]),
                   "definition_path": None, "sha256": None}
            if definition is not None:
                base = f"{safe_filename(r['schema_name'])}.{safe_filename(r['object_name'])}"
                rel = Path("definitions") / safe_filename(db) / typ / f"{base}.sql"
                if str(rel).casefold() in used:  # case-insensitive filesystems (Windows)
                    rel = rel.with_name(f"{base}__{r['object_id']}.sql")
                used.add(str(rel).casefold())
                write_text(run_dir / rel, definition)
                rec["definition_path"] = rel.as_posix()
                rec["sha256"] = sha256_text(definition)
            objects.append(rec)
    objects.sort(key=lambda o: (o["type"], o["schema"].casefold(), o["name"].casefold()))
    log.info("[%s] %d referencing module(s) extracted", db, len(objects))
    return {"targets": targets, "objects": objects, "non_module_dependencies": nonmodule}


# ------------------------------------------------------------------ analysis
def analyze_and_report(run_dir: Path, inv: dict, cfg: dict) -> dict:
    inv_dir = run_dir / "inventory"
    case_sensitive = cfg["options"]["case_sensitive"]
    site_rows, plan_rows, target_rows, nm_rows = [], [], [], []
    class_counts, tier_counts = Counter(), Counter()

    for db, dbinv in inv["databases"].items():
        targets = build_targets(dbinv, cfg)
        tmap = {t.key: t for t in targets}
        an = Analyzer(targets, case_sensitive, current_db=db)

        for t in dbinv["targets"]:
            m = mapping_for(f"{t['schema']}.{t['name']}", cfg)
            target_rows.append({
                "database": db, "clr_object": f"{t['schema']}.{t['name']}",
                "found": t.get("found"), "type": t.get("type", ""),
                "type_desc": CLR_TYPE_DESC.get(t.get("type", ""), t.get("type_desc", "")),
                "signature": t.get("signature", ""), "assembly": t.get("assembly", ""),
                "assembly_class": t.get("assembly_class", ""),
                "synonyms": ", ".join(".".join(s) for s in t.get("synonyms", [])),
                "strategy": m["strategy"], "replacement": m.get("replacement_object", ""),
                "impacted_objects": sum(1 for o in dbinv["objects"]
                                        if f"{t['schema']}.{t['name']}" in o["targets"])})

        for obj in dbinv["objects"]:
            label = f"{obj['schema']}.{obj['name']}"
            base = {"database": db, "object": label, "object_type": obj["type"]}
            if obj["is_encrypted"] or not obj["definition_path"]:
                plan_rows.append({**base, "overall_class": "C-ENCRYPTED", "complexity": "HIGH",
                                  "recommended_action": "Definition is encrypted - retrieve source from "
                                                        "source control before conversion.",
                                  "discovered_by": "; ".join(obj["discovered_by"])})
                tier_counts["HIGH"] += 1
                continue
            text = read_sql(run_dir / obj["definition_path"])
            a = an.analyze(text, obj["uses_quoted_identifier"])
            klasses = []
            for s in a.sites:
                klasses.append(s.klass)
                class_counts[s.klass] += 1
                site_rows.append({
                    **base, "clr_object": s.target, "called_via": s.alias, "line": s.line, "col": s.col,
                    "kind": s.kind, "class": s.klass, "auto": s.auto, "clause": s.clause or "",
                    "group_by": s.group_by, "nested_in": s.nested_in or "", "arg_count": len(s.args),
                    "snippet": s.snippet, "suggestion": s.suggestion,
                    "rewrite_preview": preview(s, tmap[s.target_key])})
            actionable = [k for k in klasses if not k.startswith("N")]
            catalog_hit = any(m != "sql_modules_text_scan" for m in obj["discovered_by"])
            if not actionable and catalog_hit:
                ov = "V-VERIFY"   # catalog says it depends on the CLR; lexer found no code call
            else:
                ov = overall_class(klasses) if klasses else "N-NONE"
            tier = COMPLEXITY[ov[0]]
            tier_counts[tier] += 1
            action = OBJECT_ACTION[ov[0]]
            if obj["is_schema_bound"]:
                action += " SCHEMABINDING: dependents must be dropped/recreated around the change."
            plan_rows.append({
                **base, "overall_class": ov, "complexity": tier,
                "total_sites": len(klasses), "auto_sites": sum(1 for s in a.sites if s.auto),
                "manual_sites": sum(1 for s in a.sites if not s.auto and not s.klass.startswith("N")),
                "classes": ", ".join(sorted(set(klasses))), "is_schema_bound": obj["is_schema_bound"],
                "recommended_action": action, "discovered_by": "; ".join(obj["discovered_by"]),
                "definition_path": obj["definition_path"]})

        seen_nm = set()
        for nm in dbinv.get("non_module_dependencies", []):
            k = (nm["kind"], nm["schema_name"], nm["parent_name"], nm["item_name"])
            if k in seen_nm:
                continue
            seen_nm.add(k)
            hits = [s for s in an.analyze(nm["definition"] or "").sites if s.kind in ("call", "reference")]
            if not hits:
                continue
            nm_rows.append({"database": db, "kind": nm["kind"],
                            "object": f"{nm['schema_name']}.{nm['parent_name']}",
                            "item": nm["item_name"], "clr_object": hits[0].target,
                            "definition": nm["definition"], "is_persisted": nm.get("is_persisted"),
                            "suggestion": NONMODULE_SUGGESTION[nm["kind"]]})

    tier_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}
    plan_rows.sort(key=lambda r: (tier_rank[r["complexity"]], r["database"], r["object"].casefold()))
    reporting.write_csv(inv_dir / "clr_targets.csv", target_rows)
    reporting.write_csv(inv_dir / "call_sites.csv", site_rows, [
        "database", "object", "object_type", "clr_object", "called_via", "line", "col", "kind", "class",
        "auto", "clause", "group_by", "nested_in", "arg_count", "snippet", "suggestion", "rewrite_preview"])
    reporting.write_csv(inv_dir / "conversion_plan.csv", plan_rows, [
        "database", "object", "object_type", "overall_class", "complexity", "total_sites", "auto_sites",
        "manual_sites", "classes", "is_schema_bound", "recommended_action", "discovered_by",
        "definition_path"])
    reporting.write_csv(inv_dir / "non_module_dependencies.csv", nm_rows, [
        "database", "kind", "object", "item", "clr_object", "is_persisted", "definition", "suggestion"])

    total_sites = sum(class_counts.values())
    auto_sites = sum(1 for r in site_rows if r["auto"])
    actionable_sites = sum(1 for r in site_rows if not r["class"].startswith("N"))
    summary = {"run_id": inv["run_id"], "generated_utc": utc_now().isoformat(),
               "targets_in_scope": len(target_rows),
               "targets_found": sum(1 for r in target_rows if r["found"]),
               "impacted_objects": len(plan_rows), "call_sites": total_sites,
               "actionable_sites": actionable_sites, "auto_convertible_sites": auto_sites,
               "sites_by_class": dict(class_counts), "objects_by_complexity": dict(tier_counts),
               "non_module_dependencies": len(nm_rows)}
    reporting.write_json(inv_dir / "summary.json", summary)

    pct = f"{(100 * auto_sites / actionable_sites):.0f}%" if actionable_sites else "n/a"
    reporting.write_html(
        inv_dir / "inventory_report.html", "CLR impact inventory",
        f"Run {inv['run_id']} | source {inv.get('source_server', '')} | tool {__version__}",
        [("CLR targets found", f"{summary['targets_found']}/{summary['targets_in_scope']}", ""),
         ("Impacted objects", len(plan_rows), ""),
         ("Actionable call sites", actionable_sites, ""),
         ("Auto-convertible", pct, "ok"),
         ("HIGH complexity objects", tier_counts.get("HIGH", 0), "bad"),
         ("Table-level dependencies", len(nm_rows), "warn" if nm_rows else "")],
        [("CLR targets", target_rows, ["database", "clr_object", "found", "type_desc", "signature",
                                       "synonyms", "strategy", "replacement", "impacted_objects"]),
         ("Conversion plan (hardest first)", plan_rows,
          ["database", "object", "object_type", "complexity", "overall_class", "total_sites",
           "auto_sites", "manual_sites", "recommended_action"]),
         ("Call sites needing manual work", [r for r in site_rows if not r["auto"]
                                             and not r["class"].startswith("N")],
          ["object", "line", "clr_object", "class", "snippet", "suggestion"]),
         ("Table-level dependencies (computed columns, constraints, synonyms)", nm_rows,
          ["database", "kind", "object", "item", "clr_object", "definition", "suggestion"])],
        notes=None, code_cols={"snippet", "definition"})
    return summary


# ---------------------------------------------------------------------- main
def resolve_scope(args, cfg) -> list[str]:
    names = list(args.clr or [])
    if args.clr_file:
        with open(args.clr_file, encoding="utf-8") as f:
            names += [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    if not names:
        names = [c["name"] for c in cfg["clr_objects"]]
    if not names:
        raise ConfigError("No CLR objects in scope: pass --clr / --clr-file or list clr_objects in config")
    seen, out = set(), []
    for n in names:
        k = "{}.{}".format(*split_name(n)).casefold()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--clr", action="append", help="CLR object to inventory (repeatable); overrides config scope")
    ap.add_argument("--clr-file", help="Text file with one CLR object name per line")
    ap.add_argument("--database", action="append", help="Limit to these source databases")
    ap.add_argument("--reanalyze", metavar="RUN_DIR",
                    help="Re-run analysis/suggestions on an existing run folder (no DB connection)")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    if args.reanalyze:
        run_dir = Path(args.reanalyze)
        setup_logging(run_dir / "logs", "phase1_reanalyze")
        inv = json.loads((run_dir / "inventory" / "inventory.json").read_text(encoding="utf-8"))
        s = analyze_and_report(run_dir, inv, cfg)
        log.info("Re-analysis complete: %s", json.dumps(s["objects_by_complexity"]))
        return 0

    from clr_migrator.db import connect_source, source_identity

    scope = resolve_scope(args, cfg)
    run_id = f"{safe_filename(cfg['project'])}_{stamp()}"
    run_dir = resolve_path(cfg, cfg["output_root"]) / run_id
    setup_logging(run_dir / "logs", "phase1")
    log.info("Run %s | CLR scope: %s", run_id, ", ".join(scope))

    src = cfg["source"]
    dbs = [d for d in src["databases"] if not args.database or d in args.database]
    inv = {"tool_version": __version__, "run_id": run_id, "created_utc": utc_now().isoformat(),
           "config_path": cfg["_path"], "config_sha256": cfg["_sha256"],
           "source_server": src["server"], "auth_method": src["auth"]["method"],
           "auth_client_id": source_identity(src), "runtime": runtime_identity(),
           "clr_scope": scope, "databases": {}}
    used: set = set()
    for db in dbs:
        conn = connect_source(src, db)
        try:
            inv["databases"][db] = collect_database(conn.cursor(), db, scope, cfg, run_dir, used)
        finally:
            conn.close()
    reporting.write_json(run_dir / "inventory" / "inventory.json", inv)

    s = analyze_and_report(run_dir, inv, cfg)
    log.info("Inventory complete -> %s", run_dir)
    log.info("Objects: %d | actionable sites: %d | auto-convertible: %d | by complexity: %s",
             s["impacted_objects"], s["actionable_sites"], s["auto_convertible_sites"],
             json.dumps(s["objects_by_complexity"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
