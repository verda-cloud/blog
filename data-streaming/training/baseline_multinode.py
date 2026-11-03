import csv
import socket
from datetime import datetime, timezone
import io
import os
import time
from datetime import timedelta

import numpy as np
import pyarrow.parquet as pq
import s3fs
import torch
import torch.distributed as dist
from PIL import Image
from torch import nn
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, IterableDataset, get_worker_info
from torchvision import models, transforms


def make_fs():
    return s3fs.S3FileSystem(
        key=os.environ['VERDA_S3_ACCESS_KEY'],
        secret=os.environ['VERDA_S3_SECRET_KEY'],
        client_kwargs={'endpoint_url': os.environ['VERDA_S3_ENDPOINT'],
                       'region_name': os.getenv('AWS_DEFAULT_REGION', 'us-east-1')},
        config_kwargs={'retries': {'max_attempts': 10, 'mode': 'standard'}},
    )


def read_manifest():
    fs = make_fs()
    bucket = os.environ['VERDA_S3_BUCKET']
    prefix = os.getenv('DATA_PREFIX', 'intauralist/data').strip('/')
    paths = sorted(fs.glob(f'{bucket}/{prefix}/train-*.parquet'))
    if not paths:
        raise RuntimeError('No training Parquet files found at the configured prefix.')
    groups, offset = [], 0
    for path in paths:
        with fs.open(path, 'rb') as handle:
            metadata = pq.ParquetFile(handle).metadata
            for index in range(metadata.num_row_groups):
                count = metadata.row_group(index).num_rows
                groups.append((path, index, offset, offset + count))
                offset += count
    return groups, offset


def worker_range(total, consumers, batch_size, worker_id):
    quota = total // (consumers * batch_size) * batch_size
    if quota == 0:
        raise ValueError('Too few samples: reduce batch size or DataLoader workers.')
    return worker_id * quota, (worker_id + 1) * quota


class RemoteInaturalist(IterableDataset):
    def __init__(self, groups, total, rank, world_size, batch_size, transform):
        self.groups, self.total = groups, total
        self.rank, self.world_size = rank, world_size
        self.batch_size, self.transform = batch_size, transform

    def __iter__(self):
        worker = get_worker_info()
        workers = worker.num_workers if worker else 1
        worker_id = self.rank * workers + (worker.id if worker else 0)
        start, stop = worker_range(
            self.total, self.world_size * workers, self.batch_size, worker_id)
        assigned = [g for g in self.groups if g[2] < stop and g[3] > start]
        fs = make_fs()  # Construct a client in each worker process.
        while True:  # Fixed-step benchmark: repeat the assigned samples.
            for path, group, group_start, group_stop in assigned:
                lo = max(start, group_start) - group_start
                hi = min(stop, group_stop) - group_start
                with fs.open(path, 'rb', block_size=8 * 1024**2,
                             cache_type='readahead') as handle:
                    parquet = pq.ParquetFile(handle)
                    offset = 0
                    for batch in parquet.iter_batches(
                        batch_size=256, row_groups=[group],
                        columns=['image', 'label'], use_threads=False,
                    ):
                        left, right = max(0, lo - offset), min(batch.num_rows, hi - offset)
                        if left < right:
                            rows = batch.slice(left, right - left).to_pylist()
                            for row in rows:
                                with Image.open(io.BytesIO(row['image']['bytes'])) as image:
                                    yield self.transform(image.convert('RGB')), int(row['label'])
                        offset += batch.num_rows
                        if offset >= hi:
                            break


def main():
    batch_size = int(os.getenv('BATCH_SIZE', '64'))
    workers = int(os.getenv('NUM_WORKERS', '4'))
    warmup = int(os.getenv('WARMUP_STEPS', '5'))
    measured = int(os.getenv('MEASURE_STEPS', '50'))
    prefetch = int(os.getenv('PREFETCH_FACTOR', '2'))
    if batch_size < 1 or workers < 0 or warmup < 0 or measured < 1 or prefetch < 1:
        raise ValueError('Invalid batch/worker/step/prefetch settings.')

    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl', timeout=timedelta(minutes=10))
    rank, world = dist.get_rank(), dist.get_world_size()
    try:
        # Rank zero reads metadata once; broadcast failures as well as results.
        payload = [None]
        if rank == 0:
            try:
                payload[0] = {'manifest': read_manifest()}
            except Exception as exc:
                payload[0] = {'error': str(exc)}
        dist.broadcast_object_list(payload, src=0)
        if 'error' in payload[0]:
            raise RuntimeError(payload[0]['error'])
        groups, total = payload[0]['manifest']
        consumers = world * max(workers, 1)
        _, quota = worker_range(total, consumers, batch_size, 0)

        transform = transforms.Compose([
            transforms.RandomResizedCrop(224), transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
        torch.manual_seed(1234)
        np.random.seed(1234 + rank)
        num_classes = int(os.environ.get("NUM_CLASSES", "10000"))
        model = DDP(
            models.resnet18(
                weights=None,
                num_classes=num_classes,
            ).to(device),
            device_ids=[local_rank],
        )
        torch.manual_seed(1234 + rank)
        dataset = RemoteInaturalist(groups, total, rank, world, batch_size, transform)
        options = dict(batch_size=batch_size, num_workers=workers,
                       pin_memory=True, drop_last=True)
        if workers:
            options.update(prefetch_factor=prefetch, persistent_workers=True,
                           multiprocessing_context='spawn')
        loader = DataLoader(dataset, **options)
        optimizer = torch.optim.SGD(model.parameters(), lr=0.1, momentum=0.9)
        criterion = nn.CrossEntropyLoss()
        model.train()

        if rank == 0:
            print(f'Ranks={world}, batch/GPU={batch_size}, global batch={world * batch_size}', flush=True)
            print(f'Rows={total}, row groups={len(groups)}, workers={consumers}', flush=True)
            print(f'Rows per worker pass={quota}; unused tail rows={total - quota * consumers}', flush=True)
            print('Fixed-step FP32 benchmark; workers repeat their assigned rows.', flush=True)

        dist.barrier()
        first_batch_start = time.perf_counter()
        iterator = iter(loader)
        timings = []
        trace = []
        for step in range(warmup + measured):
            if step == warmup:
                torch.cuda.synchronize(device)
                dist.barrier()
                wall_start = time.perf_counter()
            start = time.perf_counter()
            batch_requested_at = time.time()
            images, labels = next(iterator)
            fetched = time.perf_counter()
            if step == 0:
                first_batch_seconds = fetched - first_batch_start
            images = images.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(images), labels)
            loss.backward()
            optimizer.step()
            torch.cuda.synchronize(device)
            end = time.perf_counter()
            trace.append({
                "timestamp_utc": datetime.fromtimestamp(
                    batch_requested_at, tz=timezone.utc
                ).isoformat(),
                "unix_seconds": batch_requested_at,
                "hostname": socket.gethostname(),
                "rank": rank,
                "local_rank": local_rank,
                "step": step,
                "phase": "warmup" if step < warmup else "measure",
                "data_wait_ms": (fetched - start) * 1000,
                "train_ms": (end - fetched) * 1000,
                "step_ms": (end - start) * 1000,
            })
            if step >= warmup:
                timings.append([fetched - start, end - fetched, end - start])

        elapsed = time.perf_counter() - wall_start
        log_dir = os.path.join(
            os.environ.get("SLURM_SUBMIT_DIR", "."),
            "timing_logs",
            os.environ.get("SLURM_JOB_ID", "manual"),
        )      
        os.makedirs(log_dir, exist_ok=True)

        with open(
            os.path.join(log_dir, f"rank-{rank:02d}.csv"),
            "w",
            newline="",
        ) as f:
            writer = csv.DictWriter(f, fieldnames=list(trace[0]))
            writer.writeheader()
            writer.writerows(trace)
        summary = torch.tensor([elapsed, first_batch_seconds], dtype=torch.float64, device=device)
        dist.all_reduce(summary, op=dist.ReduceOp.MAX)
        local = torch.tensor(timings, dtype=torch.float64, device=device)
        gathered = [torch.empty_like(local) for _ in range(world)]
        dist.all_gather(gathered, local)
        if rank == 0:
            values = torch.cat(gathered).cpu().numpy()
            waits, trains, steps = values.T
            samples = measured * batch_size * world
            print('\nBASELINE RESULTS')
            print(f'Measured sample presentations: {samples:,}')
            print(f'Measured wall time: {summary[0].item():.2f} s')
            print(f'Global throughput: {samples / summary[0].item():.1f} images/s')
            print(f'Max rank time to first batch: {summary[1].item():.2f} s')
            print(f'Mean / median / P95 wait: {waits.mean()*1000:.1f} / '
                  f'{np.median(waits)*1000:.1f} / {np.percentile(waits,95)*1000:.1f} ms')
            print(f'Mean train / step: {trains.mean()*1000:.1f} / {steps.mean()*1000:.1f} ms')
            print(f'Input-wait fraction: {100 * waits.mean() / steps.mean():.1f}%')
            print('Wait fraction is not GPU idle percentage. Train time includes DDP communication.')
    finally:
        dist.destroy_process_group()


if __name__ == '__main__':
    main()

