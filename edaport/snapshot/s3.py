"""S3 backend for snapshots: the local store is the working copy, S3 the shared remote (like a git remote).

Objects keep their content address (<prefix>/objects/<sha256 hex>), so push/pull skip what already exists.
Needs boto3 (`pip install edaport[s3]`); credentials come from the usual AWS environment.
"""

import json
from urllib.parse import urlparse

from .store import Store, StoreError


def _remote(url: str):
    import boto3
    u = urlparse(url)
    if u.scheme != "s3" or not u.netloc:
        raise StoreError(f"expected s3://bucket/prefix, got {url!r}")
    return boto3.client("s3"), u.netloc, u.path.strip("/")


def _key(prefix: str, digest: str) -> str:
    return f"{prefix}/objects/{digest[7:]}".lstrip("/")


def push(store: Store, ref: str, url: str) -> int:
    """Upload the manifest of `ref` and every blob it names. Returns the number of objects uploaded."""
    s3, bucket, prefix = _remote(url)
    ref = store.resolve(ref)
    have = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=_key(prefix, "sha256:")):
        have.update(o["Key"] for o in page.get("Contents", []))
    sent = 0
    for digest in {ref, *store.manifest(ref)["files"].values()}:
        if _key(prefix, digest) not in have:
            s3.put_object(Bucket=bucket, Key=_key(prefix, digest), Body=store.get_blob(digest))
            sent += 1
    return sent


def pull(store: Store, ref: str, url: str) -> int:
    """Download `ref` (a sha256 design_ref) and its blobs; each object is verified against its digest."""
    s3, bucket, prefix = _remote(url)

    def fetch(digest: str) -> bytes:
        data = s3.get_object(Bucket=bucket, Key=_key(prefix, digest))["Body"].read()
        if store.put_blob(data) != digest:
            raise StoreError(f"remote object {digest} is corrupt")
        return data

    got = 0
    manifest = json.loads(store.get_blob(ref) if store.has(ref) else fetch(ref))
    for digest in set(manifest["files"].values()):
        if not store.has(digest):
            fetch(digest)
            got += 1
    return got
