from typing import Any, Callable, Optional, OrderedDict, Sequence, TypeVar

from snuba.datasets.dataset import Dataset
from snuba.query.expressions import (
    Argument,
    Column,
    Expression,
    Lambda,
    Literal,
    FunctionCall,
)
from snuba.query.conditions import (
    binary_condition,
    BooleanFunctions,
    OPERATOR_TO_FUNCTION,
)
from snuba.query.parser.functions import parse_function_to_expr
from snuba.query.schema import POSITIVE_OPERATORS
from snuba.util import is_condition, is_function, QUOTED_LITERAL_RE


TExpression = TypeVar("TExpression")


class InvalidConditionException(Exception):
    pass


def parse_conditions(
    operand_builder: Callable[[Any], TExpression],
    and_builder: Callable[[Sequence[TExpression]], Optional[TExpression]],
    or_builder: Callable[[Sequence[TExpression]], Optional[TExpression]],
    unpack_array_condition_builder: Callable[[TExpression, str, Any], TExpression],
    simple_condition_builder: Callable[[TExpression, str, Any], TExpression],
    dataset: Dataset,
    conditions: Any,
    array_join: Optional[str],
    depth: int = 0,
) -> Optional[TExpression]:
    """
    Return a boolean expression suitable for putting in the WHERE clause of the
    query.  The expression is constructed by ANDing groups of OR expressions.
    Expansion of columns is handled, as is replacement of columns with aliases,
    if the column has already been expanded and aliased elsewhere.

    operand_builder: Builds the TExpression representing the left hand side
      of a simple condition. This can be as nested as the user wants
    and_builder / or_builder: Combine a list of expressions in AND/OR
    unpack_array_condition_builder: Deals with a special case where we unpack conditions
      on array columns. More details in the code.
    simple_condition_builder: Generates a simple condition made by expression on the
      left hand side, an operator and a literal on the right hand side.
    """
    from snuba.clickhouse.columns import Array

    if not conditions:
        return None

    if depth == 0:
        # dedupe conditions at top level, but keep them in order
        sub = OrderedDict(
            (
                parse_conditions(
                    operand_builder,
                    and_builder,
                    or_builder,
                    unpack_array_condition_builder,
                    simple_condition_builder,
                    dataset,
                    cond,
                    array_join,
                    depth + 1,
                ),
                None,
            )
            for cond in conditions
        )
        return and_builder([s for s in sub.keys() if s])
    elif is_condition(conditions):
        lhs, op, lit = dataset.process_condition(conditions)

        # facilitate deduping IN conditions by sorting them.
        if op in ("IN", "NOT IN") and isinstance(lit, tuple):
            lit = tuple(sorted(lit))

        # If the LHS is a simple column name that refers to an array column
        # (and we are not arrayJoining on that column, which would make it
        # scalar again) and the RHS is a scalar value, we assume that the user
        # actually means to check if any (or all) items in the array match the
        # predicate, so we return an `any(x == value for x in array_column)`
        # type expression. We assume that operators looking for a specific value
        # (IN, =, LIKE) are looking for rows where any array value matches, and
        # exclusionary operators (NOT IN, NOT LIKE, !=) are looking for rows
        # where all elements match (eg. all NOT LIKE 'foo').
        columns = dataset.get_dataset_schemas().get_read_schema().get_columns()
        if (
            isinstance(lhs, str)
            and lhs in columns
            and isinstance(columns[lhs].type, Array)
            and columns[lhs].base_name != array_join
            and not isinstance(lit, (list, tuple))
        ):
            return unpack_array_condition_builder(operand_builder(lhs), op, lit)
        else:
            return simple_condition_builder(operand_builder(lhs), op, lit)

    elif depth == 1:
        sub = (
            parse_conditions(
                operand_builder,
                and_builder,
                or_builder,
                unpack_array_condition_builder,
                simple_condition_builder,
                dataset,
                cond,
                array_join,
                depth + 1,
            )
            for cond in conditions
        )
        return or_builder([s for s in sub if s])
    else:
        raise InvalidConditionException(str(conditions))


def parse_conditions_to_expr(
    expr: Sequence[Any], dataset: Dataset, arrayjoin: Optional[str]
) -> Optional[Expression]:
    """
    Relies on parse_conditions to parse a list of conditions into an Expression.
    """

    def operand_builder(val: Any) -> Expression:
        if is_function(val, 0):
            return parse_function_to_expr(val)
        # TODO: This will use the schema of the dataset to decide
        # if the expression is a column or a literal.
        if QUOTED_LITERAL_RE.match(val):
            return Literal(None, val[1:-1])
        else:
            return Column(None, val, None)

    def multi_expression_builder(
        expressions: Sequence[Expression], function: str
    ) -> Expression:
        assert len(expressions) > 0
        if len(expressions) == 1:
            return expressions[0]

        return binary_condition(
            None,
            function,
            expressions[0],
            multi_expression_builder(expressions[1:], function),
        )

    def and_builder(expressions: Sequence[Expression]) -> Optional[Expression]:
        if not expressions:
            return None
        return multi_expression_builder(expressions, BooleanFunctions.AND)

    def or_builder(expressions: Sequence[Expression]) -> Optional[Expression]:
        if not expressions:
            return None
        return multi_expression_builder(expressions, BooleanFunctions.OR)

    def preprocess_literal(op: str, literal: Any) -> Expression:
        """
        Replaces lists with a function call to tuple.
        """
        if isinstance(literal, (list, tuple)):
            assert op in ["IN", "NOT IN"]
            literals = tuple([Literal(None, l) for l in literal])
            return FunctionCall(None, "tuple", literals)
        else:
            assert op not in ["IN", "NOT IN"]
            return Literal(None, literal)

    def unpack_array_condition_builder(
        lhs: Expression, op: str, literal: Any
    ) -> Expression:
        function_name = "arrayExists" if op in POSITIVE_OPERATORS else "arrayAll"

        # This is an expresison like:
        # arrayExists(x -> assumeNotNull(notLike(x, rhs)), lhs)
        return FunctionCall(
            None,
            function_name,
            (
                Lambda(
                    None,
                    ("x",),
                    FunctionCall(
                        None,
                        "assumeNotNull",
                        (
                            FunctionCall(
                                None,
                                OPERATOR_TO_FUNCTION[op],
                                (Argument(None, "x"), preprocess_literal(op, literal)),
                            ),
                        ),
                    ),
                ),
                lhs,
            ),
        )

    def simple_condition_builder(lhs: Expression, op: str, literal: Any) -> Expression:
        return binary_condition(
            None, OPERATOR_TO_FUNCTION[op], lhs, preprocess_literal(op, literal)
        )

    return parse_conditions(
        operand_builder,
        and_builder,
        or_builder,
        unpack_array_condition_builder,
        simple_condition_builder,
        dataset,
        expr,
        arrayjoin,
        0,
    )