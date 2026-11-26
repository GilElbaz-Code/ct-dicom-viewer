"""S3-compatible source storage. Only sealed object references reach workers and viewers."""

import hashlib
import io
from functools import cache
from typing import Any
from uuid import UUID

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings
from app.services.errors import InvalidInput
from app.upload_models import UploadFileSpec


@cache
def client(*, public: bool = False) -> BaseClient:
    return boto3.client(
        service_name="s3",
        endpoint_url=settings.s3_public_endpoint if public else settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 2},
        ),
    )


def object_key(*, upload_id: UUID, index: int, sealed: bool) -> str:
    return f"{'sealed' if sealed else 'incoming'}/{upload_id}/{index:04d}.dcm"


def upload_form(*, upload_id: UUID, index: int, file: UploadFileSpec, expires: int) -> dict[str, Any]:
    return client(public=True).generate_presigned_post(
        Bucket=settings.s3_bucket,
        Key=object_key(upload_id=upload_id, index=index, sealed=False),
        Fields={"x-amz-meta-sha256": file.sha256},
        Conditions=[{"x-amz-meta-sha256": file.sha256}, ["content-length-range", file.size, file.size]],
        ExpiresIn=expires,
    )


def is_uploaded(*, upload_id: UUID, index: int, file: UploadFileSpec) -> bool:
    try:
        head = client().head_object(
            Bucket=settings.s3_bucket, Key=object_key(upload_id=upload_id, index=index, sealed=False)
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return head["ContentLength"] == file.size and head.get("Metadata", {}).get("sha256") == file.sha256


def seal_file(*, upload_id: UUID, index: int, file: UploadFileSpec) -> None:
    source_key = object_key(upload_id=upload_id, index=index, sealed=False)
    try:
        response = client().get_object(Bucket=settings.s3_bucket, Key=source_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey"}:
            raise InvalidInput(f"Upload is incomplete: {file.name}") from exc
        raise
    digest = hashlib.sha256()
    size = 0
    body = response["Body"]
    try:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            size += len(chunk)
            if size > file.size:
                raise InvalidInput(f"Unexpected file size: {file.name}")
            digest.update(chunk)
    finally:
        body.close()
    if size != file.size or digest.hexdigest() != file.sha256:
        raise InvalidInput(f"File size or SHA-256 checksum mismatch: {file.name}")
    # Copy exactly the object whose bytes were verified; an overwrite racing this call must fail.
    client().copy_object(
        Bucket=settings.s3_bucket,
        Key=object_key(upload_id=upload_id, index=index, sealed=True),
        CopySource={"Bucket": settings.s3_bucket, "Key": source_key},
        CopySourceIfMatch=response["ETag"],
    )


def read_object(*, reference: str) -> io.BytesIO:
    prefix = f"s3://{settings.s3_bucket}/sealed/"
    if not reference.startswith(prefix):
        raise InvalidInput("Invalid sealed source reference")
    key = reference.removeprefix(f"s3://{settings.s3_bucket}/")
    response = client().get_object(Bucket=settings.s3_bucket, Key=key)
    body = response["Body"]
    try:
        # Uploads enforce a 64 MiB per-file limit; never load a full volume here.
        return io.BytesIO(body.read())
    finally:
        body.close()


def delete_prefix(*, prefix: str) -> None:
    paginator = client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            result = client().delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": objects})
            if result.get("Errors"):
                raise RuntimeError("Object storage did not delete all expired upload files")


def initialize() -> None:
    """One-shot deployment command, not a side effect of importing the application."""
    try:
        client().head_bucket(Bucket=settings.s3_bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"404", "NoSuchBucket"}:
            raise
        client().create_bucket(Bucket=settings.s3_bucket)
    try:
        client().put_bucket_cors(
            Bucket=settings.s3_bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": settings.upload_origins,
                        "AllowedMethods": ["POST"],
                        "AllowedHeaders": ["*"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )
    except ClientError as exc:
        # MinIO has no bucket CORS API; it reads MINIO_API_CORS_ALLOW_ORIGIN (set in docker-compose.yml).
        if exc.response["Error"]["Code"] != "NotImplemented":
            raise


if __name__ == "__main__":
    initialize()
