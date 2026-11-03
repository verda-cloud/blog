#!/usr/bin/env python3
"""Upload a local folder to Verda Object Storage without converting its files."""

import argparse
import os
from pathlib import Path

import boto3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Local folder to upload")
    parser.add_argument("--prefix", default="inaturalist/data",
                        help="Destination folder prefix in the bucket")
    args = parser.parse_args()
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        parser.error(f"Not a directory: {folder}")

    bucket = os.environ["VERDA_S3_BUCKET"]
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["VERDA_S3_ENDPOINT"],
        aws_access_key_id=os.environ["VERDA_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["VERDA_S3_SECRET_KEY"],
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )

    count = 0
    prefix = args.prefix.strip("/")
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(folder).as_posix()
        key = f"{prefix}/{relative}" if prefix else relative
        print(f"Uploading {relative} -> s3://{bucket}/{key}", flush=True)
        s3.upload_file(str(path), bucket, key)
        count += 1

    print(f"Done: uploaded {count} files.")


if __name__ == "__main__":
    main()