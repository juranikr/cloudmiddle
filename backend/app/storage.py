"""S3 이미지 업로드/조회 (로컬에서는 비활성 → 501)."""

from __future__ import annotations

import uuid
from typing import Optional

from app.config import settings


def s3_enabled() -> bool:
    return bool(settings.s3_bucket and settings.aws_region)


def _client():
    import boto3

    kwargs = {"region_name": settings.aws_region}
    return boto3.client("s3", **kwargs)


def build_object_key(place_id: int, filename: str, content_type: str) -> str:
    ext = "jpg"
    if "png" in content_type:
        ext = "png"
    elif "webp" in content_type:
        ext = "webp"
    elif "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()[:8]
    return f"places/{place_id}/{uuid.uuid4().hex}.{ext}"


def presign_put(key: str, content_type: str, expires: int = 900) -> str:
    return _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def public_url(key: str) -> str:
    if settings.s3_public_base_url:
        return f"{settings.s3_public_base_url.rstrip('/')}/{key}"
    return f"https://{settings.s3_bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"


def delete_object(key: str) -> None:
    if not s3_enabled():
        return
    _client().delete_object(Bucket=settings.s3_bucket, Key=key)
