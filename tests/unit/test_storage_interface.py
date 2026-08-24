"""The Storage contract, exercised through an in-memory fake.

This is the payoff of depending on the interface rather than on boto3: the
contract is testable with no MinIO, no network and no credentials.
"""

from __future__ import annotations

import pytest

from app.storage.base import ObjectNotFoundError, Storage, StoredObject


class InMemoryStorage(Storage):
    """A complete, honest implementation of the contract, backed by a dict."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}
        self.ready = False

    async def ensure_ready(self) -> None:
        self.ready = True

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        self._objects[key] = (data, content_type)
        return StoredObject(key=key, size=len(data), content_type=content_type, etag="fake")

    async def get(self, key: str) -> bytes:
        if key not in self._objects:
            raise ObjectNotFoundError(key)
        return self._objects[key][0]

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._objects

    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        return f"https://example.test/{key}?expires={expires_in or 900}"

    async def ping(self) -> bool:
        return True


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


async def test_put_then_get_round_trips(storage: InMemoryStorage) -> None:
    payload = b"hello \xc3\xa9 world"
    stored = await storage.put("a/b.txt", payload, content_type="text/plain")

    assert stored.key == "a/b.txt"
    assert stored.size == len(payload)
    assert stored.content_type == "text/plain"
    assert await storage.get("a/b.txt") == payload


async def test_get_missing_raises_object_not_found(storage: InMemoryStorage) -> None:
    with pytest.raises(ObjectNotFoundError):
        await storage.get("nope")


async def test_exists_reflects_put_and_delete(storage: InMemoryStorage) -> None:
    assert await storage.exists("k") is False
    await storage.put("k", b"x")
    assert await storage.exists("k") is True
    await storage.delete("k")
    assert await storage.exists("k") is False


async def test_deleting_a_missing_key_is_not_an_error(storage: InMemoryStorage) -> None:
    await storage.delete("never-existed")  # must not raise


async def test_put_overwrites(storage: InMemoryStorage) -> None:
    await storage.put("k", b"first")
    await storage.put("k", b"second")
    assert await storage.get("k") == b"second"


async def test_presigned_url_includes_key(storage: InMemoryStorage) -> None:
    url = await storage.presigned_url("dir/file.bin", expires_in=60)
    assert "dir/file.bin" in url
    assert "expires=60" in url


async def test_ensure_ready_is_idempotent(storage: InMemoryStorage) -> None:
    await storage.ensure_ready()
    await storage.ensure_ready()
    assert storage.ready is True


def test_storage_cannot_be_instantiated_directly() -> None:
    """The ABC must stay abstract, or a half-built adapter could slip through."""
    with pytest.raises(TypeError):
        Storage()  # type: ignore[abstract]


def test_incomplete_adapter_is_rejected() -> None:
    class Incomplete(Storage):
        async def put(  # type: ignore[override]
            self, key: str, data: bytes, **kwargs: object
        ) -> StoredObject:
            return StoredObject(key=key, size=0, content_type="")

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]
