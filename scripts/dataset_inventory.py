"""Read-only, content-free dataset inventory. Run on the machine holding the data."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path


def collect(root, paths, hash_max_mib=64):
    root = Path(root).resolve(strict=True)
    suffixes = {".csv", ".tsv", ".parquet", ".pq", ".xlsx", ".gz", ".zip"}
    files = set()
    missing = []
    excluded_links = 0
    for name in paths:
        candidate = root / name
        if candidate.is_symlink():
            excluded_links += 1
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("input path escapes inventory root")
        if not candidate.exists():
            missing.append(name)
            continue
        candidates = candidate.rglob("*") if candidate.is_dir() else [candidate]
        for path in candidates:
            if path.suffix.lower() not in suffixes or not path.is_file():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                excluded_links += 1
                continue
            if any(part.startswith(".") or part in {"preparation-deps", "profiles"}
                   for part in path.relative_to(root).parts):
                continue
            files.add(path)
    try:
        import pyarrow.parquet as pq
    except ImportError:
        pq = None
    entries = []
    for path in sorted(files):
        before = path.stat()
        item = {"path": path.relative_to(root).as_posix(), "bytes": before.st_size,
                "format": path.suffix.lower().lstrip("."), "rows": None,
                "columns": None, "row_count_method": "not_scanned",
                "sha256": None, "hash_status": "skipped_size_limit"}
        if pq is not None and path.suffix.lower() in {".parquet", ".pq"}:
            try:
                metadata = pq.read_metadata(path)
                item.update(rows=metadata.num_rows, columns=metadata.num_columns,
                            row_count_method="parquet_footer")
            except Exception as exc:
                item["metadata_error_type"] = type(exc).__name__
        if before.st_size <= hash_max_mib * 1024 * 1024:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            item.update(sha256=digest.hexdigest(), hash_status="full_file")
        after = path.stat()
        item["stable_during_inspection"] = (
            before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        if not item["stable_during_inspection"]:
            item.update(sha256=None, hash_status="invalidated_file_changed",
                        rows=None, columns=None, row_count_method="invalidated_file_changed")
        entries.append(item)
    return {"schema_version": 1,
            "observed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "scope": paths, "hash_max_mib": hash_max_mib,
            "parquet_metadata_available": pq is not None,
            "missing_requested_paths": missing, "excluded_links": excluded_links,
            "file_count": len(entries), "total_bytes": sum(x["bytes"] for x in entries),
            "files": entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Private root; not written in output")
    parser.add_argument("--paths", nargs="+", required=True, help="Relative dataset paths only")
    parser.add_argument("--hash-max-mib", type=int, default=64)
    args = parser.parse_args()
    if args.hash_max_mib < 0:
        parser.error("--hash-max-mib must be nonnegative")
    print(json.dumps(collect(args.root, args.paths, args.hash_max_mib), indent=2))


if __name__ == "__main__":
    main()
