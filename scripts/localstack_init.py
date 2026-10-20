#!/usr/bin/env python3
"""Idempotent LocalStack bootstrap for the Quac local end-to-end environment.

Creates exactly the resources Terraform provisions in AWS, mirrored locally:

* DynamoDB table ``quac-jobs`` (``job_id`` HASH key, ``owner_id-index`` GSI,
  on-demand billing)
* SQS FIFO queue ``quac-jobs.fifo`` + DLQ ``quac-jobs-dlq.fifo``
  (``maxReceiveCount=3`` redrive policy, SHA-256 dedup enabled by default)
* S3 bucket ``quac-results`` (AES256, full public access block)

Safe to run repeatedly; every call reconciles toward the desired state.
"""

from __future__ import annotations

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
TABLE_NAME = os.environ.get("QUAC_JOBS_TABLE", "quac-jobs")
QUEUE_NAME = os.environ.get("QUAC_QUEUE_NAME", "quac-jobs.fifo")
DLQ_NAME = os.environ.get("QUAC_DLQ_NAME", "quac-jobs-dlq.fifo")
BUCKET_NAME = os.environ.get("QUAC_RESULTS_BUCKET", "quac-results")
OWNER_GSI_NAME = "owner_id-index"

VISIBILITY_TIMEOUT = int(os.environ.get("QUAC_QUEUE_VISIBILITY", "60"))
MAX_RECEIVE_COUNT = int(os.environ.get("QUAC_MAX_RECEIVE_COUNT", "3"))

_TABLE_KEY_SCHEMA = [{"AttributeName": "job_id", "KeyType": "HASH"}]
_TABLE_ATTRIBUTE_DEFS = [
    {"AttributeName": "job_id", "AttributeType": "S"},
    {"AttributeName": "owner_id", "AttributeType": "S"},
    {"AttributeName": "created_at", "AttributeType": "S"},
]
_OWNER_GSI_SPEC = {
    "IndexName": OWNER_GSI_NAME,
    "KeySchema": [
        {"AttributeName": "owner_id", "KeyType": "HASH"},
        {"AttributeName": "created_at", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "ALL"},
}


def ensure_dynamodb(client) -> None:
    """Create the jobs table if absent (reconcile-only; never deletes)."""
    try:
        client.describe_table(TableName=TABLE_NAME)
        print(f"[init] dynamodb table already present: {TABLE_NAME}")
        _ensure_owner_gsi(client)
        return
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=_TABLE_KEY_SCHEMA,
        AttributeDefinitions=_TABLE_ATTRIBUTE_DEFS,
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[_OWNER_GSI_SPEC],
        Tags=[{"Key": "Project", "Value": "quac"}, {"Key": "Environment", "Value": "local"}],
    )
    waiter = client.get_waiter("table_exists")
    waiter.wait(TableName=TABLE_NAME, WaiterConfig={"Delay": 2, "MaxAttempts": 30})
    print(f"[init] dynamodb table created: {TABLE_NAME} (+ {OWNER_GSI_NAME} GSI)")


def _ensure_owner_gsi(client) -> None:
    """Add the owner index to a pre-existing table that lacks it."""
    description = client.describe_table(TableName=TABLE_NAME)["Table"]
    existing = {g.get("IndexName") for g in description.get("GlobalSecondaryIndexes", [])}
    if OWNER_GSI_NAME in existing:
        print(f"[init] dynamodb GSI already present: {OWNER_GSI_NAME}")
        return
    client.update_table(
        TableName=TABLE_NAME,
        AttributeDefinitions=_TABLE_ATTRIBUTE_DEFS,
        GlobalSecondaryIndexUpdates=[{"Create": _OWNER_GSI_SPEC}],
    )
    print(f"[init] dynamodb GSI added: {OWNER_GSI_NAME}")


def ensure_queue(client, name: str, *, fifo: bool) -> str:
    """Create a standard-or-FIFO queue if absent; return its URL."""
    params: dict = {"QueueName": name}
    if fifo:
        params["Attributes"] = {
            "FifoQueue": "true",
            "ContentBasedDeduplication": "false",  # we send explicit SHA-256 dedup IDs
            "DeduplicationScope": "messageGroup",
        }
    try:
        url = client.create_queue(**params)["QueueUrl"]
        print(f"[init] sqs queue created: {name}")
        return url
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "QueueAlreadyExists":
            raise
        url = client.get_queue_url(QueueName=name)["QueueUrl"]
        print(f"[init] sqs queue already present: {name}")
        return url


def ensure_dlq_then_queue(sqs) -> None:
    """Create DLQ first, then the main queue wired to it via redrive policy."""
    dlq_url = ensure_queue(sqs, DLQ_NAME, fifo=True)
    dlq_arn = sqs.get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    try:
        sqs.create_queue(
            QueueName=QUEUE_NAME,
            Attributes={
                "FifoQueue": "true",
                "ContentBasedDeduplication": "false",
                "DeduplicationScope": "messageGroup",
                "MessageRetentionPeriod": "1209600",  # 14 days, like terraform/sqs.tf
                "VisibilityTimeout": str(VISIBILITY_TIMEOUT),
                "RedrivePolicy": json.dumps(
                    {
                        "deadLetterTargetArn": dlq_arn,
                        "maxReceiveCount": MAX_RECEIVE_COUNT,
                    }
                ),
            },
        )
        print(f"[init] sqs queue created: {QUEUE_NAME} (DLQ after {MAX_RECEIVE_COUNT} receives)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "QueueAlreadyExists":
            raise
        sqs.get_queue_url(QueueName=QUEUE_NAME)
        print(f"[init] sqs queue already present: {QUEUE_NAME}")


def ensure_bucket(s3) -> None:
    """Create the results bucket with prod-equivalent hardening (localstack-aware)."""
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
        print(f"[init] s3 bucket already present: {BUCKET_NAME}")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if str(code) not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        # LocalStack ignores location constraints outside eu-west-1 style regions.
        kwargs: dict = {"Bucket": BUCKET_NAME}
        if REGION != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": REGION}
        s3.create_bucket(**kwargs)
        print(f"[init] s3 bucket created: {BUCKET_NAME}")

    s3.put_public_access_block(
        Bucket=BUCKET_NAME,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=BUCKET_NAME,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    print("[init] bucket policy reconciled: public-block + AES256")


def main() -> int:
    session = boto3.session.Session(region_name=REGION)
    dynamodb = session.client("dynamodb", endpoint_url=ENDPOINT)
    sqs = session.client("sqs", endpoint_url=ENDPOINT)
    s3 = session.client("s3", endpoint_url=ENDPOINT)

    ensure_dynamodb(dynamodb)
    ensure_dlq_then_queue(sqs)
    ensure_bucket(s3)

    print("[init] localstack resources reconciled successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
