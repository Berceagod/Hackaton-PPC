"""S3 (MinIO) adapter. Streams in 5 MB parts via multipart upload: RAM stays bounded."""

import asyncio
import logging
from typing import AsyncIterator

import aioboto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from storage.base import CHUNK, ObjectNotFound, StorageError

log = logging.getLogger("filegrid.storage.s3")

PART_SIZE = 5 * 1024 * 1024  # S3 minimum part size


class S3Storage:
    name = "s3"

    def __init__(
        self, endpoint: str, access_key: str, secret_key: str, bucket: str
    ) -> None:
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._session = aioboto3.Session()

    def _client(self):
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 2},
                connect_timeout=5,
                read_timeout=60,
            ),
        )

    async def ensure_ready(self, attempts: int = 30, delay: float = 2.0) -> None:
        """Create the bucket if missing; wait for MinIO to come up."""
        last: Exception | None = None
        for _ in range(attempts):
            try:
                async with self._client() as s3:
                    try:
                        await s3.head_bucket(Bucket=self._bucket)
                    except ClientError:
                        await s3.create_bucket(Bucket=self._bucket)
                return
            except (BotoCoreError, ClientError, OSError) as exc:
                last = exc
                await asyncio.sleep(delay)
        raise StorageError(f"S3 not reachable: {last}")

    async def put_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        size = 0
        buf = bytearray()
        upload_id: str | None = None
        parts: list[dict] = []
        try:
            async with self._client() as s3:
                try:
                    async for chunk in chunks:
                        buf += chunk
                        size += len(chunk)
                        while len(buf) >= PART_SIZE:
                            part = bytes(buf[:PART_SIZE])
                            del buf[:PART_SIZE]
                            if upload_id is None:
                                resp = await s3.create_multipart_upload(
                                    Bucket=self._bucket, Key=key
                                )
                                upload_id = resp["UploadId"]
                            parts.append(
                                await self._upload_part(
                                    s3, key, upload_id, len(parts) + 1, part
                                )
                            )
                    if upload_id is None:
                        await s3.put_object(
                            Bucket=self._bucket, Key=key, Body=bytes(buf)
                        )
                    else:
                        if buf:
                            parts.append(
                                await self._upload_part(
                                    s3, key, upload_id, len(parts) + 1, bytes(buf)
                                )
                            )
                        await s3.complete_multipart_upload(
                            Bucket=self._bucket,
                            Key=key,
                            UploadId=upload_id,
                            MultipartUpload={"Parts": parts},
                        )
                except BaseException:
                    if upload_id is not None:
                        try:
                            await s3.abort_multipart_upload(
                                Bucket=self._bucket, Key=key, UploadId=upload_id
                            )
                        except Exception:  # best effort cleanup
                            log.warning("abort multipart failed", extra={"key": key})
                    raise
        except (BotoCoreError, ClientError, OSError) as exc:
            raise StorageError(f"S3 put failed: {exc}") from exc
        return size

    async def _upload_part(self, s3, key, upload_id, number, body: bytes) -> dict:
        resp = await s3.upload_part(
            Bucket=self._bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=number,
            Body=body,
        )
        return {"ETag": resp["ETag"], "PartNumber": number}

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        try:
            async with self._client() as s3:
                try:
                    resp = await s3.get_object(Bucket=self._bucket, Key=key)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") in (
                        "NoSuchKey",
                        "404",
                    ):
                        raise ObjectNotFound(key) from exc
                    raise
                async for chunk in resp["Body"].iter_chunks(CHUNK):
                    yield chunk
        except (BotoCoreError, ClientError, OSError) as exc:
            raise StorageError(f"S3 get failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            async with self._client() as s3:
                await s3.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError, OSError) as exc:
            raise StorageError(f"S3 delete failed: {exc}") from exc

    async def healthy(self) -> bool:
        try:
            async with self._client() as s3:
                try:
                    await s3.head_bucket(Bucket=self._bucket)
                except ClientError:
                    await s3.create_bucket(Bucket=self._bucket)  # MinIO came up after us
            return True
        except (BotoCoreError, ClientError, OSError):
            return False
