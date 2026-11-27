"""Build the paired 39-feature CIC dataset using the existing 38-feature splits."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np
import pyarrow as pa
import pyarrow.csv as pc
import pyarrow.parquet as pq

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "security_ai.dataset"

from .prepare_dataset import CIC_39, FEATURES, OUTPUT, SPLITS, clean_publish, stage
from ..web_ui.data import connect, digest, read_json, sql_column, sql_literal, write_json

OUTPUT_FULL = OUTPUT.with_name("CIC_39")
BASELINE_COLUMNS = (*FEATURES, "label", "y", "subclass", "source_name")


def read_counts(source, ids):
    """Read the Number field at the same parsed-row offsets used by the cleaning stage."""
    reader = pc.open_csv(source, read_options=pc.ReadOptions(block_size=8 * 2**20),
                         parse_options=pc.ParseOptions(invalid_row_handler=lambda row: "skip"),
                         convert_options=pc.ConvertOptions(include_columns=["Number"],
                                                          column_types={"Number": pa.float64()}))
    offset, found = 0, 0
    for batch in reader:
        end = offset + len(batch)
        stop = np.searchsorted(ids, end)
        if stop > found:
            values = batch[0].take(pa.array(ids[found:stop] - offset)).to_numpy(zero_copy_only=False)
            if not np.all(np.isfinite(values) & (values > 0) & (values == np.floor(values))):
                raise ValueError(f"Invalid Number in reference source: {source}")
            yield ids[found:stop], values
            found = stop
            if found == len(ids):
                return
        offset = end
    raise ValueError(f"Could not recover all baseline rows from {source}")


def recover_numbers(db, path, sources, directory):
    schema = pa.schema([("file_id", pa.int64()), ("row_id", pa.int64()), ("Number", pa.float64())])
    rows = db.execute("SELECT file_id,row_id FROM matches ORDER BY file_id,row_id").fetchnumpy()
    boundaries = np.flatnonzero(np.diff(rows["file_id"])) + 1
    with pq.ParquetWriter(path, schema, compression="zstd") as writer:
        for group in np.split(np.arange(len(rows["row_id"])), boundaries):
            if not len(group):
                continue
            file_id = rows["file_id"][group[0]]
            file = directory / sources[file_id]["file"]
            for ids, values in read_counts(file, rows["row_id"][group]):
                writer.write_table(pa.table({"file_id": np.full(len(ids), file_id),
                                             "row_id": ids, "Number": values}, schema=schema))
            if digest(file) != sources[file_id]["sha256"]:
                raise ValueError(f"Source changed during Number lookup: {file}")


def prepare_full(baseline=OUTPUT, output=OUTPUT_FULL, *, memory_mib=2048, threads=4):
    baseline, output = Path(baseline).resolve(), Path(output).resolve()
    if output.is_relative_to(baseline) or baseline.is_relative_to(output) or output.exists():
        raise ValueError("Choose a new output folder separate from the baseline")
    manifest_path = baseline / "manifest.json"
    manifest_hash = digest(manifest_path)
    manifest = read_json(manifest_path)
    preprocessing = read_json(baseline / "preprocessing.json")
    if (tuple(manifest["features"]) != FEATURES or tuple(preprocessing["features"]) != FEATURES
            or preprocessing["method"] != "z-score" or preprocessing["fit_on"] != "TRAIN"):
        raise ValueError("Baseline must be the prepared 38-feature CIC dataset")
    source_root = Path(manifest["source"]).resolve()
    if output.is_relative_to(source_root) or source_root.is_relative_to(output):
        raise ValueError("Source and output paths must be separate")
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = Path(__file__).resolve().parents[1] / ".cic38_cache"
    sources, _ = stage(source_root, cache)
    if {row["file"]: row["sha256"] for row in sources} != {
            row["file"]: row["sha256"] for row in manifest["source_files"]}:
        raise ValueError("Reference CSV files do not match the baseline manifest")

    with TemporaryDirectory(prefix="full-", dir=cache) as temporary, ExitStack() as cleanup:
        work = Path(temporary)
        publish = output.with_name(f".{output.name}-{work.name}")
        publish.mkdir()
        cleanup.callback(clean_publish, publish)
        with connect(work, memory_mib, 48, threads) as db:
            base_tables = {}
            for split in SPLITS:
                file = baseline / f"{split.lower()}.parquet"
                if digest(file) != manifest["splits"][split]["sha256"]:
                    raise ValueError(f"Baseline {split} hash mismatch")
                base_tables[split] = f"read_parquet({sql_literal(file.as_posix())})"
            cols = ", ".join(map(sql_column, FEATURES))
            parts = [f"SELECT *, {sql_literal(split)} AS split FROM {table}"
                     for split, table in base_tables.items()]
            db.execute(f"""CREATE TABLE baseline AS SELECT *, hash({cols},source_name) AS signature
                         FROM ({" UNION ALL ".join(parts)})""")
            total, distinct = db.execute("SELECT count(*), count(DISTINCT signature) FROM baseline").fetchall()[0]
            if total != manifest["total_rows"] or distinct != total:
                raise ValueError("Baseline rows must have unique feature signatures")
            names = pa.table({"filename": [Path(row["shard"]).as_posix() for row in sources],
                              "file_id": range(len(sources)), "source_name": [row["file"] for row in sources]})
            db.register("sources", names)
            paths = "[" + ",".join(map(sql_literal, names["filename"].to_pylist())) + "]"
            scaler = preprocessing["parameters"]
            normalized = ", ".join(f'(({sql_column(name)}::DOUBLE - ({scaler[name]["mean"]:.17g})) / '
                                   f'{scaler[name]["scale"]:.17g}) AS {sql_column(name)}' for name in FEATURES)
            # Verify the full vector after matching signatures and use the same first row in the same file.
            feature_match = " AND ".join(f"raw.{sql_column(name)}=baseline.{sql_column(name)}" for name in FEATURES)
            print("Matching 38-feature baseline rows to original CSV row offsets", flush=True)
            db.execute(f"""CREATE TABLE matches AS
                SELECT raw.signature, file_id, min(row_id) AS row_id
                FROM (
                    SELECT *, hash({cols},source_name) AS signature
                    FROM (SELECT {normalized}, file_id, row_id, source_name
                          FROM read_parquet({paths}, filename=true) JOIN sources USING (filename))
                ) AS raw JOIN baseline USING (signature,source_name)
                WHERE {feature_match}
                GROUP BY raw.signature,file_id""")
            if db.execute("SELECT count(*) FROM matches").fetchall()[0][0] != total:
                raise ValueError("Failed to match every baseline feature vector to the cached source")
            numbers = work / "numbers.parquet"
            print("Recovering the original Number value for each matched row", flush=True)
            recover_numbers(db, numbers, sources, source_root)
            db.execute(f"""CREATE TABLE paired AS SELECT baseline.*, numbers."Number"
                FROM baseline JOIN matches USING (signature)
                JOIN read_parquet({sql_literal(numbers.as_posix())}) AS numbers USING (file_id,row_id)""")
            if db.execute("SELECT count(*) FROM paired").fetchall()[0][0] != total:
                raise ValueError("Number lookup changed the reference row count")
            count_stats = db.execute("""SELECT avg("Number"), stddev_pop("Number")
                                       FROM paired WHERE split='TRAIN'""").fetchall()[0]
            mean, scale = count_stats[0], count_stats[1] or 1.0
            projection = ", ".join(f'( ("Number"-({mean:.17g}))/{scale:.17g}) AS "Number"'
                                   if name=="Number" else sql_column(name) for name in CIC_39)
            reference_cols = ", ".join(map(sql_column, BASELINE_COLUMNS))
            for split in SPLITS:
                dest = publish / f"{split.lower()}.parquet"
                relation = f"read_parquet({sql_literal(dest.as_posix())})"
                db.execute(f"""COPY (
                    SELECT {projection}, label,y,subclass,source_name
                    FROM paired WHERE split={sql_literal(split)}
                ) TO {sql_literal(dest.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)""")
                rows, different = db.execute(f"""SELECT
                    (SELECT count(*) FROM {relation}),
                    (SELECT count(*) FROM (SELECT {reference_cols} FROM {relation}
                     EXCEPT ALL SELECT {reference_cols} FROM {base_tables[split]}))""").fetchall()[0]
                if rows != manifest["splits"][split]["rows"] or different:
                    raise ValueError(f"Export is not paired with baseline {split}")
                manifest["splits"][split]["sha256"] = digest(dest)
                print(f"{split}: {rows:,} paired rows", flush=True)
            if digest(manifest_path) != manifest_hash:
                raise ValueError("Baseline manifest changed during preparation")
            preprocessing["features"] = CIC_39
            preprocessing["parameters"]["Number"] = {"mean": mean, "scale": scale}
            manifest["features"] = CIC_39
            manifest["version"] = "cic39-three-class-paired-1"
            manifest.pop("excluded_feature", None)
            manifest["comparison"] = {
                "baseline": str(baseline), "baseline_manifest_sha256": manifest_hash,
                "added_feature": "Number", "same_rows_and_splits": True,
                "same_common_features_and_scaler": True,
            }
            write_json(publish / "preprocessing.json", preprocessing)
            write_json(publish / "manifest.json", manifest)
        publish.rename(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=OUTPUT)
    parser.add_argument("--output", type=Path, default=OUTPUT_FULL)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--threads", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    if not 512 <= args.memory_mib <= 8192:
        parser.error("Memory must be between 512 and 8192 MiB")
    try:
        print(prepare_full(**vars(args)))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
