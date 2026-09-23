"""Call-site discovery and classification for retired CLR objects.

For every reference to a target CLR object (or a synonym of it) inside a module
definition, determines the syntactic context - query block, GROUP BY,
DISTINCT argument, OVER() window, enclosing function, string literal - and
assigns a conversion class that both phases use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tsql_lexer import Token, case_blocks, paren_structure, significant, tokenize

CLR_TYPE_DESC = {
    "AF": "CLR aggregate",
    "FS": "CLR scalar function",
    "FT": "CLR table-valued function",
    "PC": "CLR stored procedure",
    "TA": "CLR trigger",
}

# Higher rank = harder. Object's overall class is the highest-ranked site.
CLASS_RANK = {"D": 6, "C": 5, "V": 4, "B": 3, "A": 2, "N": 1}
COMPLEXITY = {"D": "HIGH", "C": "HIGH", "V": "MEDIUM", "B": "MEDIUM", "A": "LOW", "N": "NONE"}

PLACEHOLDER = re.compile(r"\{(\d+|args)\}")
LITERAL_OR_VAR = re.compile(r"^(?:N?'(?:[^']|'')*'|@[\w@#$]+)$", re.IGNORECASE | re.DOTALL)

_STOP_BACK = frozenset({"SET", "IF", "WHILE", "RETURN", "DECLARE", "EXEC", "EXECUTE", "PRINT",
                        "BEGIN", "END", "ELSE", "UPDATE", "DELETE", "INSERT", "MERGE", "VALUES",
                        "RAISERROR", "THROW"})
_STOP_FWD = frozenset({"UNION", "EXCEPT", "INTERSECT", "INSERT", "UPDATE", "DELETE", "MERGE", "SET",
                       "IF", "WHILE", "RETURN", "DECLARE", "EXEC", "EXECUTE", "PRINT", "BEGIN",
                       "END", "ELSE", "SELECT"})
_CLAUSES = {"FROM": "FROM", "WHERE": "WHERE", "GROUP": "GROUP BY", "HAVING": "HAVING",
            "ORDER": "ORDER BY", "ON": "ON", "JOIN": "JOIN", "APPLY": "APPLY"}
_NOT_FUNCTION = frozenset({"IN", "EXISTS", "AS", "VALUES", "AND", "OR", "NOT", "WHERE", "ON",
                           "SELECT", "WHEN", "THEN", "ELSE", "RETURN", "IF", "WHILE", "INTO",
                           "TABLE", "APPLY", "JOIN", "FROM", "SET", "BY", "OVER", "EXEC",
                           "EXECUTE", "CASE", "HAVING"})

# (long suggestion, short suggestion for TODO markers)
SUGGESTIONS = {
    "A-RENAME": ("Replace the CLR name with {rep}; arguments unchanged.",
                 "rename to {rep}"),
    "A-SIGNATURE": ("Rewrite the call with the configured call_template (argument mapping); see preview.",
                    "apply call template"),
    "B-STRING_AGG-GROUPED": ("Inline STRING_AGG in the grouped query (see preview). Confirm whether "
                             "consumers depend on element order; if so set within_group_order_by.",
                             "inline STRING_AGG"),
    "B-STRING_AGG-SCALAR": ("Whole-result aggregation (no GROUP BY): inline STRING_AGG (see preview). "
                            "Same order/NULL review as the grouped case.",
                            "inline STRING_AGG"),
    "C-DISTINCT": ("STRING_AGG has no DISTINCT. De-duplicate in a derived table/CTE "
                   "(SELECT DISTINCT <group keys>, <value>) and run STRING_AGG over it.",
                   "DISTINCT argument: de-duplicate in a derived table, then STRING_AGG"),
    "C-WINDOW": ("STRING_AGG is not a window function. Replace <agg> OVER (PARTITION BY ...) with "
                 "OUTER APPLY (SELECT STRING_AGG(...) ... WHERE <partition match>) or pre-aggregate "
                 "and JOIN back.",
                 "OVER() window: use OUTER APPLY with STRING_AGG"),
    "C-CONTEXT": ("Call sits outside a query block STRING_AGG can occupy, or is malformed. Use the "
                  "TVP wrapper pattern: stage values into {tvp} and call {rep}.",
                  "stage values into {tvp} and call {rep}"),
    "C-ARITY": ("Argument count {n} does not match expected {exp}; map arguments by hand.",
                "argument count {n} does not match {exp}"),
    "C-DELIMITER": ("STRING_AGG separator must be a literal or variable; assign the delimiter "
                    "expression to a variable first.",
                    "hoist delimiter expression into a variable"),
    "C-CROSSDB": ("Cross-database / linked-server call ({db}). Deploy the replacement in the owning "
                  "database and confirm co-location on the RDS instance.",
                  "cross-database call ({db})"),
    "C-NO-MAPPING": ("No strategy configured for this CLR (strategy: manual). Add a rename, template or "
                     "string_agg mapping in config to automate.",
                     "no conversion mapping configured"),
    "D-DYNAMIC-SQL": ("CLR name appears inside a string literal (dynamic SQL, OBJECT_ID check, ...). "
                      "Edit the string builder by hand; capture and test a runtime-generated statement.",
                      "CLR name inside string literal - edit dynamic SQL by hand"),
    "N-COMMENT": ("Comment-only mention; no functional change needed.", "comment mention"),
}


@dataclass
class Target:
    schema: str
    name: str
    clr_type: str = "?"
    param_count: int | None = None
    synonyms: list[tuple[str, str]] = field(default_factory=list)
    mapping: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.name}".casefold()

    @property
    def display(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def strategy(self) -> str:
        return self.mapping.get("strategy", "manual")


@dataclass
class Site:
    target_key: str
    target: str
    alias: str
    kind: str                 # call | reference | dynamic_sql | comment
    line: int
    col: int
    span_start: int           # full span (whole call incl. parentheses)
    span_end: int
    name_start: int           # multi-part name only
    name_end: int
    parts: list[str] = field(default_factory=list)
    arg_spans: list[tuple[int, int]] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    db_part: str | None = None
    server_part: str | None = None
    malformed: bool = False
    distinct: bool = False
    over: bool = False
    select_scope: bool = False
    group_by: bool = False
    clause: str | None = None
    nested_in: str | None = None
    klass: str = ""
    auto: bool = False
    suggestion: str = ""
    short: str = ""
    snippet: str = ""


@dataclass
class Header:
    create_end: int
    has_or_alter: bool
    is_alter: bool
    kind: str
    name_parts: list[str]
    name_start: int
    name_end: int


@dataclass
class Analysis:
    tokens: list[Token]
    sig: list[Token]
    sites: list[Site]
    lex_errors: list[str]
    unbalanced: int
    header: Header | None


def parse_multipart(sig: list[Token], i: int) -> tuple[list[str], list[int | None], int]:
    """Parse server.db.schema.name starting at sig[i]. Empty parts (db..obj) -> ''."""
    parts: list[str] = [sig[i].value]
    idxs: list[int | None] = [i]
    j, L = i + 1, len(sig)
    while j < L and sig[j].is_op("."):
        if j + 1 < L and sig[j + 1].kind in ("IDENT", "QIDENT"):
            parts.append(sig[j + 1].value)
            idxs.append(j + 1)
            j += 2
        elif j + 1 < L and sig[j + 1].is_op("."):
            parts.append("")
            idxs.append(None)
            j += 1
        else:
            break
    return parts, idxs, j


def iter_multipart_names(sig: list[Token]):
    """Yield (parts, first_idx, last_idx) for every multi-part identifier."""
    i, L = 0, len(sig)
    while i < L:
        if sig[i].kind in ("IDENT", "QIDENT"):
            parts, idxs, j = parse_multipart(sig, i)
            yield parts, idxs[0], idxs[-1]
            i = j
        else:
            i += 1


def name_parts(text: str) -> list[str]:
    toks, _ = tokenize(text or "")
    sig = significant(toks)
    if not sig or sig[0].kind not in ("IDENT", "QIDENT"):
        return []
    return parse_multipart(sig, 0)[0]


def referenced_names(text: str, quoted_identifier: bool = True) -> set[tuple[str, str]]:
    """All schema-qualified (schema, name) pairs referenced in a definition, casefolded."""
    toks, _ = tokenize(text, quoted_identifier)
    out = set()
    for parts, _, _ in iter_multipart_names(significant(toks)):
        if len(parts) >= 2 and parts[-1]:
            out.add(((parts[-2] or "dbo").casefold(), parts[-1].casefold()))
    return out


def parse_header(sig: list[Token]) -> Header | None:
    if not sig:
        return None
    t = sig[0]
    if t.upper not in ("CREATE", "ALTER"):
        return None
    L, j, has_or = len(sig), 1, False
    if t.upper == "CREATE" and j + 1 < L and sig[j].upper == "OR" and sig[j + 1].upper == "ALTER":
        has_or, j = True, j + 2
    if j + 1 < L and sig[j].upper in ("PROC", "PROCEDURE", "FUNCTION", "VIEW", "TRIGGER") \
            and sig[j + 1].kind in ("IDENT", "QIDENT"):
        parts, idxs, _ = parse_multipart(sig, j + 1)
        if None in idxs:
            return None
        return Header(t.end, has_or, t.upper == "ALTER", sig[j].upper, parts,
                      sig[idxs[0]].start, sig[idxs[-1]].end)
    return None


class Analyzer:
    def __init__(self, targets: list[Target], case_sensitive: bool = False, current_db: str | None = None):
        self.norm = (lambda s: s) if case_sensitive else (lambda s: s.casefold())
        self.targets = {t.key: t for t in targets}
        self.current_db = current_db
        self.qualified: dict[tuple[str, str], tuple[Target, str]] = {}
        self.unqualified: dict[str, list[tuple[Target, str]]] = {}
        names = set()
        for t in targets:
            aliases = [(t.schema, t.name, "object")] + [(s, n, f"synonym {s}.{n}") for s, n in t.synonyms]
            for sch, nm, label in aliases:
                self.qualified[(self.norm(sch), self.norm(nm))] = (t, label)
                self.unqualified.setdefault(self.norm(nm), []).append((t, label))
                names.add(re.escape(nm))
        self.text_rx = None
        if names:
            alt = "|".join(sorted(names, key=len, reverse=True))
            flags = 0 if case_sensitive else re.IGNORECASE
            self.text_rx = re.compile(rf"(?<![\w@#$])\[?(?:{alt})\]?(?![\w@#$])", flags)

    # ------------------------------------------------------------------ public
    def analyze(self, text: str, quoted_identifier: bool = True) -> Analysis:
        tokens, lex_errors = tokenize(text, quoted_identifier)
        sig = significant(tokens)
        depth, match, unbalanced = paren_structure(sig)
        case_end, case_else = case_blocks(sig)
        ctx = _Ctx(text, sig, depth, match, case_end, case_else)
        sites: list[Site] = []

        i, L = 0, len(sig)
        while i < L:
            if sig[i].kind in ("IDENT", "QIDENT"):
                parts, idxs, j = parse_multipart(sig, i)
                hit = self._match(parts, idxs, sig)
                if hit:
                    sites.append(self._build_site(ctx, hit, parts, idxs))
                i = j
            else:
                i += 1

        if self.text_rx is not None:
            for tok in tokens:
                if tok.kind not in ("STRING", "LCOMMENT", "BCOMMENT"):
                    continue
                seen = set()
                for m in self.text_rx.finditer(tok.text):
                    for tgt, label in self.unqualified.get(self.norm(m.group(0).strip("[]")), []):
                        if tgt.key in seen:
                            continue
                        seen.add(tgt.key)
                        line = tok.line + tok.text.count("\n", 0, m.start())
                        sites.append(Site(
                            target_key=tgt.key, target=tgt.display, alias=label,
                            kind="dynamic_sql" if tok.kind == "STRING" else "comment",
                            line=line, col=tok.col, span_start=tok.start, span_end=tok.end,
                            name_start=tok.start, name_end=tok.end))

        sites.sort(key=lambda s: (s.span_start, s.span_end))
        for s in sites:
            s.snippet = _line_text(text, s.span_start if s.kind in ("call", "reference")
                                   else s.span_start + 0)
            self._classify(s, self.targets[s.target_key])
        return Analysis(tokens, sig, sites, lex_errors, unbalanced, parse_header(sig))

    # ----------------------------------------------------------------- matching
    def _match(self, parts, idxs, sig):
        if len(parts) > 4 or not parts[-1]:
            return None
        name = self.norm(parts[-1])
        if len(parts) >= 2:
            return self.qualified.get((self.norm(parts[-2] or "dbo"), name))
        cands = self.unqualified.get(name)
        if not cands:
            return None
        first, L = idxs[0], len(sig)
        prev = sig[first - 1].upper if first > 0 else ""
        exec_assign = (first >= 3 and sig[first - 1].is_op("=") and sig[first - 2].kind == "VAR"
                       and sig[first - 3].upper in ("EXEC", "EXECUTE"))
        nxt_paren = first + 1 < L and sig[first + 1].is_op("(")
        for tgt, label in cands:
            # Scalar UDFs and aggregates must be schema-qualified in T-SQL, so an
            # unqualified hit is only credible for procs (after EXEC) and TVFs.
            if tgt.clr_type == "PC" and (prev in ("EXEC", "EXECUTE") or exec_assign):
                return tgt, label
            if tgt.clr_type == "FT" and nxt_paren:
                return tgt, label
        return None

    def _build_site(self, ctx: "_Ctx", hit, parts, idxs) -> Site:
        sig, L = ctx.sig, len(ctx.sig)
        tgt, label = hit
        first, last = idxs[0], idxs[-1]
        s = Site(target_key=tgt.key, target=tgt.display, alias=label, kind="reference",
                 line=sig[last].line, col=sig[first].col,
                 span_start=sig[first].start, span_end=sig[last].end,
                 name_start=sig[first].start, name_end=sig[last].end, parts=parts)
        s.db_part = parts[-3] if len(parts) >= 3 else None
        s.server_part = parts[-4] if len(parts) == 4 else None

        nx = last + 1
        if nx < L and sig[nx].is_op("("):
            s.kind = "call"
            close = ctx.match.get(nx)
            if close is None:
                s.malformed = True
            else:
                s.span_end = sig[close].end
                inner = ctx.depth[nx] + 1
                spans, st = [], sig[nx].end
                for k in range(nx + 1, close):
                    if ctx.depth[k] == inner and sig[k].is_op(","):
                        spans.append((st, sig[k].start))
                        st = sig[k].end
                spans.append((st, sig[close].start))
                if close == nx + 1:
                    spans = []
                s.arg_spans = spans
                s.args = [ctx.text[a:b].strip() for a, b in spans]
                s.distinct = close > nx + 1 and sig[nx + 1].upper == "DISTINCT"
                s.over = close + 1 < L and sig[close + 1].upper == "OVER"

        d0 = ctx.depth[first]
        for k in range(first - 1, -1, -1):
            if sig[k].is_op("(") and ctx.depth[k] < d0:
                after = sig[k + 1] if k + 1 < L else None
                prev = sig[k - 1] if k > 0 else None
                if (after is None or after.upper != "SELECT") and prev is not None \
                        and prev.kind in ("IDENT", "QIDENT") and prev.upper not in _NOT_FUNCTION:
                    s.nested_in = prev.text
                break

        scope, s.clause = _find_select_scope(ctx, first)
        s.select_scope = scope is not None
        if scope is not None:
            s.group_by = _has_group_by(ctx, scope)
        return s

    # ----------------------------------------------------------- classification
    def _classify(self, s: Site, tgt: Target) -> None:
        m, strat = tgt.mapping, tgt.strategy
        fmt = {"rep": m.get("replacement_object") or "<replacement>",
               "tvp": m.get("tvp_type") or "<TVP type>",
               "n": len(s.args), "exp": "?", "db": ""}

        def done(klass: str, auto: bool, **extra):
            fmt.update(extra)
            long_, short = SUGGESTIONS[klass]
            s.klass, s.auto = klass, auto
            s.suggestion, s.short = long_.format(**fmt), short.format(**fmt)

        if s.kind == "comment":
            return done("N-COMMENT", False)
        if s.kind == "dynamic_sql":
            return done("D-DYNAMIC-SQL", False)
        if s.malformed:
            return done("C-CONTEXT", False)
        if s.server_part or (s.db_part and self.current_db
                             and self.norm(s.db_part) != self.norm(self.current_db)):
            return done("C-CROSSDB", False, db=".".join(p for p in s.parts[:-2] if p))
        if strat == "manual":
            return done("C-NO-MAPPING", False)

        if strat == "string_agg":
            sa = m.get("string_agg", {})
            exp = tgt.param_count if tgt.param_count is not None else None
            if s.kind != "call" or not s.select_scope:
                return done("C-CONTEXT", False)
            if s.distinct:
                return done("C-DISTINCT", False)
            if s.over:
                return done("C-WINDOW", False)
            if not s.args or (exp is not None and len(s.args) != exp):
                return done("C-ARITY", False, exp=exp if exp is not None else ">=1")
            dfa = sa.get("delimiter_from_arg")
            if dfa is not None and (int(dfa) >= len(s.args)
                                    or not LITERAL_OR_VAR.match(s.args[int(dfa)])):
                return done("C-DELIMITER", False)
            return done("B-STRING_AGG-GROUPED" if s.group_by else "B-STRING_AGG-SCALAR", True)

        if strat == "rename":
            if s.kind == "call" and tgt.param_count is not None and len(s.args) > tgt.param_count:
                return done("C-ARITY", False, exp=tgt.param_count)
            return done("A-RENAME", True)

        if strat == "template":
            if s.kind != "call":
                return done("C-CONTEXT", False)
            exp = m.get("expected_arg_count", tgt.param_count)
            idx = [int(x) for x in PLACEHOLDER.findall(m["call_template"]) if x.isdigit()]
            need = max(idx) + 1 if idx else 0
            if (exp is not None and len(s.args) != int(exp)) or need > len(s.args):
                return done("C-ARITY", False, exp=exp if exp is not None else need)
            return done("A-SIGNATURE", True)

        return done("C-NO-MAPPING", False)


@dataclass
class _Ctx:
    text: str
    sig: list[Token]
    depth: list[int]
    match: dict[int, int]
    case_end: set[int]
    case_else: set[int]


def _find_select_scope(ctx: _Ctx, first: int) -> tuple[int | None, str | None]:
    """Walk backwards to the SELECT that owns the expression at sig[first]."""
    cur, clause = ctx.depth[first], None
    for k in range(first - 1, -1, -1):
        t, d = ctx.sig[k], ctx.depth[k]
        if t.is_op("(") and d < cur:
            cur = d
            continue
        if d != cur:
            continue
        if t.is_op(";"):
            return None, clause
        u = t.upper
        if not u:
            continue
        if u == "SELECT":
            return k, clause or "SELECT"
        if u in _CLAUSES and clause is None:
            clause = _CLAUSES[u]
            continue
        if u in _STOP_BACK:
            if (u == "END" and k in ctx.case_end) or (u == "ELSE" and k in ctx.case_else):
                continue
            return None, clause or u
    return None, clause


def _has_group_by(ctx: _Ctx, scope: int) -> bool:
    sd, sig, L = ctx.depth[scope], ctx.sig, len(ctx.sig)
    for m in range(scope + 1, L):
        t, d = sig[m], ctx.depth[m]
        if d < sd:
            return False
        if d > sd:
            continue
        if t.is_op(";"):
            return False
        u = t.upper
        if not u:
            continue
        if u == "GROUP" and m + 1 < L and sig[m + 1].upper == "BY":
            return True
        if u in _STOP_FWD:
            if (u == "END" and m in ctx.case_end) or (u == "ELSE" and m in ctx.case_else):
                continue
            return False
    return False


def _line_text(text: str, pos: int) -> str:
    ls = text.rfind("\n", 0, pos) + 1
    le = text.find("\n", pos)
    return text[ls: le if le != -1 else len(text)].strip()[:240]


def overall_class(klasses: list[str]) -> str:
    return max(klasses, key=lambda k: CLASS_RANK.get(k[0], 0)) if klasses else "N-NONE"
