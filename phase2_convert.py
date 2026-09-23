#!/usr/bin/env python3
"""Phase 2 - config-driven replacement, validation, audit and RDS deploy bundle.

Reads a Phase 1 run folder (inventory.json + extracted definitions) and the
clr_objects mappings in config, then for every impacted object:
  * re-analyzes the definition (so mapping changes don't need a Phase 1 rerun)
  * applies span-based edits (rename / template / inline STRING_AGG), handles
    nested calls, converts CREATE -> CREATE OR ALTER, fixes stale header names
  * leaves /* CLR-MIGRATION TODO */ markers at sites that need a human
  * validates (static checks; optional live compile on RDS, always rolled back)
  * writes converted / manual_review files, unified diffs, audit log, reports,
    and a dependency-ordered sqlcmd deploy bundle per database.

Nothing is deployed. The only database action is the optional live compile
check, which runs inside a transaction that is always rolled back.

Usage:
  python phase2_convert.py --config config.yaml --inventory output/<run_id>
  python phase2_convert.py --config config.yaml --inventory output/<run_id> --live-validate
"""
from __future__ import annotations

import argparse
import difflib
import heapq
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from clr_migrator import __version__, reporting
from clr_migrator.analyzer import Analyzer, referenced_names
from clr_migrator.common import (build_targets, read_sql, runtime_identity, safe_filename,
                                 sha256_text, setup_logging, stamp, utc_now, write_text)
from clr_migrator.config import ConfigError, load_config, resolve_path
from clr_migrator.rewriter import OverlapError, apply_edits, build_edits
from clr_migrator.validators import Finding, LiveValidator, replacement_objects, static_checks

log = logging.getLogger("phase2")

TYPE_RANK = {"FN": 1, "IF": 1, "TF": 1, "V": 2, "P": 3, "TR": 4}
DEPLOYABLE = ("READY", "READY_WITH_WARNINGS")

GLOBAL_NOTES = [
    "Element order: SQL Server never guaranteed element order for CLR user-defined aggregates "
    "(IsInvariantToOrder is reserved and not honoured), and STRING_AGG without WITHIN GROUP is "
    "likewise unordered. Existing outputs may still have looked stable because of plan shape; "
    "set within_group_order_by where reports, exports or string comparisons depend on order.",
    "NULL inputs: STRING_AGG skips NULLs. The retired getConcatenate.Accumulate() reads Value.Value "
    "before its IsNull check, so a NULL reaching it would throw rather than be skipped. Include "
    "NULL rows in parity tests.",
    "Empty input: the CLR Terminate() returns '' for an empty accumulator; STRING_AGG returns NULL. "
    "With empty_result: empty_string (default) rewrites are wrapped in ISNULL(..., N''). Note that "
    "dbo.fn_Get_Concatenate as written returns NULL for an empty TVP - align it if callers compare to ''.",
    "8000-byte limit: STRING_AGG over a non-MAX input returns (n)varchar(8000/4000) and errors when "
    "exceeded. cast_to_nvarchar_max: true (default) casts the input to NVARCHAR(MAX).",
    "Deploy the prerequisite scripts (TVP type, wrapper functions) before any converted object. "
    "Run the bundle with sqlcmd (-b) or SSMS in SQLCMD mode; it stops at the first error.",
]


def write_diff(path: Path, rel: str, before: str, after: str) -> None:
    diff = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
    write_text(path, "".join(diff))


def wrap_for_deploy(body: str, ansi: bool, qi: bool, nl: str) -> str:
    tail = "" if body.endswith(("\n", "\r\n")) else nl
    return (f"SET ANSI_NULLS {'ON' if ansi else 'OFF'};{nl}GO{nl}"
            f"SET QUOTED_IDENTIFIER {'ON' if qi else 'OFF'};{nl}GO{nl}"
            f"{body}{tail}GO{nl}")


def order_for_deploy(items: list[dict]) -> tuple[list[dict], list[str]]:
    """Topological order on references between deployable objects; ties by type rank."""
    index = {(i["schema"].casefold(), i["name"].casefold()): n for n, i in enumerate(items)}
    deps = {n: set() for n in range(len(items))}
    for n, it in enumerate(items):
        for ref in referenced_names(it["body"], it["qi"]):
            m = index.get(ref)
            if m is not None and m != n:
                deps[n].add(m)
    indeg = {n: len(d) for n, d in deps.items()}
    users = {n: [] for n in deps}
    for n, d in deps.items():
        for m in d:
            users[m].append(n)
    key = lambda n: (TYPE_RANK.get(items[n]["type"], 9), items[n]["schema"].casefold(),  # noqa: E731
                     items[n]["name"].casefold())
    heap = [(key(n), n) for n, k in indeg.items() if k == 0]
    heapq.heapify(heap)
    out, warnings = [], []
    while heap:
        _, n = heapq.heappop(heap)
        out.append(items[n])
        for u in users[n]:
            indeg[u] -= 1
            if indeg[u] == 0:
                heapq.heappush(heap, (key(u), u))
    if len(out) < len(items):
        rest = sorted((n for n, k in indeg.items() if k > 0), key=key)
        warnings.append("Dependency cycle among: " + ", ".join(
            f"{items[n]['schema']}.{items[n]['name']}" for n in rest) + " - appended by type order")
        out += [items[n] for n in rest]
    return out, warnings


def build_bundle(db: str, target_db: str, ordered: list[dict], prereqs: list[tuple[Path, str]],
                 run_meta: dict) -> str:
    nl = "\r\n"
    lines = [f"/*{nl}  CLR retirement deploy bundle - database {target_db} (source {db}){nl}"
             f"  Generated {run_meta['generated_utc']} by clr-migrator {__version__}{nl}"
             f"  Phase 1 run {run_meta['inventory_run']} | conversion {run_meta['conversion_id']}{nl}"
             f"  Objects: {len(ordered)} | prerequisites: {len(prereqs)}{nl}"
             f"  Run:  sqlcmd -S <rds-endpoint>,1433 -d {target_db} -U <user> -b -i deploy_bundle.sql{nl}"
             f"  or SSMS with Query > SQLCMD Mode enabled.{nl}*/{nl}",
             f":on error exit{nl}", f"USE {'[' + target_db.replace(']', ']]') + ']'};{nl}GO{nl}",
             f"SET NOCOUNT ON;{nl}GO{nl}"]
    for p, text in prereqs:
        lines.append(f"PRINT N'--- prerequisite {p.name}';{nl}GO{nl}")
        body = text.rstrip()
        lines.append(body + nl + ("" if body.upper().endswith("GO") else f"GO{nl}"))
    total = len(ordered)
    for n, it in enumerate(ordered, 1):
        label = f"{it['schema']}.{it['name']}".replace("'", "''")
        lines.append(f"PRINT N'[{n}/{total}] {label} ({it['type']})';{nl}GO{nl}")
        lines.append(wrap_for_deploy(it["body"], it["ansi"], it["qi"], nl))
    lines.append(f"PRINT N'Deployment complete: {total} object(s).';{nl}GO{nl}")
    return "".join(lines)


def convert_object(obj: dict, db: str, ctx: dict) -> dict:
    run_dir, conv_dir, cfg = ctx["run_dir"], ctx["conv_dir"], ctx["cfg"]
    an, tmap, norm = ctx["analyzer"], ctx["tmap"], ctx["norm"]
    label = f"{obj['schema']}.{obj['name']}"
    res = {"database": db, "object": label, "object_type": obj["type"], "status": "", "auto_edits": 0,
           "manual_sites": 0, "fail": 0, "warn": 0, "output_path": "", "diff_path": "", "notes": ""}
    findings: list[Finding] = []

    if obj["is_encrypted"] or not obj["definition_path"]:
        res.update(status="MANUAL_REVIEW", notes="Encrypted definition - source not available")
        return {"result": res, "findings": findings, "audit": [], "deploy": None}

    text = read_sql(run_dir / obj["definition_path"])
    nl = "\r\n" if "\r\n" in text else "\n"
    sha_before = sha256_text(text)
    qi = obj["uses_quoted_identifier"]
    a = an.analyze(text, qi)
    actionable = [s for s in a.sites if not s.klass.startswith("N")]
    if not actionable:
        res.update(status="NO_ACTION", notes="Only comment mentions / no call sites")
        return {"result": res, "findings": findings, "audit": [], "deploy": None}

    edits, notes = build_edits(a, tmap, obj["schema"], obj["name"],
                               cfg["options"]["create_or_alter"], norm)
    try:
        new_text, audit = apply_edits(text, edits)
    except OverlapError as ex:
        res.update(status="FAILED", notes=str(ex))
        findings.append(Finding(db, label, "V02", "FAIL", str(ex)))
        return {"result": res, "findings": findings, "audit": [], "deploy": None}

    a2 = an.analyze(new_text, qi)
    manual = [s for s in actionable if not s.auto]
    used = {}
    for s in actionable:
        if s.auto:
            used.setdefault(tmap[s.target_key].display, set()).update(
                replacement_objects(tmap[s.target_key].mapping))
    header_fixed = any(e.rule == "HDR-NAME-FIX" for e in edits)
    findings += static_checks(db, label, a2, audit, cfg, ctx["prereq_text"],
                              sha_before != obj["sha256"], header_fixed, used)
    if manual:
        # residual references are expected in partially converted objects - report once, not as FAIL
        findings = [f for f in findings if f.check_id not in ("V01", "V10")]
        findings.append(Finding(db, label, "V01", "INFO",
                                f"{len(manual)} site(s) left for manual conversion (TODO markers inserted)"))

    fails = sum(1 for f in findings if f.severity == "FAIL")
    warns = sum(1 for f in findings if f.severity == "WARN")
    if manual:
        status = "MANUAL_REVIEW"
    elif fails:
        status = "FAILED"
    elif warns:
        status = "READY_WITH_WARNINGS"
    else:
        status = "READY"

    sub = "converted" if status in DEPLOYABLE else "manual_review"
    rel = Path(sub) / safe_filename(db) / obj["type"] / Path(obj["definition_path"]).name
    write_text(conv_dir / rel, wrap_for_deploy(new_text, obj["uses_ansi_nulls"], qi, nl))
    diff_rel = Path("diffs") / safe_filename(db) / obj["type"] / (Path(obj["definition_path"]).stem + ".diff")
    write_diff(conv_dir / diff_rel, obj["definition_path"], text, new_text)

    sha_after = sha256_text(new_text)
    audit_rows = []
    for e in audit:
        audit_rows.append({
            "conversion_id": ctx["conversion_id"], "inventory_run": ctx["inventory_run"],
            "timestamp_utc": ctx["ts"], "operator": ctx["identity"]["os_user"],
            "host": ctx["identity"]["host"], "database": db, "object": label,
            "object_type": obj["type"], "rule": e.rule, "edit_kind": e.kind, "clr_object": e.target,
            "line": text.count("\n", 0, e.start) + 1, "before": e.before, "after": e.after,
            "object_sha256_before": sha_before, "object_sha256_after": sha_after})

    res.update(status=status, auto_edits=sum(1 for e in audit if e.rule[0] in "AB"),
               manual_sites=len(manual), fail=fails, warn=warns, output_path=rel.as_posix(),
               diff_path=diff_rel.as_posix(), notes="; ".join(notes))
    deploy = None
    if status in DEPLOYABLE:
        deploy = {"schema": obj["schema"], "name": obj["name"], "type": obj["type"], "body": new_text,
                  "ansi": obj["uses_ansi_nulls"], "qi": qi, "result": res}
    return {"result": res, "findings": findings, "audit": audit_rows, "deploy": deploy}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--inventory", required=True, help="Phase 1 run folder")
    ap.add_argument("--database", action="append", help="Limit to these source databases")
    ap.add_argument("--live-validate", action="store_true",
                    help="Compile converted objects on the RDS target (rolled back)")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    run_dir = Path(args.inventory)
    inv_path = run_dir / "inventory" / "inventory.json"
    if not inv_path.is_file():
        print(f"Not a Phase 1 run folder (missing {inv_path})", file=sys.stderr)
        return 2
    inv = json.loads(inv_path.read_text(encoding="utf-8"))
    conversion_id = f"conversion_{stamp()}"
    conv_dir = run_dir / conversion_id
    setup_logging(conv_dir / "logs", "phase2")
    identity = runtime_identity()

    prereqs = []
    for p in cfg["prerequisite_scripts"]:
        rp = resolve_path(cfg, p)
        if not rp.is_file():
            log.error("Prerequisite script not found: %s", rp)
            return 2
        prereqs.append((rp, read_sql(rp)))
    prereq_text = "\n".join(t for _, t in prereqs)

    results, findings, audit_rows, order_rows = [], [], [], []
    deploy_by_db: dict[str, list[dict]] = {}
    ts = utc_now().isoformat()

    for db, dbinv in inv["databases"].items():
        if args.database and db not in args.database:
            continue
        targets = build_targets(dbinv, cfg)
        ctx = {"run_dir": run_dir, "conv_dir": conv_dir, "cfg": cfg, "tmap": {t.key: t for t in targets},
               "analyzer": Analyzer(targets, cfg["options"]["case_sensitive"], current_db=db),
               "norm": (lambda s: s) if cfg["options"]["case_sensitive"] else (lambda s: s.casefold()),
               "prereq_text": prereq_text, "conversion_id": conversion_id,
               "inventory_run": inv["run_id"], "ts": ts, "identity": identity}
        for t in targets:
            log.info("[%s] %s -> strategy %s", db, t.display, t.strategy)
        for obj in dbinv["objects"]:
            out = convert_object(obj, db, ctx)
            results.append(out["result"])
            findings += out["findings"]
            audit_rows += out["audit"]
            if out["deploy"]:
                deploy_by_db.setdefault(db, []).append(out["deploy"])
            r = out["result"]
            log.info("[%s] %-60s %s", db, r["object"], r["status"])

    # ---- optional live validation on RDS
    lv = cfg["target"]["live_validation"]
    if args.live_validate or lv.get("enabled"):
        validator = LiveValidator(lv)
        try:
            req_objs = [o for t in cfg["clr_objects"] for o in replacement_objects(t)]
            needs_agg = any(a["rule"].startswith("B-STRING_AGG") for a in audit_rows)
            findings += validator.preflight(req_objs, cfg["target"]["required_types"], needs_agg)
            for db, items in deploy_by_db.items():
                for it in items:
                    f = validator.compile(db, f"{it['schema']}.{it['name']}", it["body"], it["ansi"], it["qi"])
                    findings.append(f)
                    if f.severity == "FAIL":
                        it["result"]["status"] = "FAILED"
                        it["result"]["fail"] += 1
                        log.warning("[%s] %s failed live compile", db, it["result"]["object"])
        finally:
            validator.close()
        for db in deploy_by_db:
            deploy_by_db[db] = [i for i in deploy_by_db[db] if i["result"]["status"] in DEPLOYABLE]
    else:
        findings.append(Finding("*", "<target>", "V09", "SKIP",
                                "Live compile on RDS not run (use --live-validate)"))

    # ---- deploy bundles
    run_meta = {"generated_utc": ts, "inventory_run": inv["run_id"], "conversion_id": conversion_id}
    for db, items in deploy_by_db.items():
        ordered, warns = order_for_deploy(items)
        for w in warns:
            findings.append(Finding(db, "<bundle>", "V12", "WARN", w))
        target_db = cfg["target"]["database_map"].get(db, db)
        bundle = build_bundle(db, target_db, ordered, prereqs, run_meta)
        write_text(conv_dir / "deploy" / safe_filename(db) / "deploy_bundle.sql", bundle, bom=True)
        for n, it in enumerate(ordered, 1):
            order_rows.append({"database": db, "target_database": target_db, "seq": n,
                               "object": f"{it['schema']}.{it['name']}", "object_type": it["type"],
                               "status": it["result"]["status"], "file": it["result"]["output_path"]})

    # ---- reports
    aud = conv_dir / "audit"
    val = conv_dir / "validation"
    reporting.write_csv(aud / "audit_log.csv", audit_rows, [
        "conversion_id", "inventory_run", "timestamp_utc", "operator", "host", "database", "object",
        "object_type", "rule", "edit_kind", "clr_object", "line", "before", "after",
        "object_sha256_before", "object_sha256_after"])
    reporting.write_csv(val / "validation_findings.csv", [f.row() for f in findings],
                        ["database", "object", "check_id", "severity", "message", "line"])
    reporting.write_csv(conv_dir / "conversion_summary.csv", results, [
        "database", "object", "object_type", "status", "auto_edits", "manual_sites", "fail", "warn",
        "output_path", "diff_path", "notes"])
    reporting.write_csv(conv_dir / "deploy" / "deploy_order.csv", order_rows,
                        ["database", "target_database", "seq", "object", "object_type", "status", "file"])

    status_counts = Counter(r["status"] for r in results)
    manifest = {"tool_version": __version__, "conversion_id": conversion_id,
                "inventory_run": inv["run_id"], "generated_utc": ts, "runtime": identity,
                "config_path": cfg["_path"], "config_sha256": cfg["_sha256"],
                "inventory_config_sha256": inv.get("config_sha256"),
                "target": {k: v for k, v in cfg["target"].items() if k != "live_validation"},
                "live_validation": bool(args.live_validate or lv.get("enabled")),
                "prerequisites": [{"path": str(p), "sha256": sha256_text(t)} for p, t in prereqs],
                "status_counts": dict(status_counts), "edits_applied": len(audit_rows)}
    reporting.write_json(conv_dir / "manifest.json", manifest)

    problem = [f.row() for f in findings if f.severity in ("FAIL", "WARN")]
    reporting.write_html(
        val / "conversion_report.html", "CLR conversion - validation & audit",
        f"Conversion {conversion_id} | inventory {inv['run_id']} | target {cfg['target']['platform']} "
        f"engine {cfg['target']['engine_major_version']} compat {cfg['target']['compat_level']}",
        [("Objects processed", len(results), ""),
         ("Ready", status_counts.get("READY", 0), "ok"),
         ("Ready with warnings", status_counts.get("READY_WITH_WARNINGS", 0), "warn"),
         ("Manual review", status_counts.get("MANUAL_REVIEW", 0), "bad"),
         ("Failed", status_counts.get("FAILED", 0), "bad"),
         ("No action", status_counts.get("NO_ACTION", 0), ""),
         ("Edits audited", len(audit_rows), "")],
        [("Object status", sorted(results, key=lambda r: (r["status"], r["object"])),
          ["database", "object", "object_type", "status", "auto_edits", "manual_sites", "fail", "warn",
           "notes"]),
         ("Validation failures and warnings", problem,
          ["database", "object", "check_id", "severity", "message", "line"]),
         ("Deploy order", order_rows, ["database", "seq", "object", "object_type", "status"]),
         ("Audit trail (edits)", audit_rows, ["object", "line", "rule", "clr_object", "before", "after"])],
        notes=GLOBAL_NOTES, code_cols={"before", "after", "message"})

    log.info("Done -> %s", conv_dir)
    log.info("Status: %s | edits audited: %d", dict(status_counts), len(audit_rows))
    return 1 if status_counts.get("FAILED") else 0


if __name__ == "__main__":
    sys.exit(main())
