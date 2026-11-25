"""Read-only source inventory and disk-backed, group-disjoint CIC preparation."""
from collections import Counter, defaultdict
import csv
from hashlib import file_digest, sha256
from itertools import permutations
import json
from pathlib import Path
import re
import shutil

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.csv as pc
import pyarrow.compute as ac
import pyarrow.parquet as pq

from .contract import BASIC_8, BASIC_10, FLAGS, PRESETS, contract

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT.parent / "CIC-IoT-2023"
WORK = ROOT / "security_ai_v2" / "work_70_15_15"
SPLITS = ("TRAIN", "VALIDATION", "TEST")
TARGET = dict(zip(SPLITS, (.70, .15, .15)))
VERSION = "cic-rebuild-2"
SOURCE_CACHE_VERSION = "cic-rebuild-1"
SPLIT_WEIGHTS = (14, 3, 3)
SCENARIOS = {
    prefix + "-" + name
    for prefix, names in (("DoS", ("HTTP_Flood", "SYN_Flood", "TCP_Flood", "UDP_Flood")),
                          ("DDoS", ("ACK_Fragmentation", "HTTP_Flood", "ICMP_Flood", "ICMP_Fragmentation",
                                    "PSHACK_FLOOD", "RSTFINFLOOD", "SlowLoris", "SYN_Flood",
                                    "SynonymousIP_Flood", "TCP_Flood", "UDP_Flood", "UDP_Fragmentation")))
    for name in names
}
LABELS = {re.sub(r"[^a-z0-9]", "", name.lower()): name for name in SCENARIOS}
LABELS.update(dict(benign="BenignTraffic", benigntraffic="BenignTraffic", benignfinal="BenignTraffic"))


def digest(path):
    with Path(path).open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def classify(value):
    name = LABELS.get(normalize(value))
    if not name:
        return None
    family = "BENIGN" if name == "BenignTraffic" else "DDOS" if name.startswith("DDoS-") else "DOS"
    return name, family, int(family != "BENIGN")


def inferred(relative):
    if len(relative.parts) < 2:
        return None
    info = classify(relative.parent.name)
    if not info or not normalize(relative.name).startswith(normalize(info[0])):
        return None
    return info


def inventory(directory):
    base = Path(directory).resolve()
    if not base.is_dir():
        raise ValueError("DATASET_NOT_FOUND")
    files = sorted(p for p in base.rglob("*.csv") if p.is_file() and p.resolve().is_relative_to(base))
    if not files:
        raise ValueError("DATASET_NOT_FOUND")
    return files


def header(path):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        fields = next(csv.reader(stream), [])
    if not fields or len(fields) != len(set(fields)):
        raise ValueError("INVALID_CSV_HEADER")
    return fields


def write_shard(path, base, stage, chunk_mib, source_sha):
    rel = path.relative_to(base)
    fields = header(path)
    label = next((field for field in fields if field.lower() == "label"), None)
    label_info = inferred(rel) if label is None else None
    names = list(BASIC_10)
    has_features = set(names).issubset(fields)
    # Read the label or one existing field even for excluded schemas to count valid records.
    selected = [n for n in names if n in fields]
    if "Number" in fields:
        selected.append("Number")
    if label:
        selected.append(label)
    if not selected:
        selected.append(fields[0])
    malformed = 0

    def invalid_row(_):
        nonlocal malformed
        malformed += 1
        return "skip"

    reader = pc.open_csv(path, read_options=pc.ReadOptions(block_size=chunk_mib * 2**20),
                         parse_options=pc.ParseOptions(invalid_row_handler=invalid_row),
                         convert_options=pc.ConvertOptions(include_columns=selected,
                             column_types={n: pa.string() for n in selected}, strings_can_be_null=True))
    source = {"file": rel.as_posix(), "bytes": path.stat().st_size, "sha256": source_sha, "fields": fields,
              "label_provenance": "CSV_LABEL" if label else "INFERRED_FOLDER_FILENAME" if label_info else "UNVERIFIED",
              "rows": 0, "eligible": 0, "labels": Counter(), "eligible_labels": Counter(), "quality": defaultdict(Counter),
              "group_sizes": Counter()}
    schema = pa.schema([(n, pa.float32()) for n in names] + [
        ("row_id", pa.int64()), ("original_label", pa.string()), ("scenario", pa.string()),
        ("family", pa.string()), ("y", pa.int8())])
    offset = 0
    with pq.ParquetWriter(stage, schema, compression="zstd") as writer:
        for batch in reader:
            nrows = batch.num_rows
            source["rows"] += nrows
            if label:
                originals = batch.column(label).to_pylist()
                parsed = [classify(value) for value in originals]
                source["labels"].update(str(value) for value in originals)
            else:
                originals = [(label_info[0] if label_info else rel.parent.name)] * nrows
                parsed = [label_info] * nrows
                source["labels"][originals[0]] += nrows
            columns = {}
            bad = np.zeros(nrows, dtype=bool)
            for name in selected:
                if name == label or name not in (*names, "Number"):
                    continue
                values = batch.column(name)
                try:
                    numeric = ac.cast(values, pa.float64()).to_numpy(zero_copy_only=False)
                except pa.ArrowInvalid:
                    with duckdb.connect(":memory:") as db:
                        db.register("cells", pa.table({"value": values}))
                        numeric = db.execute("SELECT try_cast(value AS DOUBLE) AS number FROM cells").fetchnumpy()["number"]
                        if np.ma.isMaskedArray(numeric):
                            numeric = numeric.filled(np.nan)
                nulls = values.is_null().to_numpy(zero_copy_only=False)
                inf = np.isinf(numeric)
                nan = np.isnan(numeric) & ~nulls
                impossible = np.isfinite(numeric) & ((numeric < 0) |
                    (numeric > (1 if name in FLAGS else np.finfo(np.float32).max)))
                counts = source["quality"][name]
                counts.update(missing=int(nulls.sum()), nan_or_invalid=int(nan.sum()),
                              infinity=int(inf.sum()), impossible=int(impossible.sum()))
                if name == "Number":
                    observed, counts_for_number = np.unique(numeric[np.isfinite(numeric)], return_counts=True)
                    source["group_sizes"].update({format(float(v), ".8g"): int(c) for v, c in zip(observed, counts_for_number)})
                    continue
                bad |= nulls | inf | nan | impossible
                columns[name] = numeric
            if not has_features:
                source["schema_rejected"] = True
                offset += nrows
                continue
            contradictory = ((columns["Min"] > columns["Max"]) |
                             (columns["AVG"] + 1e-6 < columns["Min"]) |
                             (columns["AVG"] - 1e-6 > columns["Max"]))
            source["impossible_length_order"] = source.get("impossible_length_order", 0) + int(contradictory.sum())
            bad |= contradictory
            keep = ~bad & np.fromiter((info is not None for info in parsed), dtype=bool, count=nrows)
            idx = np.flatnonzero(keep)
            if len(idx):
                labels = [parsed[i] for i in idx]
                family_rows = Counter(i[1] for i in labels)
                source["eligible_labels"].update(family_rows)
                writer.write_table(pa.Table.from_arrays(
                    [pa.array(columns[n][idx].astype(np.float32)) for n in names] +
                    [pa.array(idx + offset), pa.array([originals[i] for i in idx]),
                     pa.array([i[0] for i in labels]), pa.array([i[1] for i in labels]),
                     pa.array([i[2] for i in labels], type=pa.int8())], schema=schema))
            source["eligible"] += len(idx)
            offset += nrows
    source["malformed"] = malformed
    if digest(path) != source_sha:
        raise ValueError(f"SOURCE_CHANGED: {source['file']}")
    return source


def split_groups(sources, seed=42, authority=None):
    """Single assignment per verified ID or file hash, then scenario balance."""
    authority = authority or {}
    grouped = defaultdict(list)
    for source in sources:
        if source["eligible"]:
            grouped[authority.get(source["file"], source["sha256"])].append(source)
    if len(grouped) < 3:
        raise ValueError("INSUFFICIENT_SOURCE_GROUPS")
    data = []
    totals = Counter()
    for key, sources_in_group in grouped.items():
        scenario_counts = Counter()
        for source in sources_in_group:
            scenario_counts.update(source["scenarios"])
        totals.update(scenario_counts)
        data.append((key, scenario_counts, sum(scenario_counts.values())))
    seen = defaultdict(Counter)
    global_seen = Counter()
    total_rows = sum(totals.values())
    assignment = {}
    pending = Counter(s for _, c, _ in data for s in c)
    data.sort(key=lambda row: (-row[2], sha256(f"{seed}:{row[0]}".encode()).hexdigest()))
    for key, counts, size in data:
        def cost(split):
            score = ((global_seen[split] + size - total_rows * TARGET[split]) ** 2
                    - (global_seen[split] - total_rows * TARGET[split]) ** 2) / max(1, total_rows**2)
            for scenario, count in counts.items():
                target = totals[scenario] * TARGET[split]
                previous = seen[scenario][split]
                score += ((previous + count - target)**2 - (previous - target)**2) / max(1, totals[scenario]**2)
                missing = sum(not seen[scenario][name] for name in SPLITS)
                if not previous and pending[scenario] <= missing and totals[scenario] > count:
                    score -= 1.5
            return score
        selected = min(SPLITS, key=cost)
        assignment[key] = selected
        global_seen[selected] += size
        for scenario, count in counts.items():
            seen[scenario][selected] += count
            pending[scenario] -= 1
    for source in sources:
        if source["eligible"]:
            source["group"] = authority.get(source["file"], source["sha256"])
            source["split"] = assignment[source["group"]]
    return dict(seen)


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql_column(value):
    return '"' + value.replace('"', '""') + '"'


def connect(work, memory_mib, disk_gib, threads):
    db = duckdb.connect(":memory:")
    db.execute(f"SET memory_limit='{memory_mib}MiB'; SET threads={threads}; SET preserve_insertion_order=false")
    db.execute(f"SET temp_directory={sql_literal((work / 'spill').as_posix())};"
               f"SET max_temp_directory_size='{disk_gib}GiB'")
    return db


def stage_dataset(directory, work, *, chunk_mib=8, disk_gib=16, memory_mib=768, threads=2, seed=42,
                  groups=None, progress=print):
    folder = Path(directory).resolve()
    paths = inventory(folder)
    work = Path(work).resolve()
    if work.is_relative_to(folder) or folder.is_relative_to(work):
        raise ValueError("WORKSPACE_OVERLAPS_SOURCE")
    for value, low, high in ((chunk_mib, 1, 64), (disk_gib, 2, 128), (memory_mib, 256, 4096), (threads, 1, 8)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("RESOURCE_CONFIG_INVALID")
    work.mkdir(parents=True, exist_ok=True)
    stages = work / "source"
    stages.mkdir(exist_ok=True)
    sources = []
    for i, path in enumerate(paths):
        file_hash = digest(path)
        relative = path.relative_to(folder).as_posix()
        shard_id = sha256(f"{SOURCE_CACHE_VERSION}\0{relative}\0{file_hash}".encode()).hexdigest()
        info_file = stages / f"{shard_id}.json"
        shard = stages / f"{shard_id}.parquet"
        existing = read_json(info_file) if info_file.is_file() and shard.is_file() else None
        if existing is None or digest(shard) != existing.get("shard_sha256"):
            incomplete = shard.with_suffix(".partial")
            existing = write_shard(path, folder, incomplete, chunk_mib, file_hash)
            incomplete.replace(shard)
            existing["shard_sha256"] = digest(shard)
            write_json(info_file, existing)
        existing["shard"] = str(shard)
        if existing["eligible"]:
            meta = pq.ParquetFile(shard)
            scenarios = Counter()
            for batch in meta.iter_batches(columns=["scenario"]):
                scenarios.update({p["values"]: p["counts"] for p in ac.value_counts(batch.column(0)).to_pylist()})
            existing["scenarios"] = scenarios
        sources.append(existing)
        if i % 5 == 0 or i + 1 == len(paths):
            progress(f"inventory {i+1}/{len(paths)} {relative} rows={existing['rows']:,}")
        size = sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
        if size > disk_gib * 2**30 or shutil.disk_usage(work).free < 512 * 2**20:
            raise ValueError("DISK_QUOTA_EXCEEDED")
    manifest = {"version": VERSION, "source_dir": str(folder), "sources": sources, "seed": seed,
                "files": len(sources), "bytes": sum(s["bytes"] for s in sources),
                "rows": sum(s["rows"] for s in sources), "eligible": sum(s["eligible"] for s in sources),
                "malformed": sum(s["malformed"] for s in sources)}
    write_json(work / "inventory.json", manifest)
    manifest["group_policy"] = "VERIFIED_GROUP_MAPPING" if groups and all(
        source["file"] in groups for source in sources if source["eligible"]) else "EXPLORATORY_FILE_GROUP_SPLIT"
    manifest["source_scenario_counts"] = split_groups(sources, seed, groups)
    assemble(manifest, work, memory_mib, disk_gib, threads, progress)
    return manifest


def balanced_signatures(fixed, shared):
    """Allocate exact/near duplicate signatures using only label and split availability."""
    choices = []
    available = [list(row) for row in fixed]
    for label, group in enumerate(shared):
        best_score = (-1, -1)
        best_choices = []
        # Compare fixed priorities, largest retained count, and capacity-balancing passes.
        for priority in permutations(range(len(SPLITS))):
            order = {rank: i for i, rank in enumerate(priority)}
            for margin in (None, 0.0, .5):
                totals = list(fixed[label])
                candidate = []
                for signature, counts in group:
                    usable = [rank for rank, count in enumerate(counts) if count]
                    if margin is None:
                        rank = min(usable, key=order.__getitem__)
                    else:
                        rank = min(usable, key=lambda r: (
                            (totals[r] + margin * counts[r]) / SPLIT_WEIGHTS[r], -counts[r], order[r]))
                    totals[rank] += counts[rank]
                    candidate.append((signature, rank))
                score = (min(n // weight for n, weight in zip(totals, SPLIT_WEIGHTS)), sum(totals))
                if score > best_score:
                    best_score = score
                    available[label] = totals
                    best_choices = candidate
        choices.extend(best_choices)
    caps = [[weight * min(n // w for n, w in zip(counts, SPLIT_WEIGHTS))
             for weight in SPLIT_WEIGHTS] for counts in available]
    if any(not min(quotas) for quotas in caps):
        raise ValueError("INSUFFICIENT_BINARY_ROWS_FOR_BALANCED_SPLIT")
    return choices, available, caps


def assemble(manifest, work, memory_mib, disk_gib, threads, progress):
    sources = [s for s in manifest["sources"] if s["eligible"]]
    files = [s["shard"] for s in sources]
    mapping = pa.table({"filename": [Path(path).as_posix() for path in files],
                        "source_name": [s["file"] for s in sources], "source_sha256": [s["sha256"] for s in sources],
                        "group_id": [s["group"] for s in sources],
                        "split": [s["split"] for s in sources],
                        "rank": [SPLITS.index(s["split"]) for s in sources]})
    raw_paths = "[" + ",".join(sql_literal(path) for path in mapping["filename"].to_pylist()) + "]"
    progress("dedup audit started")
    with connect(work, memory_mib, disk_gib, threads) as db:
        db.register("routing", mapping)
        db.execute(f"""CREATE VIEW input AS SELECT * EXCLUDE(filename) FROM read_parquet({raw_paths}, filename=true)
                    JOIN routing USING (filename)""")
        # Quantize the BASIC_8 projection. It is a subset of BASIC_10, so this gate applies to both presets.
        canonical = ", ".join(f"round({sql_column(n)}::DOUBLE,{2 if n in FLAGS else 1})" for n in BASIC_8)
        exact = ", ".join(map(sql_column, BASIC_8))
        canonical_sql = f"hash({canonical})"
        db.execute(f"CREATE VIEW signed AS SELECT *, {canonical_sql} AS signature, hash({exact}) AS exact_signature FROM input")
        audit = work / "signature-index.partial"
        lock = work / "manifest.json"
        if lock.exists():
            existing = read_json(lock)
            previous = [(s["file"], s["sha256"], s.get("split")) for s in existing["sources"]]
            incoming = [(s["file"], s["sha256"], s.get("split")) for s in manifest["sources"]]
            if previous != incoming or set(SPLITS) != existing["splits"].keys():
                raise ValueError("TEST_SPLIT_LOCKED_USE_NEW_WORKSPACE")
            if not all(digest(work / f"{split.lower()}.parquet") == existing["splits"][split]["sha256"] for split in SPLITS):
                raise ValueError("LOCKED_SPLIT_HASH_MISMATCH")
            manifest.update(existing)
            return
        db.execute(f"""COPY (
                   SELECT signature, min(rank) AS sole_rank, min(y) AS label, min(y) != max(y) AS conflicting,
                          count(*) AS rows, count(DISTINCT rank) AS split_count,
                          count(*) FILTER (WHERE rank=0) AS train_rows,
                          count(*) FILTER (WHERE rank=1) AS validation_rows,
                          count(*) FILTER (WHERE rank=2) AS test_rows
                   FROM signed GROUP BY signature
                   ) TO {sql_literal(audit.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        db.execute(f"CREATE VIEW signatures AS SELECT * FROM read_parquet({sql_literal(audit.as_posix())})")
        summary = db.execute("""SELECT count(*), coalesce(sum(rows) FILTER (WHERE split_count>1),0),
                    count(*) FILTER (WHERE split_count>1), coalesce(sum(rows) FILTER(WHERE conflicting),0)
                    FROM signatures""").fetchone()
        exact_count = db.execute("SELECT count(DISTINCT exact_signature) FROM signed").fetchone()[0]
        fixed = [[0] * len(SPLITS) for _ in (0, 1)]
        for label, rank, count in db.execute("""SELECT label, sole_rank, sum(rows) FROM signatures
                         WHERE NOT conflicting AND split_count=1 GROUP BY 1,2""").fetchall():
            fixed[label][rank] = int(count)
        shared = [[], []]
        for signature, label, train_rows, val_rows, test_rows in db.execute("""
            SELECT signature, label, train_rows, validation_rows, test_rows FROM signatures
            WHERE NOT conflicting AND split_count>1
            ORDER BY greatest(train_rows, validation_rows, test_rows) DESC, signature
        """).fetchall():
            shared[label].append((signature, (train_rows, val_rows, test_rows)))
        selections, available, caps = balanced_signatures(fixed, shared)
        db.register("selected_signatures", pa.table({
            "signature": pa.array((signature for signature, _ in selections), type=pa.uint64()),
            "selected_rank": pa.array((rank for _, rank in selections), type=pa.int8()),
        }))
        manifest["split_target"] = {split: TARGET[split] for split in SPLITS}
        manifest["split_balance"] = {
            "method": "SIGNATURE_ALLOCATION_THEN_CLASS_BALANCED_SEEDED_TRIM",
            "labels": {name: {"available_after_signature_allocation": dict(zip(SPLITS, counts)),
                              "retained_target": dict(zip(SPLITS, quotas))}
                       for name, counts, quotas in zip(("NORMAL", "ATTACK"), available, caps)},
            "rows_trimmed_to_ratio": sum(map(sum, available)) - sum(map(sum, caps)),
        }
        manifest["dedup"] = {"near_rule": "round flags to 0.01 and lengths to 0.1 byte in BASIC_8, use 64-bit hash",
                             "distinct_exact_B8": exact_count, "exact_duplicate_rows_B8": manifest["eligible"] - exact_count,
                             "distinct_near_signatures": summary[0], "rows_in_cross_split_signatures": summary[1],
                             "cross_split_signatures_before": summary[2], "conflicting_label_rows_removed": summary[3],
                             "priority": "Maximize attainable balanced retention among deterministic signature allocations; remove conflicting labels"}
        manifest["splits"] = {}
        for rank, split in enumerate(SPLITS):
            progress(f"materialize {split}")
            output = work / f"{split.lower()}.parquet"
            partial = output.with_suffix(".partial")
            dest = sql_literal(partial.as_posix())
            trim = "TRUE"
            if any(caps[label][rank] < available[label][rank] for label in (0, 1)):
                trim = (f"row_number() OVER (PARTITION BY y ORDER BY "
                        f"hash(source_sha256,row_id,{manifest['seed']}), source_name, row_id)"
                        f" <= CASE y WHEN 0 THEN {caps[0][rank]} ELSE {caps[1][rank]} END")
            query = f"""SELECT signed.* EXCLUDE(rank,exact_signature), {sql_literal(VERSION)} AS dataset_version
                        FROM signed JOIN signatures USING(signature)
                        LEFT JOIN selected_signatures USING(signature)
                        WHERE split={sql_literal(split)} AND rank=coalesce(selected_rank,sole_rank) AND NOT conflicting"""
            if trim != "TRUE":
                query += " QUALIFY " + trim
            db.execute(f"COPY ({query}) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)")
            partial.replace(output)
            result = db.execute(f"""SELECT y,family,scenario,count(*) FROM read_parquet({sql_literal(output.as_posix())})
                                  GROUP BY ALL ORDER BY 1,2,3""").fetchall()
            actual_counts = [sum(row[3] for row in result if row[0] == label) for label in (0, 1)]
            if actual_counts != [caps[0][rank], caps[1][rank]]:
                raise RuntimeError(f"{split}_CLASS_BALANCE_MISMATCH")
            manifest["splits"][split] = {"rows": sum(row[3] for row in result), "sha256": digest(output),
                                         "scenarios": [dict(zip(("y","family","scenario","rows"), row)) for row in result]}
            if sum(p.stat().st_size for p in work.rglob("*") if p.is_file()) > disk_gib * 2**30:
                raise ValueError("DISK_QUOTA_EXCEEDED")
        union = " UNION ALL ".join(f"SELECT DISTINCT signature, split FROM read_parquet({sql_literal((work / (s.lower()+'.parquet')).as_posix())})"
                                 for s in SPLITS)
        manifest["dedup"]["cross_split_signatures_after"] = db.execute(
            f"SELECT count(*) FROM (SELECT signature FROM ({union}) GROUP BY signature HAVING count(*)>1)").fetchone()[0]
        if manifest["dedup"]["cross_split_signatures_after"]:
            raise RuntimeError("CROSS_SPLIT_LEAKAGE")
        groups_union = " UNION ALL ".join(
            f"SELECT DISTINCT group_id, split FROM read_parquet({sql_literal((work / (split.lower()+'.parquet')).as_posix())})"
            for split in SPLITS)
        manifest["group_overlap"] = db.execute(
            f"SELECT count(*) FROM (SELECT group_id FROM ({groups_union}) GROUP BY group_id HAVING count(*)>1)").fetchone()[0]
        if manifest["group_overlap"]:
            raise RuntimeError("CROSS_SPLIT_GROUP_LEAKAGE")
        manifest["total_rows"] = sum(s["rows"] for s in manifest["splits"].values())
        for info in manifest["splits"].values():
            info["fraction"] = info["rows"] / manifest["total_rows"]
        manifest["removed_rows"] = manifest["eligible"] - manifest["total_rows"]
        train = sql_literal((work / "train.parquet").as_posix())
        cols = ", ".join(map(sql_column, BASIC_10))
        sample = db.execute(f"""SELECT {cols} FROM read_parquet({train})
                      USING SAMPLE reservoir(25000 ROWS) REPEATABLE ({manifest['seed']})""").fetchnumpy()
        values = np.column_stack([sample[n] for n in BASIC_10])
        with np.errstate(invalid='ignore', divide='ignore'):
            corr = np.corrcoef(values, rowvar=False)
        manifest["feature_audit"] = {"scope": "TRAIN_ONLY", "sample_rows": len(values),
            "stats": {n: {"min": float(min(v)), "max": float(max(v)), "std": float(np.std(v)),
                       "p05_p50_p95": np.quantile(v, [.05,.5,.95]).tolist(),
                       "modal_fraction": float(np.unique(v, return_counts=True)[1].max()/len(v))}
                       for n,v in zip(BASIC_10, values.T)},
            "correlation": {n: {m: float(v) if np.isfinite(v) else None for m,v in zip(BASIC_10,row)}
                            for n,row in zip(BASIC_10,corr)},
            "presets": {name: contract(name) for name in PRESETS}}
    audit.unlink(missing_ok=True)
    write_json(work / "manifest.json", manifest)
