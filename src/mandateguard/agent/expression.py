"""A deliberately tiny expression language for compiled policy rules.

Every rule in `policy/mandate_policy.yaml` carries an `expression` string that has to be
true for the mandate to comply with that clause. Those strings are written by an LLM
reading the RBI circular (T4.1). That is the whole reason this module exists.

**Why not `eval`.** `eval` on a string an LLM produced, in a process that also holds a
payments book, is not a shortcut -- it is arbitrary code execution with a regulator's name
on it. `eval("__import__('os').system('...')")` is a valid Python expression. So is
`eval("open('/etc/passwd').read()")`. Neither is a valid policy rule.

**Why not a regex or a JSON predicate tree.** A regex over the string cannot tell
`amount_inr <= 15000` from `amount_inr <= 15000 or True`, and a JSON tree is unreadable in
a diff -- and the diff is the human-in-the-loop review step that T4.1 is built around. A
one-line Python-shaped expression is the only form that a reviewer can check against a
clause at a glance.

So the string is *parsed* with `ast.parse` and then walked by this module against a
whitelist of node types. What survives the whitelist:

* boolean operators (`and`, `or`, `not`)
* comparisons (`==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `is`, `is not`)
* names, which must exist in the evaluation context
* literal constants, and literal tuples/lists/sets of them

What does not, and therefore cannot appear in a rule at all: **function calls**, attribute
access, subscripts, comprehensions, lambdas, assignment expressions, f-strings, arithmetic.
No call node means no `__import__`, no `open`, no `getattr` -- the class of attack is
removed rather than filtered. Arithmetic is excluded for a different reason: a rule that
computes is a rule that has drifted from the clause it cites, and every threshold in this
circular is a literal number in the text.

The cost of that austerity is real. `pre_debit_notice_fields` cannot be checked with
`all(...)`; the rule has to spell out five `in` comparisons and the YAML gets longer. That
is the trade accepted here: verbose and reviewable beats terse and executable.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

__all__ = [
    "ExpressionError",
    "UnknownFieldError",
    "evaluate",
    "evaluate_tracing",
    "parse",
    "referenced_names",
]


class ExpressionError(ValueError):
    """The expression is not a legal policy expression, or could not be evaluated."""


class UnknownFieldError(ExpressionError):
    """The expression names a field the evaluation context does not define.

    Separate from `ExpressionError` because the two have different owners: a general
    expression error is a malformed rule, while this one usually means the rule and the
    audit context drifted apart -- a field was renamed on one side only.
    """


_COMPARATORS: dict[type[ast.cmpop], Any] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def parse(expression: str) -> ast.expr:
    """Parse and whitelist-check an expression, returning its root node.

    Raises `ExpressionError` on anything the grammar above does not allow. Called at load
    time by `policy/loader.py`, so a rule with an illegal expression fails when the policy
    file is read rather than when the rule first fires on a real mandate.
    """
    if not expression.strip():
        raise ExpressionError("empty expression")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"{expression!r} is not parseable: {exc}") from exc
    _check(tree.body, expression)
    return tree.body


def referenced_names(expression: str) -> set[str]:
    """Every field name the expression reads.

    The loader compares this against the audit context's declared fields, which turns a
    typo in a rule -- `amount_in` for `amount_inr` -- into a load-time failure instead of a
    rule that silently never fires.
    """
    return {node.id for node in ast.walk(parse(expression)) if isinstance(node, ast.Name)}


def evaluate(expression: str, context: Mapping[str, Any]) -> bool:
    """Evaluate a checked expression against a context, returning a strict bool.

    The return type is enforced rather than coerced. `amount_inr` on its own is a legal
    expression that would evaluate truthily for every non-zero amount, and a rule that
    passes because a rupee figure is non-zero is worse than a rule that fails loudly.
    """
    return evaluate_tracing(expression, context)[0]


def evaluate_tracing(expression: str, context: Mapping[str, Any]) -> tuple[bool, set[str]]:
    """Evaluate, and also report which fields were *actually read* getting there.

    Not the same set as `referenced_names`, and the difference decides verdicts.
    `referenced_names` is syntax: every name that appears in the string. This is what the
    evaluation touched, and `and`/`or` short-circuit, so:

        is_variable_amount and customer_cap_inr is not None

    reads both names for a variable mandate and only the first for a fixed one.

    The auditor uses this to decide whether a field the extraction could not determine
    matters. Using the syntactic set instead makes every fixed-amount mandate depend on
    `customer_cap_inr` -- a field that decides nothing for it -- and the auditor abstains on
    mandates it could have graded. That is not a safe direction to be wrong in: it shows up
    in T4.7 as abstain *precision* collapsing, and in production as a review queue full of
    mandates nobody needed to look at.
    """
    seen: set[str] = set()
    value = _eval(parse(expression), context, expression, seen)
    if not isinstance(value, bool):
        raise ExpressionError(
            f"{expression!r} evaluated to {value!r} ({type(value).__name__}), not a bool. "
            "A policy rule has to answer yes or no; a truthy value that happens to be "
            "non-empty is not an answer."
        )
    return value, seen


# --------------------------------------------------------------------------------
# Whitelist check.
# --------------------------------------------------------------------------------


def _check(node: ast.AST, expression: str) -> None:
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _check(value, expression)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.Not):
            raise _rejected(node, expression, "only `not` is allowed as a unary operator")
        _check(node.operand, expression)
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _COMPARATORS:
                raise _rejected(node, expression, f"comparison {type(op).__name__} is not allowed")
        _check(node.left, expression)
        for comparator in node.comparators:
            _check(comparator, expression)
    elif isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise _rejected(node, expression, "names may only be read")
    elif isinstance(node, ast.Constant):
        return
    elif isinstance(node, ast.Tuple | ast.List | ast.Set):
        for element in node.elts:
            if not isinstance(element, ast.Constant):
                raise _rejected(node, expression, "collection literals may only contain constants")
    else:
        raise _rejected(node, expression, f"{type(node).__name__} is not allowed")


def _rejected(node: ast.AST, expression: str, why: str) -> ExpressionError:
    return ExpressionError(
        f"{expression!r} is not a legal policy expression: {why}. Legal expressions use "
        "only and/or/not, comparisons, field names, literals and literal collections -- "
        "no calls, no attributes, no arithmetic. See agent/expression.py."
    )


# --------------------------------------------------------------------------------
# Evaluation.
# --------------------------------------------------------------------------------


def _eval(node: ast.AST, context: Mapping[str, Any], expression: str, seen: set[str]) -> Any:
    if isinstance(node, ast.BoolOp):
        # Short-circuiting is load-bearing, not an optimisation: rules are written as
        # `x is not None and x >= 24`, and evaluating the right half of that against None
        # would raise where the rule intends "does not apply".
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = _eval(value, context, expression, seen)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = _eval(value, context, expression, seen)
            if result:
                return result
        return result

    if isinstance(node, ast.UnaryOp):
        return not _eval(node.operand, context, expression, seen)

    if isinstance(node, ast.Compare):
        left = _eval(node.left, context, expression, seen)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = _eval(right_node, context, expression, seen)
            try:
                outcome = _COMPARATORS[type(op)](left, right)
            except TypeError as exc:
                raise ExpressionError(
                    f"{expression!r} compared {left!r} with {right!r} and Python refused: "
                    f"{exc}. A rule comparing a missing value is a rule whose guard is "
                    "wrong -- add the `is not None` half to its `applies_when`."
                ) from exc
            if not outcome:
                return False
            left = right
        return True

    if isinstance(node, ast.Name):
        seen.add(node.id)
        if node.id not in context:
            raise UnknownFieldError(
                f"{expression!r} reads {node.id!r}, which the audit context does not "
                f"define. Known fields: {', '.join(sorted(context))}."
            )
        return context[node.id]

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        values = [_eval(element, context, expression, seen) for element in node.elts]
        return set(values) if isinstance(node, ast.Set) else tuple(values)

    raise _rejected(node, expression, f"{type(node).__name__} is not allowed")
