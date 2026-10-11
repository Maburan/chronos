"""``python_expression`` task type: evaluates a single, tightly
restricted arithmetic/comparison expression against the task's own
``payload["variables"]``.

**This is deliberately NOT general-purpose code execution.** It never
calls ``eval``/``exec``, never imports anything, never runs a subprocess
or shell command. Instead it parses the expression with ``ast.parse(...,
mode="eval")`` (which already rejects statements, imports, function
defs, loops, and assignments at the syntax level) and then walks the
resulting AST through ``_safe_eval``, an explicit whitelist of allowed
node types:

  - literal constants (numbers, strings, bools, ``None``)
  - variable lookups, restricted to names present in ``variables``
  - the arithmetic/comparison/boolean operators in ``_ALLOWED_*`` below
  - calls to a fixed, small whitelist of pure functions (``min``, ``max``,
    ``abs``, ``round``) -- no other callable, and no keyword arguments

Anything else -- attribute access (``os.system``), subscripting,
comprehensions, lambdas, calls to anything not on the whitelist -- is
rejected by the walker's fallback case, since the walker only recognizes
node types it explicitly whitelists (a whitelist, not a blacklist, is
what makes this safe against new AST node types Chronos didn't think of).

If a future need genuinely requires more (loops, custom functions,
external I/O), that is a different, much higher-risk task type and a
deliberate decision to make later -- not something to grow this one into.
"""

from __future__ import annotations

import ast
import logging
import operator

from db.models import TaskRecord
from worker.tasks.errors import InvalidTaskPayloadError

logger = logging.getLogger(__name__)

PYTHON_EXPRESSION_TASK_TYPE = "python_expression"

_MAX_EXPRESSION_LENGTH = 500

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}
_ALLOWED_COMPARE = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}
_ALLOWED_BOOLOPS = {
    ast.And: lambda a, b: a and b,
    ast.Or: lambda a, b: a or b,
}
_ALLOWED_FUNCTIONS = {"min": min, "max": max, "abs": abs, "round": round}


def _safe_eval(node: ast.AST, variables: dict):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, variables)

    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, (int, float, bool, str)):
            return node.value
        raise InvalidTaskPayloadError(f"unsupported constant type: {type(node.value).__name__}")

    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise InvalidTaskPayloadError(f"unknown variable: {node.id!r}")
        return variables[node.id]

    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _safe_eval(node.left, variables)
        right = _safe_eval(node.right, variables)
        return _ALLOWED_BINOPS[type(node.op)](left, right)

    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand, variables))

    if isinstance(node, ast.BoolOp) and type(node.op) in _ALLOWED_BOOLOPS:
        combine = _ALLOWED_BOOLOPS[type(node.op)]
        values = [_safe_eval(v, variables) for v in node.values]
        result = values[0]
        for value in values[1:]:
            result = combine(result, value)
        return result

    if isinstance(node, ast.Compare):
        left = _safe_eval(node.left, variables)
        for op, comparator in zip(node.ops, node.comparators):
            if type(op) not in _ALLOWED_COMPARE:
                raise InvalidTaskPayloadError(
                    f"unsupported comparison operator: {type(op).__name__}"
                )
            right = _safe_eval(comparator, variables)
            if not _ALLOWED_COMPARE[type(op)](left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
            raise InvalidTaskPayloadError(
                "only a fixed whitelist of functions may be called: "
                + ", ".join(sorted(_ALLOWED_FUNCTIONS))
            )
        if node.keywords:
            raise InvalidTaskPayloadError("keyword arguments are not supported")
        args = [_safe_eval(a, variables) for a in node.args]
        return _ALLOWED_FUNCTIONS[node.func.id](*args)

    raise InvalidTaskPayloadError(f"unsupported expression syntax: {type(node).__name__}")


def python_expression_task(record: TaskRecord) -> None:
    payload = record.payload or {}

    expression = payload.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise InvalidTaskPayloadError(
            "python_expression task requires a non-empty 'expression' string"
        )
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise InvalidTaskPayloadError(
            f"expression exceeds the maximum allowed length ({_MAX_EXPRESSION_LENGTH})"
        )

    variables = payload.get("variables", {})
    if not isinstance(variables, dict):
        raise InvalidTaskPayloadError("'variables' must be an object")
    for name, value in variables.items():
        if value is not None and not isinstance(value, (int, float, bool, str)):
            raise InvalidTaskPayloadError(f"variable {name!r} has an unsupported type")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise InvalidTaskPayloadError(f"invalid expression syntax: {exc}") from exc

    result = _safe_eval(tree, variables)
    logger.info(
        "python_expression_task: task_id=%s expression=%r result=%r",
        record.id,
        expression,
        result,
    )
