"""Span-based rewriter.

Edits are applied by character offset against the original text, so anything
outside a rewritten span (formatting, comments, line endings) is untouched.
Nested call sites - e.g. a CLR scalar inside the argument of a CLR aggregate -
are rendered inner-first and substituted into the outer call's arguments.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .analyzer import PLACEHOLDER, Analysis, Site, Target


class OverlapError(Exception):
    pass


@dataclass
class Edit:
    start: int
    end: int
    kind: str                  # rename | call | insert | replace
    rule: str
    payload: str = ""
    site: Site | None = None
    target: Target | None = None


@dataclass
class AuditEntry:
    rule: str
    kind: str
    target: str
    start: int
    end: int
    before: str
    after: str
    note: str = ""


def quote_name(s: str) -> str:
    return "[" + s.replace("]", "]]") + "]"


def _sub(template: str, args: list[str]) -> str:
    def rep(m):
        k = m.group(1)
        return ", ".join(args) if k == "args" else args[int(k)]
    return PLACEHOLDER.sub(rep, template)


def render_string_agg(args: list[str], mapping: dict) -> str:
    sa = mapping.get("string_agg", {})
    value = args[0]
    if sa.get("cast_to_nvarchar_max", True):
        # Without the cast, STRING_AGG over a non-MAX input is capped at 8000
        # bytes and raises an error; the CLR returned NVARCHAR(MAX).
        value = f"CAST({value} AS NVARCHAR(MAX))"
    if sa.get("delimiter_from_arg") is not None:
        sep = args[int(sa["delimiter_from_arg"])]
    else:
        sep = "N'" + str(sa.get("delimiter", ",")).replace("'", "''") + "'"
    expr = f"STRING_AGG({value}, {sep})"
    order_by = sa.get("within_group_order_by")
    if order_by:
        expr += f" WITHIN GROUP (ORDER BY {_sub(order_by, args)})"
    if sa.get("empty_result", "empty_string") == "empty_string":
        # CLR Terminate() returns '' for an empty accumulator; STRING_AGG returns NULL.
        expr = f"ISNULL({expr}, N'')"
    return expr


def render_call(site: Site, tgt: Target, args: list[str]) -> str:
    if tgt.strategy == "string_agg":
        return render_string_agg(args, tgt.mapping)
    if tgt.strategy == "template":
        return _sub(tgt.mapping["call_template"], args)
    raise ValueError(f"render_call not valid for strategy {tgt.strategy}")


def preview(site: Site, tgt: Target) -> str:
    """Single-site rewrite preview for the Phase 1 report (no nested resolution)."""
    if not site.auto:
        return ""
    if tgt.strategy == "rename":
        rep = tgt.mapping["replacement_object"]
        return f"{rep}({', '.join(site.args)})" if site.kind == "call" else rep
    return render_call(site, tgt, site.args)


def _safe_comment(s: str) -> str:
    return s.replace("*/", "* /").replace("/*", "/ *")


def build_edits(analysis: Analysis, targets: dict[str, Target], catalog_schema: str,
                catalog_name: str, create_or_alter: bool, norm) -> tuple[list[Edit], list[str]]:
    edits: list[Edit] = []
    notes: list[str] = []
    for s in analysis.sites:
        tgt = targets[s.target_key]
        if s.auto:
            if tgt.strategy == "rename":
                edits.append(Edit(s.name_start, s.name_end, "rename", s.klass,
                                  tgt.mapping["replacement_object"], s, tgt))
            else:
                edits.append(Edit(s.span_start, s.span_end, "call", s.klass, "", s, tgt))
            if s.db_part:
                notes.append(f"line {s.line}: same-database qualifier '{s.db_part}' dropped by rewrite")
            if s.alias.startswith("synonym"):
                notes.append(f"line {s.line}: call via {s.alias}; retarget or drop the synonym")
        elif s.klass != "N-COMMENT":
            marker = f"/* CLR-MIGRATION TODO [{s.klass}] {tgt.display}: {_safe_comment(s.short)} */ "
            edits.append(Edit(s.span_start, s.span_start, "insert", "TODO-MARKER", marker, s, tgt))

    h = analysis.header
    if h:
        if create_or_alter and not h.has_or_alter and not h.is_alter:
            edits.append(Edit(h.create_end, h.create_end, "insert", "HDR-CREATE-OR-ALTER", " OR ALTER"))
        hdr_schema = h.name_parts[-2] if len(h.name_parts) >= 2 else None
        hdr_name = h.name_parts[-1]
        if hdr_schema is None or norm(hdr_schema) != norm(catalog_schema) or hdr_name != catalog_name:
            edits.append(Edit(h.name_start, h.name_end, "replace", "HDR-NAME-FIX",
                              f"{quote_name(catalog_schema)}.{quote_name(catalog_name)}"))
            notes.append(f"header name '{'.'.join(h.name_parts)}' replaced with catalog name "
                         f"{catalog_schema}.{catalog_name} (stale sp_rename or missing schema)")
    return edits, notes


def _sort_key(e: Edit):
    return (e.start, 0 if e.start == e.end else 1, -e.end)


def _contains(parent: Edit, child: Edit) -> bool:
    if parent.start == parent.end:
        return False
    if child.start == child.end:
        return parent.start < child.start < parent.end
    return child.start >= parent.start and child.end <= parent.end


def apply_edits(text: str, edits: list[Edit]) -> tuple[str, list[AuditEntry]]:
    audit: list[AuditEntry] = []
    ordered = sorted(edits, key=_sort_key)
    return _apply(text, 0, len(text), ordered, audit), audit


def _apply(text: str, lo: int, hi: int, edits: list[Edit], audit: list[AuditEntry]) -> str:
    out, pos, i = [], lo, 0
    while i < len(edits):
        e = edits[i]
        if e.start < pos:
            raise OverlapError(f"Overlapping edits at offset {e.start} ({e.rule})")
        j, children = i + 1, []
        while j < len(edits) and _contains(e, edits[j]):
            children.append(edits[j])
            j += 1
        out.append(text[pos:e.start])
        out.append(_render(text, e, children, audit))
        pos, i = e.end, j
    out.append(text[pos:hi])
    return "".join(out)


def _render(text: str, e: Edit, children: list[Edit], audit: list[AuditEntry]) -> str:
    before = text[e.start:e.end]
    if e.kind == "call":
        args = []
        for a, b in e.site.arg_spans:
            kids = [c for c in children if c.start >= a and c.end <= b]
            args.append(_apply(text, a, b, kids, audit).strip())
        after = render_call(e.site, e.target, args)
    else:
        after = e.payload
    audit.append(AuditEntry(e.rule, e.kind, e.target.display if e.target else "", e.start, e.end,
                            before, after))
    return after
