"""Fetch official TLC 2017 files, verify rows, prepare a lossless two-column view.

Raw data stays unchanged. No duplicated rows, sampling, filters or generated records.
Without --download, only metadata is probed; downloads have a bounded disk budget.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import shutil
import socket
import struct
import time
import urllib.request

import pyarrow as pa
import pyarrow.parquet as pq

SOURCE_PAGE = "https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page"
BASE = "https://d37ci6vzurychx.cloudfront.net/trip-data/"


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def probe(month):
    url = BASE + f"yellow_tripdata_2017-{month:02d}.parquet"
    request = urllib.request.Request(url, headers={"Range": "bytes=-65536"})
    with urllib.request.urlopen(request, timeout=40) as response:
        if response.status != 206:
            raise RuntimeError(f"server did not honor bounded footer request: {month}")
        size = int(response.headers["Content-Range"].rsplit("/", 1)[1])
        footer = response.read(1_000_001)
    if len(footer) > 1_000_000 or footer[-4:] != b"PAR1":
        raise RuntimeError("invalid or oversized footer")
    metadata_size = struct.unpack("<I", footer[-8:-4])[0]
    if metadata_size + 8 > len(footer):
        if metadata_size > 1_000_000:
            raise RuntimeError("metadata exceeded probe budget")
        request = urllib.request.Request(url, headers={"Range": f"bytes=-{metadata_size + 8}"})
        with urllib.request.urlopen(request, timeout=40) as response:
            if response.status != 206:
                raise RuntimeError("range not honored")
            footer = response.read(metadata_size + 9)
    metadata = pq.read_metadata(io.BytesIO(b"PAR1" + footer[-metadata_size-8:]))
    schema = metadata.schema.to_arrow_schema()
    for col in ("PULocationID", "total_amount"):
        if col not in schema.names:
            raise RuntimeError(f"required field absent: {month}/{col}")
    return {"month": month, "url": url, "bytes": size, "rows": metadata.num_rows,
            "row_groups": metadata.num_row_groups,
            "selected_types": {c: str(schema.field(c).type) for c in ("PULocationID", "total_amount")}}


def fetch(item, directory):
    target = directory / item["url"].rsplit("/", 1)[1]
    started = time.perf_counter()
    if target.exists():
        if target.stat().st_size != item["bytes"] or pq.read_metadata(target).num_rows != item["rows"]:
            raise RuntimeError(f"existing download does not match: {target.name}")
    else:
        partial = target.with_suffix(".part")
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with urllib.request.urlopen(item["url"], timeout=60) as response, partial.open("wb") as stream:
                while block := response.read(1024 * 1024):
                    stream.write(block)
                    digest.update(block)
                    downloaded += len(block)
                    if downloaded > item["bytes"]:
                        raise RuntimeError("download exceeded advertised size")
            if downloaded != item["bytes"]:
                raise RuntimeError("truncated download")
            if pq.read_metadata(partial).num_rows != item["rows"]:
                raise RuntimeError("downloaded row count disagrees with probe")
            partial.rename(target)
        except Exception:
            # Keep incomplete downloads for diagnosis, never pass them to analysis.
            raise
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    emit({"stage": "downloaded", "month": item["month"], "rows": item["rows"],
          "bytes": target.stat().st_size, "seconds": round(time.perf_counter() - started, 3)})
    return {**item, "sha256": digest.hexdigest(), "local_file": target.name}


def prepare(directory, manifest):
    target = directory / "tlc_yellow_2017_region_revenue.parquet"
    if target.exists():
        raise RuntimeError("prepared dataset already exists; refuse to overwrite")
    schema = pa.schema([("region", pa.int64()), ("revenue", pa.float64())])
    partial = directory / "tlc_yellow_2017_region_revenue.partial.parquet"
    rows = 0
    with pq.ParquetWriter(partial, schema=schema, compression="snappy") as writer:
        for item in sorted(manifest["files"], key=lambda f: f["month"]):
            source = pq.ParquetFile(directory / item["local_file"])
            for batch in source.iter_batches(batch_size=1_000_000, columns=["PULocationID", "total_amount"]):
                table = pa.Table.from_batches([batch]).rename_columns(["region", "revenue"]).cast(schema, safe=True)
                writer.write_table(table, row_group_size=1_000_000)
                rows += len(table)
            emit({"stage": "prepared_month", "month": item["month"], "cumulative_rows": rows})
    if rows != manifest["rows"] or pq.read_metadata(partial).num_rows != rows:
        raise RuntimeError("prepared row count differs from originals")
    partial.rename(target)
    return {"file": target.name, "rows": rows, "bytes": target.stat().st_size,
            "schema": str(schema), "aliases": {"region": "PULocationID: taxi pickup zone ID",
                "revenue": "total_amount: passenger total amount, NOT driver net income"},
            "transform": "select two columns, rename, common int64/float64 types; no row filters/duplication/sampling"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--max-download-gib", type=float, default=8)
    parser.add_argument("--resolve-ip", help="Optional verified IPv4 for this source host only; TLS verification remains enabled")
    args = parser.parse_args()
    if args.resolve_ip:
        address = ipaddress.IPv4Address(args.resolve_ip)
        if not address.is_global:
            raise ValueError("source override must be a public IPv4 address")
        original_resolver = socket.getaddrinfo
        def resolve_source(host, port, *positional, **keyword):
            # Do not rewrite the URL: HTTPS still verifies the official hostname.
            return original_resolver(str(address) if host == "d37ci6vzurychx.cloudfront.net" else host,
                                     port, *positional, **keyword)
        socket.getaddrinfo = resolve_source
    args.directory.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        files = list(pool.map(probe, range(1, 13)))
    rows = sum(item["rows"] for item in files)
    size = sum(item["bytes"] for item in files)
    manifest = {"source_page": SOURCE_PAGE, "dataset": "2017 yellow taxi, all 12 official monthly files",
                "rows": rows, "download_bytes": size, "files": files,
                "data_quality": "Provider-reported public records; TLC does not guarantee accuracy/completeness."}
    emit({"stage": "probe", "rows": rows, "download_gib": size / 1024**3, "months": 12})
    with (args.directory / "probe.json").open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    if rows < 100_000_000:
        raise RuntimeError("official year has fewer than 100 million records")
    if not args.download:
        return
    if size > args.max_download_gib * 1024**3:
        raise RuntimeError("download exceeds user-configured budget")
    if shutil.disk_usage(args.directory).free < size * 3 + 4 * 1024**3:
        raise RuntimeError("insufficient disk budget")
    with ThreadPoolExecutor(max_workers=2) as pool:
        manifest["files"] = list(pool.map(lambda item: fetch(item, args.directory), files))
    if args.prepare:
        manifest["prepared"] = prepare(args.directory, manifest)
    with (args.directory / "provenance.json").open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    emit({"stage": "complete", "rows": rows, "prepared": manifest.get("prepared")})


if __name__ == "__main__":
    main()
