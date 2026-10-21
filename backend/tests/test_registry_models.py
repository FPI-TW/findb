"""Registry model schema invariants not covered by the ORM/migration drift test.

Alembic autogenerate does not compare partial-index predicates, so the active-outbox
uniqueness predicate is asserted here.
"""

from app.models.registry import NormalizationOutbox


def test_normalization_outbox_has_one_active_generation_per_job() -> None:
    index = next(
        index
        for index in NormalizationOutbox.__table__.indexes
        if index.name == "uq_normalization_outbox_active_job"
    )

    assert index.unique is True
    assert str(index.dialect_options["postgresql"]["where"]) == (
        "status IN ('pending', 'publishing')"
    )
