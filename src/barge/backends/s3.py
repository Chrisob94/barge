from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any, BinaryIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import BotoCoreError, ClientError

from .. import paths
from .base import Backend, BackendError, Entry, NotFound, ProgressCallback, sort_entries

MB = 1024 * 1024
TRANSFER_CONFIG = TransferConfig(multipart_threshold=16 * MB, multipart_chunksize=16 * MB)
S3_ERRORS = (ClientError, BotoCoreError)


class _Body(io.RawIOBase):
    # botocore's StreamingBody isn't a real file object; this makes it one
    def __init__(self, body: Any) -> None:
        self._body = body

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self._body.read(len(b))
        b[: len(data)] = data
        return len(data)

    def close(self) -> None:
        self._body.close()
        super().close()


def _translate(e: Exception, path: str) -> BackendError:
    if not isinstance(e, ClientError):
        return BackendError(f"S3: {e}")
    err = e.response.get("Error", {})
    code = err.get("Code", "")
    if code in ("404", "NoSuchKey", "NotFound"):
        return NotFound(path)
    if code == "NoSuchBucket":
        return BackendError(f"Bucket {err.get('BucketName', '')!r} doesn't exist")
    if code in ("403", "AccessDenied"):
        return BackendError(f"Access denied: {path}")
    return BackendError(f"S3 {code}: {err.get('Message', '')}")


class S3Backend(Backend):
    """A bucket, or a prefix inside one. Folders are just '/' in key names."""

    scheme = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
        aws_profile: str | None = None,
        client: Any = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            session = boto3.Session(profile_name=aws_profile, region_name=region)
            client = session.client("s3", endpoint_url=endpoint_url)
        self.client = client

    @property
    def label(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}".rstrip("/")

    def _key(self, path: str) -> str:
        rel = paths.normalize(path).lstrip("/")
        if self.prefix and rel:
            return f"{self.prefix}/{rel}"
        return self.prefix or rel

    def _dir_prefix(self, path: str) -> str:
        key = self._key(path)
        return key + "/" if key else ""

    def _path(self, key: str) -> str:
        key = key.rstrip("/")
        if self.prefix:
            key = key[len(self.prefix) :]
        return paths.normalize(key)

    def _pages(self, prefix: str, delimiter: str | None = None) -> Iterator[dict[str, Any]]:
        # list_objects_v2 tops out at 1000 keys per response
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
        if delimiter:
            kwargs["Delimiter"] = delimiter
        try:
            yield from self.client.get_paginator("list_objects_v2").paginate(**kwargs)
        except S3_ERRORS as e:
            raise _translate(e, prefix) from e

    def _objects(self, prefix: str) -> Iterator[dict[str, Any]]:
        for page in self._pages(prefix):
            yield from page.get("Contents", [])

    def _first_keys(self, prefix: str, n: int) -> list[str]:
        try:
            resp = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=n)
        except S3_ERRORS as e:
            raise _translate(e, prefix) from e
        return [o["Key"] for o in resp.get("Contents", [])]

    def list_dir(self, path: str) -> list[Entry]:
        prefix = self._dir_prefix(path)
        entries: list[Entry] = []
        has_marker = False
        for page in self._pages(prefix, delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                entries.append(Entry(self._path(cp["Prefix"]), is_dir=True))
            for obj in page.get("Contents", []):
                if obj["Key"] == prefix:
                    has_marker = True  # empty folder made by mkdir
                    continue
                entries.append(Entry(self._path(obj["Key"]), False, obj["Size"], obj["LastModified"]))

        if not entries and not has_marker and paths.normalize(path) != "/":
            raise NotFound(path)
        return sort_entries(entries)

    def stat(self, path: str) -> Entry:
        path = paths.normalize(path)
        if path == "/":
            return Entry("/", is_dir=True)
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._key(path))
            return Entry(path, False, head["ContentLength"], head["LastModified"])
        except S3_ERRORS as e:
            err = _translate(e, path)
            if not isinstance(err, NotFound):
                raise err from e
        if self._first_keys(self._dir_prefix(path), 1):
            return Entry(path, is_dir=True)
        raise NotFound(path)

    def open_read(self, path: str) -> BinaryIO:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(path))
        except S3_ERRORS as e:
            raise _translate(e, path) from e
        return io.BufferedReader(_Body(obj["Body"]))  # type: ignore[return-value]

    def write(
        self,
        path: str,
        src: BinaryIO,
        *,
        size: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        try:
            self.client.upload_fileobj(
                src, self.bucket, self._key(path), Callback=progress, Config=TRANSFER_CONFIG
            )
        except S3_ERRORS as e:
            raise _translate(e, path) from e

    def delete(self, path: str) -> None:
        self.stat(path)  # S3 "deletes" missing keys without complaint
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(path))
        except S3_ERRORS as e:
            raise _translate(e, path) from e

    def rmdir(self, path: str) -> None:
        prefix = self._dir_prefix(path)
        if any(k != prefix for k in self._first_keys(prefix, 2)):
            raise BackendError(f"{path} isn't empty")
        try:
            self.client.delete_object(Bucket=self.bucket, Key=prefix)
        except S3_ERRORS as e:
            raise _translate(e, path) from e

    def mkdir(self, path: str) -> None:
        if paths.normalize(path) == "/":
            return
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._dir_prefix(path), Body=b"")
        except S3_ERRORS as e:
            raise _translate(e, path) from e

    def rename(self, src: str, dst: str) -> None:
        if self.exists(dst):
            raise BackendError(f"{dst} already exists")
        if not self.stat(src).is_dir:
            self._copy(self._key(src), self._key(dst))
            self.client.delete_object(Bucket=self.bucket, Key=self._key(src))
            return
        # there's no rename for prefixes, so copy everything and delete the old tree
        for child in self.walk(src):
            if not child.is_dir:
                rel = paths.relative_to(child.path, src)
                self._copy(self._key(child.path), self._key(paths.join(dst, rel)))
        self.mkdir(dst)
        self.delete_tree(src)

    def _copy(self, src_key: str, dst_key: str) -> None:
        try:
            # managed copy, so objects over 5 GB work too
            self.client.copy(
                {"Bucket": self.bucket, "Key": src_key}, self.bucket, dst_key, Config=TRANSFER_CONFIG
            )
        except S3_ERRORS as e:
            raise _translate(e, src_key) from e

    def walk(self, path: str) -> Iterator[Entry]:
        # one flat listing instead of a request per folder
        root = paths.normalize(path)
        prefix = self._dir_prefix(path)
        seen: set[str] = set()
        for obj in self._objects(prefix):
            key = obj["Key"]
            if key == prefix:
                continue
            p = self._path(key)
            parts = paths.relative_to(p, root).split("/")
            for i in range(1, len(parts)):
                d = paths.join(root, *parts[:i])
                if d not in seen:
                    seen.add(d)
                    yield Entry(d, is_dir=True)
            if key.endswith("/"):
                if p not in seen:
                    seen.add(p)
                    yield Entry(p, is_dir=True)
            else:
                yield Entry(p, False, obj["Size"], obj["LastModified"])

    def delete_tree(self, path: str) -> None:
        prefix = self._dir_prefix(path)
        if not prefix:
            raise BackendError("Refusing to delete the whole bucket")

        batch: list[dict[str, str]] = []
        total = 0
        for obj in self._objects(prefix):
            batch.append({"Key": obj["Key"]})
            if len(batch) == 1000:  # delete_objects takes at most 1000 keys
                self._delete_batch(batch, path)
                total += len(batch)
                batch = []
        if batch:
            self._delete_batch(batch, path)
            total += len(batch)
        if total == 0:
            raise NotFound(path)

    def _delete_batch(self, batch: list[dict[str, str]], path: str) -> None:
        try:
            resp = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})
        except S3_ERRORS as e:
            raise _translate(e, path) from e
        if errors := resp.get("Errors"):
            first = errors[0]
            msg = f"{first['Key']}: {first.get('Message', '')}"
            raise BackendError(f"{len(errors)} objects weren't deleted ({msg})")
