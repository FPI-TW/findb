"""Count SQL statements so Admin reads keep a bounded, data-independent cost.

An Admin read polled by the Dashboard must not issue more statements as rows,
days, members or schedulers grow: every request repeats that cost. Tests build
a small and a larger fixture, count what one call issues, and require both the
same count and a fixed ceiling.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event


class StatementLog(list[str]):
    """Statements captured while the context was open, in execution order."""

    def summary(self) -> str:
        return "\n".join(" ".join(statement.split())[:160] for statement in self)


@contextmanager
def count_statements(db) -> Iterator[StatementLog]:
    """Capture every statement the session's engine sends while open."""
    statements = StatementLog()

    def capture(conn, cursor, statement, params, context, many):
        statements.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def assert_within_budget(small: StatementLog, large: StatementLog, *, ceiling: int) -> None:
    """The larger fixture issues the same statements as the small one, within the ceiling."""
    assert len(large) == len(small), (
        f"statement count grows with data: {len(small)} -> {len(large)}\n{large.summary()}"
    )
    assert len(large) <= ceiling, (
        f"{len(large)} statements exceed the budget of {ceiling}\n{large.summary()}"
    )
