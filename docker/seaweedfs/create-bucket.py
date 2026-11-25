"""One-shot: create the app's private bucket if it does not exist yet.

Runs inside the app image (boto3 is already a dependency), with the app's own
bucket-scoped credentials.
"""

import os
import sys
import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

bucket = os.environ["S3_BUCKET"]
client = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("S3_REGION", "us-east-1"),
)

for _attempt in range(30):
    try:
        client.head_bucket(Bucket=bucket)
        print(f"bucket {bucket} exists")
        sys.exit(0)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchBucket"):
            client.create_bucket(Bucket=bucket)
            print(f"bucket {bucket} created")
            sys.exit(0)
        time.sleep(2)
    except EndpointConnectionError:
        time.sleep(2)
print("object store not reachable", file=sys.stderr)
sys.exit(1)
