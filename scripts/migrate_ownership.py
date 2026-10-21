#!/usr/bin/env python3
"""Backfill ownership attributes on pre-P1 job records (P1 migration).

Job rows written before identity shipped carry no ``entity`` discriminator
and (in very old rows) no ``owner_id`` / ``schema_version``. The GSI job
queries and tenant-isolation checks assume all three, so this one-shot
migration stamps them:

* ``entity = "job"``           (always, guarded by attribute_not_exists)
* ``owner_id``                 (only if absent — never overwrites a real owner)
* ``schema_version = 1``       (only if absent)

Items that already carry ``entity`` (users, keys, sessions, usage, audit and
migrated jobs) are skipped, so the script is idempotent and safe to re-run.

Usage:
    python migrate_ownership.py [--owner system] [--dry-run]

``--owner`` sets the fallback owner for rows that have none (default
``system``, matching the API's pre-auth default — such rows stay invisible
to every tenant). ``--dry-run`` prints what would change without writing.
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_OWNER = "system"
SCAN_PAGE_SIZE = 100


def migrate(table_name: str, region: str, endpoint: str | None, owner: str, dry_run: bool) -> int:
    """Backfill legacy job rows; return the number of items updated."""
    resource = boto3.resource("dynamodb", region_name=region, endpoint_url=endpoint)
    table = resource.Table(table_name)

    scanned = 0
    updated = 0
    exclusive_start_key = None

    while True:
        kwargs: dict = {"Limit": SCAN_PAGE_SIZE}
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            scanned += 1
            if "entity" in item:
                continue  # identity row or already-migrated job
            job_id = str(item.get("job_id", "?"))
            if dry_run:
                print(f"[migrate] would backfill {job_id}")
                updated += 1
                continue
            try:
                table.update_item(
                    Key={"job_id": job_id},
                    UpdateExpression=(
                        "SET #e = :job, "
                        "owner_id = if_not_exists(owner_id, :owner), "
                        "schema_version = if_not_exists(schema_version, :version)"
                    ),
                    ConditionExpression="attribute_not_exists(#e)",
                    ExpressionAttributeNames={"#e": "entity"},
                    ExpressionAttributeValues={
                        ":job": "job",
                        ":owner": owner,
                        ":version": 1,
                    },
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "Unknown")
                if code == "ConditionalCheckFailedException":
                    continue  # raced with a concurrent writer that migrated it
                raise
            updated += 1
            print(f"[migrate] backfilled {job_id} owner={owner}")

        exclusive_start_key = page.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break

    verb = "would update" if dry_run else "updated"
    print(f"[migrate] scanned {scanned} items; {verb} {updated}")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner",
        default=DEFAULT_OWNER,
        help=f"fallback owner_id for rows without one (default: {DEFAULT_OWNER})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    args = parser.parse_args()

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    endpoint = os.environ.get("AWS_ENDPOINT_URL")  # None in AWS, LocalStack URL locally
    table_name = os.environ.get("QUAC_JOBS_TABLE", "quac-jobs")

    migrate(table_name, region, endpoint, args.owner, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
