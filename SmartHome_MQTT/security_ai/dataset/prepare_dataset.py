"""Build a balanced, normalized three-class CICIoT2023 dataset."""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pyarrow as pa

SECURITY_AI = Path(__file__).resolve().parents[1]
if not __package__:
    # Running the file directly does not put the repository on Python's import path.
    sys.path.insert(0, str(SECURITY_AI.parent))
    __package__ = "security_ai.dataset"

from ..web_ui.cic_full import CIC_38, CIC_39, PROTOCOLS
from ..web_ui.data import (
    DEFAULT_DATA, LABELS, SPLITS, SPLIT_WEIGHTS, classify, connect, digest,
    inventory, read_json, sql_column, sql_literal, write_json, write_shard,
)

FEATURES = CIC_38
FAMILY = {"BENIGN": "Benign", "DOS": "DoS", "DDOS": "DDoS"}
SCENARIOS = set(LABELS.values())
OUTPUT = Path(__file__).resolve().parent / "processed" / "CIC_38"
STAGE_VERSION = "cic38-clean-1"
VERSION = "cic38-three-class-1"


def stage(source, cache):
    """Validate numeric data in bounded batches and cache one checked shard per file."""
    cache.mkdir(parents=True, exist_ok=True)
    paths = inventory(source)
    sources, excluded = [], []
    for index, path in enumerate(paths, 1):
        relative = path.relative_to(source).as_posix()
        if classify(path.parent.name) is None:
            excluded.append(relative)
            continue
        checksum = digest(path)
        key = sha256(f"{STAGE_VERSION}:{relative}:{checksum}".encode()).hexdigest()
        record, shard = cache / f"{key}.json", cache / f"{key}.parquet"
        saved = read_json(record) if record.exists() and shard.exists() else None
        if saved is None or saved["shard_sha256"] != digest(shard):
            partial = shard.with_suffix(".partial")
            saved = write_shard(path, source, partial, 8, checksum, FEATURES,
                                feature_maxima={**dict.fromkeys(PROTOCOLS, 1), "Protocol Type": 255, "Time_To_Live": 255})
            partial.replace(shard)
            saved["shard_sha256"] = digest(shard)
            write_json(record, saved)
        sources.append({**saved, "shard": str(shard)})
        if len(sources) % 45 == 0:
            print(f"Cleaned {index}/{len(paths)} CSV files", flush=True)
    return sources, excluded


def layout(sources):
    groups = defaultdict(list)
    for scenario, file_id, count in sources:
        groups[scenario].append((file_id, count))
    missing = sorted(SCENARIOS - groups.keys())
    if missing:
        raise ValueError(f"Missing eligible subclasses: {missing}")

    routes, shared = [], []
    for scenario, files in sorted(groups.items()):
        if sum(rows for _, rows in files) < 3:
            raise ValueError(f"Too few unique rows for three splits: {scenario}")
        if len(files) < 3:
            shared.append(scenario)
            continue
        total = sum(count for _, count in files)
        assigned = dict.fromkeys(SPLITS, 0)
        targets = {split: total * weight / sum(SPLIT_WEIGHTS) for split, weight in zip(SPLITS, SPLIT_WEIGHTS)}
        for index, (file_id, count) in enumerate(sorted(files, key=lambda row: (-row[1], row[0]))):
            empty = [split for split in SPLITS if not assigned[split]]
            choices = empty if len(files) - index == len(empty) else SPLITS
            split = min(choices, key=lambda key: (assigned[key] + count) / targets[key])
            assigned[split] += count
            routes.append((scenario, file_id, split))
    return routes, shared


def scenario_quotas(available):
    counts = {(split, scenario): (family, count) for split, scenario, family, count in available}
    by_class = defaultdict(list)
    for (split, scenario), (family, count) in counts.items():
        by_class[split, family].append((scenario, count))
    if missing := [(split, scenario) for split in SPLITS for scenario in sorted(SCENARIOS)
                   if (split, scenario) not in counts]:
        raise ValueError(f"Subclasses missing from assigned splits: {missing}")
    weights = dict(zip(SPLITS, SPLIT_WEIGHTS))
    units = min(sum(count for _, count in by_class[split, family]) // weights[split]
                for split in SPLITS for family in FAMILY)
    allocations = []
    for (split, _), entries in sorted(by_class.items()):
        total = units * weights[split]
        free = total - len(entries)
        capacity = sum(count - 1 for _, count in entries)
        if free < 0:
            raise ValueError("Insufficient unique rows to balance classes and retain each subclass")
        slots = {scenario: divmod(free * (count - 1), capacity) if capacity else (0, 0)
                 for scenario, count in entries}
        remaining = free - sum(n for n, _ in slots.values())
        extra = {scenario for scenario, _ in sorted(slots.items(), key=lambda item: (-item[1][1], item[0]))[:remaining]}
        allocations.extend((split, scenario, 1 + n + (scenario in extra))
                           for scenario, (n, _) in slots.items())
    return allocations


def clean_publish(folder):
    if folder.exists():
        for name in ("train.parquet", "validation.parquet", "test.parquet", "manifest.json",
                     "preprocessing.json", "manifest.tmp", "preprocessing.tmp"):
            (folder / name).unlink(missing_ok=True)
        folder.rmdir()


def prepare(source, output=OUTPUT, *, seed=42, memory_mib=2048, threads=4):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Source and output paths must be separate")
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}. Choose another --output path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = SECURITY_AI / ".cic38_cache"
    sources, excluded = stage(source, cache)
    with TemporaryDirectory(prefix="build-", dir=cache) as working, ExitStack() as cleanup:
        work = Path(working)
        # Create the publish directory beside the output so it inherits the project's ACL.
        publish = output.with_name(f".{output.name}-{work.name}")
        publish.mkdir()
        cleanup.callback(clean_publish, publish)
        with connect(work, memory_mib, 48, threads) as db:
            files = pa.table({"filename": [Path(row["shard"]).as_posix() for row in sources],
                              "file_id": range(len(sources)), "source_name": [row["file"] for row in sources]})
            db.register("files", files)
            paths = "[" + ",".join(sql_literal(path) for path in files["filename"].to_pylist()) + "]"
            db.execute(f"""CREATE VIEW raw AS SELECT * EXCLUDE(filename)
                           FROM read_parquet({paths}, filename=true) JOIN files USING (filename)""")
            cols = ", ".join(map(sql_column, FEATURES))
            raw_keys = sql_literal((work / "row_keys").as_posix())
            print("Indexing clean rows and checking duplicate feature vectors", flush=True)
            db.execute(f"""COPY (
                  SELECT hash({cols}) AS signature, hash({cols}) % 32 AS bucket,
                         (file_id << 32) + row_id AS row_key, scenario, family FROM raw
                ) TO {raw_keys} (FORMAT PARQUET, PARTITION_BY (bucket), COMPRESSION ZSTD)""")
            canonical = work / "canonical"
            canonical.mkdir()
            for bucket in sorted((work / "row_keys").glob("bucket=*")):
                input_glob = sql_literal((bucket / "*.parquet").as_posix())
                target = sql_literal((canonical / (bucket.name + ".parquet")).as_posix())
                db.execute(f"""COPY (
                    SELECT signature, min(row_key) AS row_key, arg_min(scenario,row_key) AS scenario,
                           arg_min(family,row_key) AS family, count(*) AS occurrences,
                           min(family) != max(family) AS conflict
                    FROM read_parquet({input_glob}) GROUP BY signature
                ) TO {target} (FORMAT PARQUET, COMPRESSION ZSTD)""")
            all_unique = sql_literal((canonical / "*.parquet").as_posix())
            db.execute(f"""CREATE VIEW eligible AS SELECT signature, row_key >> 32 AS file_id,
                               row_key & 4294967295 AS row_id, scenario, family
                           FROM read_parquet({all_unique}) WHERE NOT conflict""")
            unique, duplicate, conflicting = db.execute(f"""
                SELECT count(*) FILTER (WHERE NOT conflict), coalesce(sum(occurrences - 1) FILTER (WHERE NOT conflict),0),
                       coalesce(sum(occurrences) FILTER (WHERE conflict),0)
                FROM read_parquet({all_unique})""").fetchone()
            group_counts = db.execute("SELECT scenario, file_id, count(*) FROM eligible GROUP BY ALL").fetchall()
            routes, shared = layout(group_counts)
            db.register("routes", pa.table(dict(zip(("scenario", "file_id", "split"), zip(*routes)))
                                          if routes else {"scenario":pa.array([],pa.string()),
                                                          "file_id":pa.array([],pa.int64()),
                                                          "split":pa.array([],pa.string())}))
            after_route = sql_literal((work / "assigned.parquet").as_posix())
            if shared:
                shared_sql = ", ".join(map(sql_literal, shared))
                rare = f"""UNION ALL
                   SELECT signature,file_id,row_id,scenario,family,
                          CASE WHEN rank <= greatest(1, least(total-2, floor(total*.70))) THEN 'TRAIN'
                               WHEN rank <= greatest(2, least(total-1, floor(total*.85))) THEN 'VALIDATION'
                               ELSE 'TEST' END AS split
                   FROM (
                       SELECT *, row_number() OVER (PARTITION BY scenario ORDER BY file_id,row_id) AS rank,
                                 count(*) OVER (PARTITION BY scenario) AS total
                       FROM eligible WHERE scenario IN ({shared_sql})
                   )"""
            else:
                rare = ""
            db.execute(f"""COPY (
                   SELECT eligible.*, routes.split FROM eligible JOIN routes USING (scenario,file_id)
                   {rare}
                ) TO {after_route} (FORMAT PARQUET, COMPRESSION ZSTD)""")
            db.execute(f"CREATE VIEW assigned AS SELECT * FROM read_parquet({after_route})")
            available = db.execute("SELECT split, scenario, family, count(*) FROM assigned GROUP BY ALL").fetchall()
            allocations = scenario_quotas(available)
            db.register("quotas", pa.table(dict(zip(("split", "scenario", "quota"), zip(*allocations)))))
            selection = sql_literal((work / "selection.parquet").as_posix())
            db.execute(f"""COPY (
                   SELECT file_id,row_id,split FROM assigned JOIN quotas USING (split,scenario)
                   QUALIFY row_number() OVER (PARTITION BY split,scenario ORDER BY hash(signature,{seed}),signature) <= quota
                ) TO {selection} (FORMAT PARQUET, COMPRESSION ZSTD)""")
            retained = sql_literal((work / "retained").as_posix())
            print("Balancing Benign, DoS and DDoS in each split", flush=True)
            db.execute(f"""COPY (
                   SELECT {cols}, raw.scenario, raw.family, source_name, split
                   FROM raw JOIN read_parquet({selection}) USING (file_id,row_id)
                ) TO {retained} (FORMAT PARQUET, PARTITION_BY (split), COMPRESSION ZSTD)""")
            train = sql_literal((work / "retained/split=TRAIN/*.parquet").as_posix())
            stats_sql = ", ".join(f"avg({sql_column(name)}::DOUBLE), stddev_pop({sql_column(name)}::DOUBLE)" for name in FEATURES)
            stats = db.execute(f"SELECT {stats_sql} FROM read_parquet({train})").fetchone()
            scaler = {name: {"mean": stats[i*2], "scale": stats[i*2+1] or 1.0}
                      for i, name in enumerate(FEATURES)}
            normalized = ", ".join(f'(({sql_column(name)}::DOUBLE - ({stat["mean"]:.17g})) / '
                                   f'{stat["scale"]:.17g}) AS {sql_column(name)}'
                                   for name, stat in scaler.items())
            names = "CASE family WHEN 'BENIGN' THEN 'Benign' WHEN 'DOS' THEN 'DoS' ELSE 'DDoS' END"
            y_id = "CASE family WHEN 'BENIGN' THEN 0 WHEN 'DOS' THEN 1 ELSE 2 END"
            manifest_splits = {}
            for split in SPLITS:
                print(f"Exporting {split}", flush=True)
                dest = publish / f"{split.lower()}.parquet"
                raw_split = sql_literal((work / f"retained/split={split}/*.parquet").as_posix())
                db.execute(f"""COPY (
                       SELECT {normalized}, {names} AS label, ({y_id})::UTINYINT AS y,
                              scenario AS subclass, source_name FROM read_parquet({raw_split})
                    ) TO {sql_literal(dest.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)""")
                truth = db.execute(f"""SELECT label, subclass, count(*) FROM
                            read_parquet({sql_literal(dest.as_posix())}) GROUP BY ALL ORDER BY 1,2""").fetchall()
                seen = {scenario for _, scenario, _ in truth}
                assert seen == SCENARIOS, (split, sorted(SCENARIOS - seen))
                count = Counter()
                for label, _, size in truth:
                    count[label] += size
                assert len(set(count.values())) == 1 and set(count) == set(FAMILY.values())
                manifest_splits[split] = {"rows": sum(count.values()), "labels": dict(count),
                                          "subclasses": {scenario: size for _, scenario, size in truth},
                                          "sha256": digest(dest)}
            full = sum(info["rows"] for info in manifest_splits.values())
            for split, weight in zip(SPLITS, SPLIT_WEIGHTS):
                assert manifest_splits[split]["rows"] * 20 == full * weight
                manifest_splits[split]["fraction"] = weight / 20
            write_json(publish / "preprocessing.json", {"method": "z-score", "fit_on": "TRAIN",
                      "features": FEATURES, "parameters": scaler, "missing_policy": "DROP_INVALID_ROW"})
            write_json(publish / "manifest.json", {"version": VERSION, "source": str(source), "seed": seed,
                "features": FEATURES, "excluded_feature": "Number", "label_mapping": {"Benign":0,"DoS":1,"DDoS":2},
                "metadata_columns": ["label","y","subclass","source_name"], "total_rows": full,
                "input": {"files": len(sources), "rows": sum(row["rows"] for row in sources),
                          "invalid_rows": sum(row["rows"] - row["eligible"] for row in sources),
                          "malformed_csv_rows": sum(row["malformed"] for row in sources),
                          "excluded_files": excluded},
                "dedup": {"key": "DuckDB hash of all 38 float32 features",
                          "duplicate_rows_removed": duplicate, "conflicting_label_rows_removed": conflicting,
                          "unique_eligible_rows": unique},
                "split_policy": {"shared_source_subclasses": shared,
                                 "rare_policy": "contiguous ordered blocks of unique rows",
                                 "other_policy": "whole source CSV file per split",
                                 "label_balance": "equal Benign, DoS, DDoS per split after seeded undersampling"},
                "source_files": [{key:row[key] for key in ("file","sha256","label_provenance")} for row in sources],
                "splits": manifest_splits})
        publish.rename(output)
    return manifest_splits


def existing_splits(output, source, seed):
    manifest = read_json(output / "manifest.json")
    if (manifest["version"] != VERSION or tuple(manifest["features"]) != FEATURES
            or Path(manifest["source"]).resolve() != source.resolve() or manifest["seed"] != seed):
        raise ValueError(f"Existing output does not match this configuration: {output}. Choose another --output path.")
    preprocessing = read_json(output / "preprocessing.json")
    if tuple(preprocessing["features"]) != FEATURES or preprocessing["fit_on"] != "TRAIN":
        raise ValueError("Existing preprocessing metadata does not match the 38-feature dataset")
    for split in SPLITS:
        if digest(output / f"{split.lower()}.parquet") != manifest["splits"][split]["sha256"]:
            raise ValueError(f"Existing {split} file failed SHA-256 verification. Choose another --output path.")
    return manifest["splits"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=DEFAULT_DATA)
    p.add_argument("--output", type=Path, default=OUTPUT)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--memory-mib", type=int, default=2048)
    p.add_argument("--threads", type=int, choices=range(1, 9), default=4)
    args = p.parse_args()
    if args.seed < 0 or not 512 <= args.memory_mib <= 8192:
        p.error("Expected a nonnegative seed and memory between 512 and 8192 MiB")
    try:
        if args.output.is_dir():
            splits = existing_splits(args.output, args.source, args.seed)
            print(f"Verified existing dataset: {args.output.resolve()}", flush=True)
        else:
            splits = prepare(**vars(args))
    except (ValueError, OSError, KeyError) as error:
        p.exit(1, f"{error}\n")
    for name, info in splits.items():
        print(f"{name}: {info['rows']:,} rows, {info['labels']}", flush=True)


if __name__ == "__main__":
    main()
