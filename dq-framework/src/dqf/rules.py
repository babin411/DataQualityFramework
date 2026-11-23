"""Business rules: plain field conditions in the configuration, turned into Great Expectations.

A rule in config/dq_config.yaml says WHEN a row is in scope and THEN what must be true of it:

    rules:
      DECISION_MATCHES_SCORE:
        description: No auto-approval with a merchant fraud score below 400
        applies_to: UW_A1_SCORED
        when: {DECISION: AUTO_APPROVE}
        then: {MF_SCORE: {at_least: 400}}

Nobody writes SQL. Each `then` field becomes a built-in GX expectation, and `when` becomes its
`row_condition` (GX 1.x "Restrict an Expectation to specific rows"), so GX generates and runs the SQL
inside Snowflake and reports how many of the in-scope rows break the rule.

Logic that field conditions cannot express is a named custom GX expectation, written once by a
developer in dqf/expectations.py, and used by name:   check: ssn_tax_id_match_follows_derivation

The vocabulary
    when  (which rows the rule is about; several fields = all must match; a list of such blocks = any)
        FIELD: value              equal to                     FIELD: [a, b]        one of
        FIELD: present / missing  has a value / is empty
        FIELD: {not: v}  {not_in: [a, b]}  {at_least: n}  {at_most: n}  {above: n}  {below: n}
               {between: [lo, hi]}
    then  (what must be true; several fields = each must hold)
        FIELD: value / [a, b]     must be that value / one of them
        FIELD: present / missing  must have a value / must be empty
        FIELD: {not: v}  {not_in: [...]}  {at_least: n}  {at_most: n}  {above: n}  {below: n}
               {between: [lo, hi]}  {matches: regex}  {not_matches: regex}
               {length_between: [lo, hi]}  {equals_field: OTHER_FIELD}
    Empty values are not judged by value conditions (as in GX); add `present` to require a value.

This module is plain Python until build() is called, so the configuration reader can use it to
validate rules without importing GX.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME_RE = re.compile(r"^[A-Za-z_][\w$]*$")
KEYWORDS = {"present": "not_null", "missing": "null"}
WHEN_OPS = {"is": "eq", "not": "ne", "in": "in", "not_in": "not_in", "at_least": "ge", "at_most": "le",
            "above": "gt", "below": "lt", "between": "between"}
THEN_OPS = {"is", "in", "not", "not_in", "at_least", "at_most", "above", "below", "between", "matches",
            "not_matches", "length_between", "equals_field", "present"}


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    name: str
    description: str
    applies_to: list[str]
    when: list[list[tuple]] = field(default_factory=list)   # OR of AND-blocks of (field, op, value)
    then: list[tuple] = field(default_factory=list)          # (field, kind, params)
    check: str | None = None                                 # a named custom expectation instead of `then`
    params: dict = field(default_factory=dict)               # `with:` arguments for the custom check
    max_bad_pct: float | None = None


# ---- reading a rule ------------------------------------------------------------------------------
def _value(v, where: str):
    if isinstance(v, bool):
        raise RuleError(f"{where}: write yes/no/true/false values in quotes ('Y', 'yes'): YAML reads them as booleans")
    if isinstance(v, (int, float, str)):
        return v
    if hasattr(v, "isoformat"):                              # a YAML date
        return v.isoformat()
    raise RuleError(f"{where}: '{v}' is not a single value")


def _values(v, where: str) -> list:
    items = v if isinstance(v, list) else [v]
    if not items:
        raise RuleError(f"{where}: needs at least one value")
    return [_value(x, where) for x in items]


def _pair(v, where: str) -> tuple:
    if not (isinstance(v, list) and len(v) == 2):
        raise RuleError(f"{where}: needs two values, [low, high]")
    lo, hi = (_number(x, where) for x in v)
    if lo > hi:
        raise RuleError(f"{where}: the low value is above the high value")
    return lo, hi


def _number(v, where: str):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RuleError(f"{where}: '{v}' is not a number")
    return v


def _field(name, where: str) -> str:
    if not _NAME_RE.match(str(name)):
        raise RuleError(f"{where}: '{name}' is not a valid column name")
    return str(name).upper()


def _when_block(block, where: str) -> list[tuple]:
    if not isinstance(block, dict) or not block:
        raise RuleError(f"{where}: must be field: condition pairs, e.g. {{DECISION: AUTO_APPROVE}}")
    out = []
    for name, spec in block.items():
        at = f"{where}.{name}"
        col = _field(name, at)
        if isinstance(spec, str) and spec.lower() in KEYWORDS:
            out.append((col, KEYWORDS[spec.lower()], None))
        elif isinstance(spec, list):
            out.append((col, "in", _values(spec, at)))
        elif isinstance(spec, dict):
            for op, value in spec.items():
                if op not in WHEN_OPS:
                    raise RuleError(f"{at}: unknown condition '{op}' (use {', '.join(WHEN_OPS)})")
                kind = WHEN_OPS[op]
                if kind == "between":
                    lo, hi = _pair(value, f"{at}.{op}")
                    out += [(col, "ge", lo), (col, "le", hi)]
                elif kind in ("in", "not_in"):
                    out.append((col, kind, _values(value, f"{at}.{op}")))
                elif kind in ("ge", "le", "gt", "lt"):
                    out.append((col, kind, _number(value, f"{at}.{op}")))
                else:
                    out.append((col, kind, _value(value, f"{at}.{op}")))
        else:
            out.append((col, "eq", _value(spec, at)))
    return out


def _then_parts(then, where: str) -> list[tuple]:
    if not isinstance(then, dict) or not then:
        raise RuleError(f"{where}: must be field: requirement pairs, e.g. {{MF_SCORE: {{at_least: 400}}}}")
    parts = []
    for name, spec in then.items():
        at = f"{where}.{name}"
        col = _field(name, at)
        if isinstance(spec, str) and spec.lower() in KEYWORDS:
            parts.append((col, "present" if spec.lower() == "present" else "missing", {}))
            continue
        if not isinstance(spec, dict):
            parts.append((col, "in", {"values": _values(spec, at)}))
            continue
        unknown = set(spec) - THEN_OPS
        if unknown:
            raise RuleError(f"{at}: unknown requirement {', '.join(sorted(unknown))} (use {', '.join(sorted(THEN_OPS))})")
        rng = {}
        for op, value in spec.items():
            w = f"{at}.{op}"
            if op in ("is", "in"):
                parts.append((col, "in", {"values": _values(value, w)}))
            elif op in ("not", "not_in"):
                parts.append((col, "not_in", {"values": _values(value, w)}))
            elif op in ("at_least", "above"):
                rng.update(min=_number(value, w), strict_min=op == "above")
            elif op in ("at_most", "below"):
                rng.update(max=_number(value, w), strict_max=op == "below")
            elif op == "between":
                rng["min"], rng["max"] = _pair(value, w)
            elif op in ("matches", "not_matches"):
                try:
                    re.compile(str(value))
                except re.error as exc:
                    raise RuleError(f"{w}: the pattern does not compile ({exc})") from exc
                parts.append((col, op, {"regex": str(value)}))
            elif op == "length_between":
                lo, hi = _pair(value, w)
                parts.append((col, "length", {"min": lo, "max": hi}))
            elif op == "equals_field":
                parts.append((col, "equals_field", {"other": _field(value, w)}))
            elif op == "present":
                parts.append((col, "present" if value else "missing", {}))
        if rng:
            parts.append((col, "range", rng))
    return parts


def parse_rule(name: str, body: dict, where: str, known_checks: set[str]) -> Rule:
    """One rule from the configuration, checked. Raises RuleError with where the problem is."""
    unknown = set(body) - {"description", "applies_to", "when", "then", "check", "with", "max_bad_pct", "alert"}
    if unknown:
        raise RuleError(f"{where}: unknown key(s) {', '.join(sorted(unknown))} "
                        "(allowed: description, applies_to, when, then, check, with, max_bad_pct, alert)")
    applies = body.get("applies_to")
    applies = [a.strip() for a in applies.split(",")] if isinstance(applies, str) else (applies or [])
    if not applies:
        raise RuleError(f"{where}: needs applies_to (the source or sources it checks)")
    has_then, check = body.get("then") not in (None, {}, ""), body.get("check")
    if has_then == bool(check):
        raise RuleError(f"{where}: needs either then (field requirements) or check (a named custom check)")
    if check and check not in known_checks:
        raise RuleError(f"{where}: unknown check '{check}' (known: {', '.join(sorted(known_checks)) or 'none'})")
    if body.get("with") and not check:
        raise RuleError(f"{where}: 'with' gives arguments to a named check; use it with 'check'")
    when = body.get("when") or []
    blocks = when if isinstance(when, list) else [when]
    pct = body.get("max_bad_pct")
    if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100):
        raise RuleError(f"{where}: max_bad_pct must be a number between 0 and 100")
    return Rule(
        name=name, description=str(body.get("description") or name).strip(),
        applies_to=[str(a).upper() for a in applies],
        when=[_when_block(b, f"{where}.when" + (f"[{i}]" if len(blocks) > 1 else "")) for i, b in enumerate(blocks, 1)],
        then=_then_parts(body["then"], f"{where}.then") if has_then else [],
        check=check, params=dict(body.get("with") or {}), max_bad_pct=pct)


# ---- words ---------------------------------------------------------------------------------------
_WHEN_WORDS = {"eq": "is", "ne": "is not", "in": "is one of", "not_in": "is not one of", "ge": "is at least",
               "le": "is at most", "gt": "is above", "lt": "is below"}


def _show(v) -> str:
    return ", ".join(map(str, v)) if isinstance(v, list) else str(v)


def when_text(rule: Rule) -> str:
    def one(col, op, v):
        return f"{col} is empty" if op == "null" else f"{col} has a value" if op == "not_null" \
            else f"{col} {_WHEN_WORDS[op]} {_show(v)}"
    return " or ".join("(" + " and ".join(one(*s) for s in b) + ")" if len(rule.when) > 1 and len(b) > 1
                       else " and ".join(one(*s) for s in b) for b in rule.when)


def part_text(part: tuple) -> str:
    col, kind, p = part
    if kind == "in":
        return f"{col} must be {_show(p['values'])}" if len(p["values"]) == 1 else f"{col} must be one of {_show(p['values'])}"
    if kind == "not_in":
        return f"{col} must not be {_show(p['values'])}"
    if kind == "range":
        lo = (f"above {p['min']}" if p.get("strict_min") else f"at least {p['min']}") if "min" in p else None
        hi = (f"below {p['max']}" if p.get("strict_max") else f"at most {p['max']}") if "max" in p else None
        return f"{col} must be " + " and ".join(x for x in (lo, hi) if x)
    if kind in ("present", "missing"):
        return f"{col} must " + ("have a value" if kind == "present" else "be empty")
    if kind in ("matches", "not_matches"):
        return f"{col} must {'match' if kind == 'matches' else 'not match'} {p['regex']}"
    if kind == "length":
        return f"{col} must be {p['min']} to {p['max']} characters long"
    return f"{col} must equal {p['other']}"


def text(rule: Rule, check_texts: dict | None = None) -> str:
    """The rule in words, e.g. 'when DECISION is AUTO_APPROVE: MF_SCORE must be at least 400'."""
    what = (check_texts or {}).get(rule.check, rule.check) if rule.check else "; ".join(part_text(p) for p in rule.then)
    return f"when {when_text(rule)}: {what}" if rule.when else what


# ---- GX ------------------------------------------------------------------------------------------
def row_condition(rule: Rule):
    if not rule.when:
        return None
    from great_expectations.expectations.row_conditions import Column

    def one(col, op, v):
        c = Column(col)
        return {"eq": lambda: c == v, "ne": lambda: c != v, "in": lambda: c.is_in(v), "not_in": lambda: c.is_not_in(v),
                "ge": lambda: c >= v, "le": lambda: c <= v, "gt": lambda: c > v, "lt": lambda: c < v,
                "null": c.is_null, "not_null": c.is_not_null}[op]()

    blocks = []
    for block in rule.when:
        cond = one(*block[0])
        for statement in block[1:]:
            cond = cond & one(*statement)
        blocks.append(cond)
    out = blocks[0]
    for b in blocks[1:]:
        out = out | b
    return out


def build(rule: Rule) -> list[tuple[str, object]]:
    """(label, GX expectation) for each part of the rule. A named check is one part."""
    import great_expectations as gx

    from dqf.expectations import CHECKS

    E = gx.expectations
    common = {"mostly": 1 - (rule.max_bad_pct or 0) / 100, "description": rule.description}
    cond = row_condition(rule)
    if cond is not None:
        common["row_condition"] = cond
    if rule.check:
        return [(rule.check, CHECKS[rule.check].cls(**rule.params, **common))]
    out = []
    for part in rule.then:
        col, kind, p = part
        if kind == "in":
            e = E.ExpectColumnValuesToBeInSet(column=col, value_set=p["values"], **common)
        elif kind == "not_in":
            e = E.ExpectColumnValuesToNotBeInSet(column=col, value_set=p["values"], **common)
        elif kind == "range":
            e = E.ExpectColumnValuesToBeBetween(column=col, min_value=p.get("min"), max_value=p.get("max"),
                                                strict_min=p.get("strict_min", False),
                                                strict_max=p.get("strict_max", False), **common)
        elif kind == "present":
            e = E.ExpectColumnValuesToNotBeNull(column=col, **common)
        elif kind == "missing":
            e = E.ExpectColumnValuesToBeNull(column=col, **common)
        elif kind == "matches":
            e = E.ExpectColumnValuesToMatchRegex(column=col, regex=p["regex"], **common)
        elif kind == "not_matches":
            e = E.ExpectColumnValuesToNotMatchRegex(column=col, regex=p["regex"], **common)
        elif kind == "length":
            e = E.ExpectColumnValueLengthsToBeBetween(column=col, min_value=p["min"], max_value=p["max"], **common)
        else:
            e = E.ExpectColumnPairValuesToBeEqual(column_A=col, column_B=p["other"], **common)
        out.append((part_text(part), e))
    return out


# ---- a query for a person to see the breaking rows -----------------------------------------------
def _lit(v) -> str:
    return str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"


def when_sql(rule: Rule) -> str | None:
    def one(col, op, v):
        if op in ("null", "not_null"):
            return f"{col} is {'not ' if op == 'not_null' else ''}null"
        if op in ("in", "not_in"):
            return f"{col} {'not ' if op == 'not_in' else ''}in ({', '.join(map(_lit, v))})"
        return f"{col} {dict(eq='=', ne='<>', ge='>=', le='<=', gt='>', lt='<')[op]} {_lit(v)}"
    if not rule.when:
        return None
    blocks = [" and ".join(one(*s) for s in b) for b in rule.when]
    return blocks[0] if len(blocks) == 1 else " or ".join(f"({b})" for b in blocks)


def part_breaks_sql(part: tuple) -> str:
    """True for a row breaking this part. Empty values only break present / missing, as in GX."""
    col, kind, p = part
    if kind == "in":
        return f"{col} not in ({', '.join(map(_lit, p['values']))})"
    if kind == "not_in":
        return f"{col} in ({', '.join(map(_lit, p['values']))})"
    if kind == "range":
        bits = []
        if "min" in p:
            bits.append(f"{col} {'<=' if p.get('strict_min') else '<'} {p['min']}")
        if "max" in p:
            bits.append(f"{col} {'>=' if p.get('strict_max') else '>'} {p['max']}")
        return "(" + " or ".join(bits) + ")"
    if kind == "present":
        return f"{col} is null"
    if kind == "missing":
        return f"{col} is not null"
    if kind in ("matches", "not_matches"):
        return f"{'not ' if kind == 'matches' else ''}regexp_like({col}::varchar, {_lit(p['regex'])})"
    if kind == "length":
        return f"length({col}::varchar) not between {p['min']} and {p['max']}"
    return f"{col} <> {p['other']}"


def breaks_sql(rule: Rule) -> str:
    """A condition true for the rows that break the rule (in scope and failing any part)."""
    from dqf.expectations import CHECKS

    broken = [CHECKS[rule.check].breaks_sql] if rule.check else [part_breaks_sql(p) for p in rule.then]
    broken_sql = broken[0] if len(broken) == 1 else "(" + " or ".join(broken) + ")"
    scope = when_sql(rule)
    return f"({scope}) and {broken_sql}" if scope else broken_sql
