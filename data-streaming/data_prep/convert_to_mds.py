#!/usr/bin/env python3

import os

import pyarrow.parquet as pq
import s3fs
from streaming import MDSWriter


BUCKET = os.environ.get("VERDA_S3_BUCKET")
INPUT_PREFIX = os.environ["DATA_PREFIX"].strip("/")
OUTPUT_PREFIX = os.environ.get(
   "DATA_MDS_PREFIX",
   "inaturalist_mds_v1",
).strip("/")

ENDPOINT = os.environ["VERDA_S3_ENDPOINT"]
ACCESS_KEY = os.environ["VERDA_S3_ACCESS_KEY"]
SECRET_KEY = os.environ["VERDA_S3_SECRET_KEY"]

# MDSWriter uses these independently of s3fs
os.environ["AWS_ACCESS_KEY_ID"] = ACCESS_KEY
os.environ["AWS_SECRET_ACCESS_KEY"] = SECRET_KEY
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["S3_ENDPOINT_URL"] = ENDPOINT

def make_fs():
   return s3fs.S3FileSystem(
       key=ACCESS_KEY,
       secret=SECRET_KEY,
       client_kwargs={
           "endpoint_url": ENDPOINT,
           "region_name": "us-east-1",
       },
   )


fs = make_fs()

input_shards = sorted(
    fs.glob(f"{BUCKET}/{INPUT_PREFIX}/train-*.parquet")
)

if not input_shards:
    raise RuntimeError(
        f"No training Parquet files found in "
        f"s3://{BUCKET}/{INPUT_PREFIX}/"
    )

print(f"Found {len(input_shards)} input shards", flush=True)

remote_output = (
   f"s3://{BUCKET}/{OUTPUT_PREFIX}"
)

columns = {
   "image": "bytes",
   "label": "int",
}


count = 0

with MDSWriter(
   out=remote_output,
   columns=columns,
   size_limit=64 * 1024 * 1024,

   # JPEGs are already compressed, so keep this simple.
   compression=None,

   progress_bar=True,
) as writer:

   for shard in input_shards:

       print(f"Reading {shard}")

       with fs.open(
           shard,
           "rb",
           block_size=8 * 1024 * 1024,
           cache_type="readahead",
       ) as f:

           parquet = pq.ParquetFile(f)

           for batch in parquet.iter_batches(
               batch_size=512,
               columns=["image", "label"],
           ):

               images = batch.column("image")
               labels = batch.column("label")

               for image, label in zip(
                   images,
                   labels,
               ):
                   image_record = image.as_py()
                   image_bytes = image_record["bytes"]
                   label_value = int(label.as_py())

                   if not image_bytes:
                    raise ValueError(f"Missing embedded image bytes in {shard}")

                   if not 0 <= label_value < 10000:
                    raise ValueError(f"Unexpected label: {label_value}")

                   writer.write({
                       "image": image_record["bytes"],
                       "label": label_value,
                   })

                   count += 1

                   if count % 10_000 == 0:
                       print(
                           f"Converted {count:,} samples"
                       )


print(f"Done: {count:,} samples")
print(f"Output: {remote_output}")


