from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="juppy44/inat2021-train-mini-test",
    repo_type="dataset",
    allow_patterns="data/train-*.parquet",
    local_dir="./inaturalist",
)