from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ForbiddenError
from app.services.game_access import get_authorized_user_or_none


def _repo(user):
    repo = AsyncMock()
    repo.read_by_column.return_value = user
    return repo


@pytest.mark.asyncio
async def test_get_authorized_user_or_none_returns_none_for_unknown_user():
    repo = _repo(None)

    result = await get_authorized_user_or_none(repo, "ghost", api_key="key-1")

    assert result is None
    repo.read_by_column.assert_awaited_once_with(
        "externalUserId", "ghost", not_found_raise_exception=False
    )


@pytest.mark.asyncio
async def test_get_authorized_user_or_none_returns_owned_user():
    user = SimpleNamespace(apiKey_used="key-1")

    result = await get_authorized_user_or_none(_repo(user), "u", api_key="key-1")

    assert result is user


@pytest.mark.asyncio
async def test_get_authorized_user_or_none_forbids_other_callers_user():
    user = SimpleNamespace(apiKey_used="other-key")

    with pytest.raises(ForbiddenError):
        await get_authorized_user_or_none(_repo(user), "u", api_key="key-1")
