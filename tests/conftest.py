import pytest


@pytest.fixture(params=[True, False], ids=["expire", "no-expire"])
def expire_on_commit(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)
