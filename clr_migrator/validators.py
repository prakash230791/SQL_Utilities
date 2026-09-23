"""Validation checks run by Phase 2.

Static (always):
  V01 NO_RESIDUAL_CLR      no call/reference to a retired CLR remains in code
  V02 LEXICAL_INTEGRITY    converted text tokenizes cleanly, parentheses balanced
  V03 SOURCE_UNCHANGED     definition file hash still matches Phase 1 inventory
  V04 TARGET_FEATURES      STRING_AGG / WITHIN GROUP / CREATE OR ALTER supported by target
  V05 RDS_COMPAT           lint for features unavailable or restricted on Amazon RDS
  V06 ORDER_PARITY         STRING_AGG without WITHIN GROUP (element order not guaranteed)
  V07 HEADER_NAME          CREATE header name differed from catalog name (auto-fixed)
  V08 REPLACEMENT_PRESENT  replacement objects defined in prerequisite scripts / on target
  V10 DYNAMIC_SQL_RESIDUE  retired CLR name still inside a string literal
Live (optional, --live-validate):
  V09 LIVE_COMPILE         CREATE OR ALTER executed on RDS inside a transaction, rolled back
  V11 TARGET_PREFLIGHT     target version / compat level / prerequisite objects exist
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass

from .analyzer import Analysis, iter_multipart_names, name_parts

log = logging.getLogger(__name__)


@dataclass
class Finding:
    database: str
    object: str
    check_id: str
    severity: str        # PASS | INFO | WARN | FAIL | SKIP
    message: str
    line: int | None = None

    def row(self) -> dict:
        return asdict(self)


RDS_LINT_RULES = [
    ("RDS-XPCMDSHELL", "FAIL", r"\bxp_cmdshell\b",
     "xp_cmdshell is not available on Amazon RDS for SQL Server"),
    ("RDS-OLE-AUTOMATION", "WARN", r"\bsp_OA\w+",
     "OLE Automation procedures (sp_OA*) are not available on RDS; move logic to the application tier"),
    ("RDS-ASSEMBLY", "WARN", r"\bCREATE\s+ASSEMBLY\b|\bEXTERNAL\s+NAME\b",
     "CLR assembly reference - contradicts the CLR retirement goal and is restricted on RDS"),
    ("RDS-ADHOC-DQ", "WARN", r"\bOPEN(?:ROWSET|DATASOURCE)\b",
     "OPENROWSET/OPENDATASOURCE depend on ad hoc distributed queries, which cannot be enabled on RDS"),
    ("RDS-BULK-FILE", "WARN", r"\bBULK\s+INSERT\b",
     "BULK INSERT on RDS can only read files staged through the S3 integration (D:\\S3\\)"),
    ("RDS-SP-CONFIGURE", "WARN", r"\bsp_configure\b|\bRECONFIGURE\b",
     "Instance settings on RDS are managed through parameter groups, not sp_configure"),
    ("RDS-FILESTREAM", "WARN", r"\bFILESTREAM\b|\bFILETABLE\b",
     "FILESTREAM / FileTable are not supported on RDS"),
    ("RDS-TRUSTWORTHY", "WARN", r"\bTRUSTWORTHY\b",
     "Setting TRUSTWORTHY is restricted on RDS"),
    ("RDS-LINKED-SERVER-DDL", "WARN", r"\bsp_addlinkedserver\b",
     "Linked server creation on RDS is restricted; confirm the remote source is reachable and supported"),
]
_RDS_RX = [(rid, sev, re.compile(rx, re.IGNORECASE), msg) for rid, sev, rx, msg in RDS_LINT_RULES]


def _code_only(analysis: Analysis) -> str:
    """Original text with strings/comments blanked (newlines kept for line numbers)."""
    out = []
    for t in analysis.tokens:
        if t.kind in ("STRING", "LCOMMENT", "BCOMMENT"):
            out.append(re.sub(r"[^\n]", " ", t.text))
        else:
            out.append(t.text)
    return "".join(out)


def static_checks(db: str, obj: str, converted: Analysis, audit, cfg: dict,
                  prereq_text: str, sha_mismatch: bool, header_fixed: bool,
                  targets_used: dict) -> list[Finding]:
    F: list[Finding] = []
    add = lambda cid, sev, msg, line=None: F.append(Finding(db, obj, cid, sev, msg, line))  # noqa: E731
    tgt = cfg["target"]

    # V01 / V10 residual references
    residual = [s for s in converted.sites if s.kind in ("call", "reference")]
    dynamic = [s for s in converted.sites if s.kind == "dynamic_sql"]
    for s in residual:
        add("V01", "FAIL", f"Residual reference to {s.target} ({s.klass})", s.line)
    for s in dynamic:
        add("V10", "FAIL", f"{s.target} still referenced inside a string literal", s.line)
    if not residual:
        add("V01", "PASS", "No residual call/reference to retired CLR objects")

    # V02 lexical integrity
    if converted.lex_errors or converted.unbalanced:
        add("V02", "FAIL", "; ".join(converted.lex_errors) or f"{converted.unbalanced} unbalanced parentheses")
    else:
        add("V02", "PASS", "Tokenizes cleanly; parentheses balanced")

    # V03 source drift
    if sha_mismatch:
        add("V03", "WARN", "Definition file changed since Phase 1 inventory (hash mismatch)")

    # V04 target feature support
    rules = {a.rule for a in audit}
    uses_agg = any(r.startswith("B-STRING_AGG") for r in rules)
    if uses_agg:
        if int(tgt["compat_level"]) < 140 or int(tgt["engine_major_version"]) < 14:
            add("V04", "FAIL", "STRING_AGG / WITHIN GROUP need SQL Server 2017+ and compatibility level 140+; "
                               f"target configured as engine {tgt['engine_major_version']} / compat {tgt['compat_level']}")
        else:
            add("V04", "PASS", "Target supports STRING_AGG")
    if "HDR-CREATE-OR-ALTER" in rules and int(tgt["engine_major_version"]) < 13:
        add("V04", "FAIL", "CREATE OR ALTER needs SQL Server 2016 SP1+")

    # V05 RDS lint
    code = _code_only(converted)
    for rid, sev, rx, msg in _RDS_RX:
        for m in rx.finditer(code):
            add("V05", sev, f"{rid}: {msg}", code.count("\n", 0, m.start()) + 1)
    target_db = tgt["database_map"].get(db, db)
    for parts, first, _ in iter_multipart_names(converted.sig):
        line = converted.sig[first].line
        if len(parts) == 4:
            add("V05", "WARN", f"RDS-LINKED-SERVER: four-part name {'.'.join(parts)}", line)
        elif len(parts) == 3 and parts[0]:
            dbp = parts[0].casefold()
            if dbp in ("master", "msdb"):
                add("V05", "WARN", f"RDS-SYSTEM-DB: reference to {parts[0]} - permissions are limited on RDS", line)
            elif dbp not in (db.casefold(), str(target_db).casefold(), "tempdb"):
                add("V05", "WARN", f"RDS-CROSS-DB: {'.'.join(parts)} - database must live on the same RDS instance", line)

    # V06 order parity
    agg_edits = [a for a in audit if a.rule.startswith("B-STRING_AGG")]
    unordered = [a for a in agg_edits if "WITHIN GROUP" not in a.after]
    if unordered:
        add("V06", "WARN", f"{len(unordered)} STRING_AGG rewrite(s) without WITHIN GROUP - element order is not "
                           "guaranteed; confirm consumers do not depend on it")

    # V07 header
    if header_fixed:
        add("V07", "WARN", "CREATE header name did not match catalog name; replaced with catalog name")

    # V08 replacement objects present in prerequisite scripts
    lower_prereq = prereq_text.casefold()
    for tgt_display, needed in targets_used.items():
        for obj_name in needed:
            parts = name_parts(obj_name)
            if not parts:
                continue
            if parts[-1].casefold() not in lower_prereq:
                add("V08", "WARN", f"Replacement {obj_name} (for {tgt_display}) not found in prerequisite "
                                   "scripts; it must exist on the target before this object deploys")
    return F


def replacement_objects(mapping: dict) -> list[str]:
    """Objects the rewritten code will depend on."""
    if mapping.get("strategy") == "rename":
        return [mapping["replacement_object"]]
    if mapping.get("strategy") == "template":
        found = re.findall(r"((?:\[[^\]]+\]|\w+)\s*\.\s*(?:\[[^\]]+\]|\w+))\s*\(", mapping["call_template"])
        return [re.sub(r"\s+", "", f) for f in found]
    return []


class LiveValidator:
    """Compiles converted objects on the RDS target inside a transaction that is
    always rolled back. Use a non-production target: CREATE OR ALTER briefly
    takes schema-modification locks on existing objects."""

    def __init__(self, lv_cfg: dict):
        from .db import connect_target
        self.cfg = lv_cfg
        self.conn = connect_target(lv_cfg)

    def preflight(self, required_objects: list[str], required_types: list[str],
                  needs_string_agg: bool) -> list[Finding]:
        db = self.cfg["database"]
        F = []
        cur = self.conn.cursor()
        cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)), "
                    "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)), "
                    "(SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME())")
        ver, edition, compat = cur.fetchone()
        major = int(str(ver).split(".")[0])
        F.append(Finding(db, "<target>", "V11", "INFO", f"Target {ver} {edition}, compat level {compat}"))
        if needs_string_agg and (major < 14 or int(compat) < 140):
            F.append(Finding(db, "<target>", "V11", "FAIL",
                             "Target cannot run STRING_AGG (needs SQL Server 2017+ / compat 140+)"))
        for o in sorted(set(required_objects)):
            cur.execute("SELECT OBJECT_ID(?)", (o,))
            ok = cur.fetchone()[0] is not None
            F.append(Finding(db, "<target>", "V11", "PASS" if ok else "FAIL",
                             f"Prerequisite object {o} {'exists' if ok else 'MISSING - deploy prerequisites first'}"))
        for t in sorted(set(required_types)):
            cur.execute("SELECT TYPE_ID(?)", (t,))
            ok = cur.fetchone()[0] is not None
            F.append(Finding(db, "<target>", "V11", "PASS" if ok else "FAIL",
                             f"Prerequisite type {t} {'exists' if ok else 'MISSING'}"))
        return F

    def compile(self, db: str, obj: str, body: str, ansi_nulls: bool, quoted_identifier: bool) -> Finding:
        import pyodbc
        self.conn.autocommit = False
        cur = self.conn.cursor()
        try:
            cur.execute(f"SET ANSI_NULLS {'ON' if ansi_nulls else 'OFF'}")
            cur.execute(f"SET QUOTED_IDENTIFIER {'ON' if quoted_identifier else 'OFF'}")
            cur.execute(body)
            return Finding(db, obj, "V09", "PASS", "Compiled on target (rolled back)")
        except pyodbc.Error as ex:
            msg = " | ".join(str(a) for a in ex.args)[:800]
            return Finding(db, obj, "V09", "FAIL", f"Target compile failed: {msg}")
        finally:
            try:
                self.conn.rollback()
            finally:
                self.conn.autocommit = True

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
