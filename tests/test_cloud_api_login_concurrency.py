"""Concurrent public cloud calls must share one authentication request."""

import asyncio

from custom_components.narwal.narwal_client.cloud_api import NarwalCloudApi


class _Response:
    def __init__(self, body: object) -> None:
        self._body = body

    async def __aenter__(self) -> _Response:
        # Yield to the event loop so concurrent callers really interleave.
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self, *, content_type: object = None) -> object:
        del content_type
        return self._body


class _Session:
    def __init__(self) -> None:
        self.login_calls = 0
        self.device_calls = 0

    def request(self, method: str, url: str, **_kwargs: object) -> _Response:
        if url.endswith("/loginByPw"):
            self.login_calls += 1
            return _Response({"code": "0", "result": {"token": "token", "uuid": "uuid"}})
        self.device_calls += 1
        return _Response({"code": "0", "result": {"deviceInfoList": []}})


def test_concurrent_device_queries_login_once() -> None:
    """Parallel discovery callers do not overwrite each other's cloud token."""
    session = _Session()
    api = NarwalCloudApi(session, phone="13800000000", password="secret")  # type: ignore[arg-type]

    async def query() -> None:
        await asyncio.gather(api.devices(), api.devices())

    asyncio.run(query())

    assert session.login_calls == 1
    assert session.device_calls == 2
