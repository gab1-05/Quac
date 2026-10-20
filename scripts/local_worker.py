#!/usr/bin/env python3
"""Local Lambda stand-in for the Quac worker.

Long-polls the local FIFO queue and feeds each batch into the *real*
``worker.worker.lambda_handler`` — the identical entry point AWS Lambda invokes
in production — so the local path exercises the same envelope parsing,
idempotency, simulation, S3 persistence, webhook dispatch, and
``batchItemFailures`` taxonomy.

Failure handling mirrors Lambda semantics:

* records listed in ``batchItemFailures`` are left undeleted (they become
  visible again after the queue visibility timeout, then retry / trip the DLQ).
* all other records are deleted after a successful (or non-retryable) process.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Inside the Lambda base image, cwd is LAMBDA_TASK_ROOT (/var/task) where the
# worker package lives; on a host run cwd is the repo root. Ensure both are
# importable regardless of how this runner was launched.
sys.path.insert(0, os.getcwd())

from worker.worker import lambda_handler

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
QUEUE_URL = os.environ.get(
    "QUAC_QUEUE_URL", "http://localhost:4566/000000000000/quac-jobs.fifo"
)
WAIT_SECONDS = int(os.environ.get("QUAC_LOCAL_WAIT_SECONDS", "10"))
MAX_MESSAGES = int(os.environ.get("QUAC_LOCAL_BATCH_SIZE", "5"))


def main() -> int:
    session = boto3.session.Session(region_name=REGION)
    sqs = session.client("sqs", endpoint_url=ENDPOINT)

    print(f"[worker] long-polling {QUEUE_URL}", flush=True)
    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=QUEUE_URL,
                MaxNumberOfMessages=MAX_MESSAGES,
                WaitTimeSeconds=WAIT_SECONDS,
                VisibilityTimeout=60,
                AttributeNames=["MessageGroupId", "SequenceNumber"],
            )
        except Exception as exc:  # noqa: BLE001 - local infra hiccup, keep polling
            print(f"[worker] receive failed, retrying: {exc}", flush=True)
            time.sleep(3)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        records = [
            {
                "messageId": msg["MessageId"],
                "receiptHandle": msg["ReceiptHandle"],
                "body": msg["Body"],
                "attributes": msg.get("Attributes", {}),
                "messageAttributes": msg.get("MessageAttributes", {}),
            }
            for msg in messages
        ]
        print(f"[worker] batch of {len(records)} message(s)", flush=True)

        result = lambda_handler({"Records": records}, None)
        failed_ids = {
            item["itemIdentifier"]
            for item in result.get("batchItemFailures", [])
            if isinstance(item, dict)
        }

        for msg, record in zip(messages, records, strict=True):
            job_id = "unknown"
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                job_id = json.loads(record["body"]).get("job_id") or "unknown"

            if msg["MessageId"] in failed_ids:
                print(
                    f"[worker]   retryable failure job={job_id} "
                    "(left undeleted for redelivery/DLQ)",
                    flush=True,
                )
                continue

            sqs.delete_message(
                QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"]
            )
            print(f"[worker]   completed job={job_id}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
