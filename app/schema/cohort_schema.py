"""Validated cohort tag types shared by request schemas."""

from typing import Annotated, List

from pydantic import AfterValidator, Field, StringConstraints

from app.model.cohorts import COHORT_PATTERN, MAX_COHORTS_PER_REQUEST

CohortTag = Annotated[str, StringConstraints(pattern=COHORT_PATTERN)]


def _dedupe(tags: List[str]) -> List[str]:
    """Drop repeated tags, keeping the first occurrence's position."""
    return list(dict.fromkeys(tags))


CohortList = Annotated[
    List[CohortTag],
    Field(max_length=MAX_COHORTS_PER_REQUEST),
    AfterValidator(_dedupe),
]
