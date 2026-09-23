"""Loss-less T-SQL tokenizer.

Every character of the input belongs to exactly one token, so edits made by
character offset preserve formatting, comments and line endings byte-for-byte.

Handles the constructs that break regex-based tools: nested block comments,
N'...' strings with '' escapes, [bracketed]] identifiers], "quoted" identifiers
(QUOTED_IDENTIFIER aware), @variables, #temp names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TRIVIA = frozenset({"WS", "LCOMMENT", "BCOMMENT"})

_WS = re.compile(r"\s+")
_WORD = re.compile(r"(?:[^\W\d]|#)[\w@#$]*")
_VAR = re.compile(r"@@?[\w@#$]*")
_NUM = re.compile(r"0[xX][0-9A-Fa-f]*|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")

_BEGIN_NO_END = frozenset({"TRAN", "TRANSACTION", "DISTRIBUTED", "DIALOG", "CONVERSATION"})


@dataclass(slots=True)
class Token:
    kind: str      # WS LCOMMENT BCOMMENT STRING QIDENT IDENT VAR NUMBER OP
    text: str
    start: int
    end: int
    line: int
    col: int

    @property
    def upper(self) -> str:
        """Upper-cased text for bare identifiers/keywords, '' otherwise."""
        return self.text.upper() if self.kind == "IDENT" else ""

    @property
    def value(self) -> str:
        """Identifier value with delimiters removed and escapes resolved."""
        if self.kind == "QIDENT":
            if self.text.startswith("["):
                return self.text[1:-1].replace("]]", "]")
            return self.text[1:-1].replace('""', '"')
        return self.text

    def is_op(self, ch: str) -> bool:
        return self.kind == "OP" and self.text == ch


def _scan_delimited(sql: str, pos: int, close: str) -> tuple[int, bool]:
    """pos is the index of the opening delimiter. Returns (end, terminated)."""
    i, n = pos + 1, len(sql)
    while True:
        j = sql.find(close, i)
        if j == -1:
            return n, False
        if j + 1 < n and sql[j + 1] == close:   # doubled delimiter = escape
            i = j + 2
            continue
        return j + 1, True


def tokenize(sql: str, quoted_identifier: bool = True) -> tuple[list[Token], list[str]]:
    tokens: list[Token] = []
    errors: list[str] = []
    i, n, line, line_start = 0, len(sql), 1, 0
    while i < n:
        c, start = sql[i], i
        if c.isspace():
            i = _WS.match(sql, i).end()
            kind = "WS"
        elif c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            kind = "LCOMMENT"
        elif c == "/" and sql.startswith("/*", i):
            depth = 0
            while i < n:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                else:
                    i += 1
            if depth > 0:
                errors.append(f"Unterminated block comment starting line {line}")
            kind = "BCOMMENT"
        elif c in "nN" and i + 1 < n and sql[i + 1] == "'":
            i, ok = _scan_delimited(sql, i + 1, "'")
            kind = "STRING"
            if not ok:
                errors.append(f"Unterminated string literal starting line {line}")
        elif c == "'":
            i, ok = _scan_delimited(sql, i, "'")
            kind = "STRING"
            if not ok:
                errors.append(f"Unterminated string literal starting line {line}")
        elif c == "[":
            i, ok = _scan_delimited(sql, i, "]")
            kind = "QIDENT"
            if not ok:
                errors.append(f"Unterminated [identifier] starting line {line}")
        elif c == '"':
            i, ok = _scan_delimited(sql, i, '"')
            kind = "QIDENT" if quoted_identifier else "STRING"
            if not ok:
                errors.append(f'Unterminated "quoted" token starting line {line}')
        elif c == "@":
            i = _VAR.match(sql, i).end()
            kind = "VAR"
        else:
            m = _WORD.match(sql, i)
            if m:
                i, kind = m.end(), "IDENT"
            else:
                m = _NUM.match(sql, i)
                if m and m.end() > i:
                    i, kind = m.end(), "NUMBER"
                else:
                    i, kind = i + 1, "OP"
        text = sql[start:i]
        tokens.append(Token(kind, text, start, i, line, start - line_start + 1))
        nl = text.count("\n")
        if nl:
            line += nl
            line_start = start + text.rfind("\n") + 1
    return tokens, errors


def significant(tokens: list[Token]) -> list[Token]:
    return [t for t in tokens if t.kind not in TRIVIA]


def paren_structure(sig: list[Token]) -> tuple[list[int], dict[int, int], int]:
    """depth[i] = nesting level token i sits at ('(' and ')' carry the outer level).
    match maps '(' index -> ')' index. Returns count of unbalanced parens."""
    depth: list[int] = []
    match: dict[int, int] = {}
    stack: list[int] = []
    d = unbalanced = 0
    for idx, t in enumerate(sig):
        if t.is_op("("):
            depth.append(d)
            stack.append(idx)
            d += 1
        elif t.is_op(")"):
            if stack:
                match[stack.pop()] = idx
                d -= 1
            else:
                unbalanced += 1
            depth.append(d)
        else:
            depth.append(d)
    return depth, match, unbalanced + len(stack)


def case_blocks(sig: list[Token]) -> tuple[set[int], set[int]]:
    """Indices of END / ELSE tokens that belong to CASE expressions (not
    BEGIN...END or IF...ELSE), so statement-boundary logic can skip them."""
    stack: list[str] = []
    case_end: set[int] = set()
    case_else: set[int] = set()
    L = len(sig)
    for i, t in enumerate(sig):
        u = t.upper
        if not u:
            continue
        nxt = sig[i + 1].upper if i + 1 < L else ""
        if u == "CASE":
            stack.append("CASE")
        elif u == "BEGIN":
            if nxt not in _BEGIN_NO_END:
                stack.append("BEGIN")
        elif u == "END":
            if nxt == "CONVERSATION":
                continue
            if stack and stack.pop() == "CASE":
                case_end.add(i)
        elif u == "ELSE" and stack and stack[-1] == "CASE":
            case_else.add(i)
    return case_end, case_else
