"""Lossless preparation of downloaded UCI archives; never expands row counts.

Raw archives and prepared data stay outside the source checkout. No workbook is
authored. Online Retail II preserves both original sheets and missing values.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import time
import zipfile

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SOURCES = {
    "gas": {"archive": "gas.zip", "file": "gas-drift.parquet", "rows": 13910,
            "url": "https://archive.ics.uci.edu/dataset/224/gas+sensor+array+drift+dataset",
            "doi": "10.24432/C5RP6W", "creator": "Alexander Vergara",
            "license_note": "UCI footer says CC BY 4.0; original description restricts use to research, excludes commercial purposes. Internal research benchmark only; no raw redistribution."},
    "retail": {"archive": "retail-ii.zip", "file": "retail-ii.parquet", "rows": 1067371,
               "url": "https://archive.ics.uci.edu/dataset/502/online+retail+ii",
               "doi": "10.24432/C5CG6D", "creator": "Daqing Chen", "license_note": "CC BY 4.0"},
    "susy": {"archive": "susy.zip", "file": "susy.parquet", "rows": 5000000,
             "url": "https://archive.ics.uci.edu/dataset/279/susy", "doi": "10.24432/C54606",
             "creator": "Daniel Whiteson", "license_note": "CC BY 4.0; source events are Monte Carlo simulations, not measured experimental events."},
}
SUSY_COLUMNS = ["class_label", "lepton_1_pt", "lepton_1_eta", "lepton_1_phi",
                "lepton_2_pt", "lepton_2_eta", "lepton_2_phi", "missing_energy",
                "missing_energy_phi", "MET_rel", "axial_MET", "M_R", "M_TR_2",
                "R", "MT2", "S_R", "M_Delta_R", "dPhi_r_b", "cos_theta_r1"]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def gas_frame(archive):
    members = [name for name in archive.namelist() if re.search(r"batch\d+\.dat$", name, re.I)]
    members.sort(key=lambda name: int(re.search(r"batch(\d+)\.dat$", name, re.I).group(1)))
    if len(members) != 10:
        raise ValueError("gas archive must contain all ten original batches")
    rows = []
    for name in members:
        batch = int(re.search(r"batch(\d+)\.dat$", name, re.I).group(1))
        for line in archive.read(name).decode("ascii").splitlines():
            if not line.strip():
                continue
            fields = line.split()
            features = [field.split(":", 1) for field in fields[1:]]
            if [int(pair[0]) for pair in features] != list(range(1, 129)):
                raise ValueError("unexpected missing/duplicate/out-of-order sensor feature")
            rows.append([int(fields[0]), batch, *[float(pair[1]) for pair in features]])
    return pd.DataFrame(rows, columns=["gas_class", "source_batch"] +
                        [f"feature_{index:03d}" for index in range(1, 129)])


def save_frame(frame, destination):
    frame.to_parquet(destination, index=False)
    restored = pd.read_parquet(destination)
    pd.testing.assert_frame_equal(frame.reset_index(drop=True), restored, check_exact=True)
    return {"rows": len(frame), "columns": list(frame.columns),
            "dtypes": {name: str(dtype) for name, dtype in frame.dtypes.items()},
            "null_counts": {name: int(count) for name, count in frame.isna().sum().items()},
            "round_trip_exact": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", choices=SOURCES, required=True)
    args = parser.parse_args()
    source = SOURCES[args.dataset]
    archive_path = args.root / "raw" / source["archive"]
    destination = args.root / source["file"]
    if destination.exists():
        raise FileExistsError("prepared destination already exists; will not overwrite")
    started = time.perf_counter()
    with zipfile.ZipFile(archive_path) as archive:
        # Read specific members, never extract arbitrary paths from a ZIP.
        if any(info.file_size > 8 * 1024**3 for info in archive.infolist()):
            raise ValueError("unexpectedly large archive member")
        if args.dataset == "gas":
            record = save_frame(gas_frame(archive), destination)
            record["preparation"] = "All 10 batches in source order; 128 SVMLight feature indices mapped to feature_001..128; original class kept; source_batch added. No row filtering, duplication or imputation."
        elif args.dataset == "retail":
            members = [name for name in archive.namelist() if name.lower().endswith(".xlsx")]
            if len(members) != 1:
                raise ValueError("expected one original workbook")
            with archive.open(members[0]) as stream:
                sheets = pd.read_excel(stream, sheet_name=None, dtype={
                    "Invoice": "string", "StockCode": "string", "Description": "string",
                    "Country": "string"})
            frame = pd.concat(sheets.values(), ignore_index=True)
            # Customer IDs are nominal. Preserve blank IDs; do not create 'nan' IDs.
            frame["Customer ID"] = frame["Customer ID"].astype("Int64").astype("string")
            record = save_frame(frame, destination)
            record["sheets"] = {name: len(sheet) for name, sheet in sheets.items()}
            record["preparation"] = "Concatenated both original sheets in workbook order. Identifiers stored as nullable text, timestamps preserved; no dropped returns, blank customers, rows or columns. Overlaps in period with earlier Online Retail dataset; not independent retail evidence."
        else:
            members = [name for name in archive.namelist() if name.lower().endswith(".csv.gz")]
            if len(members) != 1:
                raise ValueError("expected one complete SUSY CSV.GZ")
            total = 0
            nulls = pd.Series(0, index=SUSY_COLUMNS, dtype="int64")
            with archive.open(members[0]) as compressed, gzip.GzipFile(fileobj=compressed) as stream:
                reader = pd.read_csv(stream, header=None, names=SUSY_COLUMNS, dtype="float64",
                                     float_precision="round_trip", chunksize=250000)
                with pq.ParquetWriter(destination, pa.schema([(name, pa.float64()) for name in SUSY_COLUMNS]),
                                      compression="snappy") as writer:
                    for chunk in reader:
                        table = pa.Table.from_pandas(chunk, preserve_index=False)
                        pd.testing.assert_frame_equal(chunk.reset_index(drop=True), table.to_pandas(), check_exact=True)
                        writer.write_table(table)
                        total += len(chunk)
                        nulls += chunk.isna().sum()
                        print(json.dumps({"stage": "prepare", "dataset": "susy", "rows": total}), flush=True)
            metadata = pq.ParquetFile(destination)
            if metadata.metadata.num_rows != total:
                raise AssertionError("SUSY Parquet row count changed")
            # Verify the actual saved row groups, not only the in-memory Arrow conversion.
            with archive.open(members[0]) as compressed, gzip.GzipFile(fileobj=compressed) as stream:
                reader = pd.read_csv(stream, header=None, names=SUSY_COLUMNS, dtype="float64",
                                     float_precision="round_trip", chunksize=250000)
                for index, chunk in enumerate(reader):
                    pd.testing.assert_frame_equal(chunk.reset_index(drop=True),
                        metadata.read_row_group(index).to_pandas(), check_exact=True)
            record = {"rows": total, "columns": SUSY_COLUMNS, "dtypes": dict.fromkeys(SUSY_COLUMNS, "float64"),
                      "null_counts": {key: int(value) for key, value in nulls.items()}, "round_trip_exact": True,
                      "preparation": "All five million original rows, streamed twice for saved-row-group exact verification; source column order named from UCI documentation; float64 preserved, no downcast, synthetic replication, filtering or imputation."}
    if record["rows"] != source["rows"]:
        raise AssertionError("download/preparation rows differ from published full dataset")
    record.update(dataset=args.dataset, source_url=source["url"], doi=source["doi"],
                  creator=source["creator"], license_note=source["license_note"],
                  archive_sha256=sha256(archive_path), parquet_sha256=sha256(destination),
                  archive_bytes=archive_path.stat().st_size, parquet_bytes=destination.stat().st_size,
                  seconds=time.perf_counter() - started, source_rows_verified=True)
    (args.root / f"source-{args.dataset}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"stage": "prepared", "dataset": args.dataset, "rows": record["rows"],
                      "columns": len(record["columns"]), "round_trip_exact": True}), flush=True)


if __name__ == "__main__":
    main()
