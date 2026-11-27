"""Import local classification datasets with an explicit target and reproducible splits."""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .data import ROOT, SPLITS, connect, digest, sql_column as col, sql_literal as lit, write_json

DATASET_ROOT = ROOT / "security_ai" / "dataset"
PROCESSED = DATASET_ROOT / "processed"
DATASETS = {"CIC_38": PROCESSED / "CIC_38", "CIC_39": PROCESSED / "CIC_39"}
TRAIN_ROOT = ROOT / "security_ai" / "train"
IMPORTS = PROCESSED / "imported"
METADATA = {"label", "y", "target", "subclass", "scenario", "family", "source_name", "source_sha256",
            "row_id", "group_id", "signature", "split", "dataset_version", "original_label"}


def tabular_layout(manifest):
    order = manifest.get("features") if manifest else None
    classes = manifest.get("label_mapping") if manifest else None
    if (not isinstance(order, list) or not order or not isinstance(classes, dict) or len(classes) < 2
            or set(classes.values()) != set(range(len(classes)))):
        return None
    return tuple(order), classes


def source_tables(path, split_paths=None):
    if split_paths is not None:
        if set(split_paths) != set(SPLITS):
            raise ValueError("Specify all three split files")
        selected = {key: Path(split_paths[key]).expanduser().resolve() for key in SPLITS}
        if (len(set(selected.values())) != len(SPLITS)
                or any(not file.is_file() or file.suffix.lower() not in (".csv", ".parquet", ".pq")
                       for file in selected.values())):
            raise ValueError("Train, Validation and Test must be separate CSV/Parquet files")
        return selected
    if path.is_file() and path.suffix.lower() in (".csv", ".parquet", ".pq"):
        return {"ALL": path}
    if path.is_dir():
        result = {}
        for split in SPLITS:
            stem = split.lower()
            aliases = {"validation", "val", "valid"} if split == "VALIDATION" else {stem}
            matches = sorted(p for p in path.iterdir() if p.is_file()
                             and p.stem.casefold() in aliases and p.suffix.lower() in (".csv", ".parquet", ".pq"))
            if len(matches) != 1:
                raise ValueError(f"Expected one {stem} CSV or Parquet in {path}")
            result[split] = matches[0]
        return result
    raise ValueError("Select a CSV/Parquet file or a folder with train, validation, and test files")


def relation(path):
    quoted = lit(path.as_posix())
    return (f"read_csv({quoted}, header=true, all_varchar=true)" if path.suffix.lower() == ".csv"
            else f"read_parquet({quoted})")


def import_dataset(path, target="", features=(), seed=42, memory_mib=2048, threads=2, destination=IMPORTS, split_paths=None):
    path = Path(path).expanduser().resolve()
    tables = source_tables(path, split_paths)
    if split_paths is not None:
        path = tables["TRAIN"].parent
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / uuid4().hex
    pending = final.with_suffix(".pending")
    with TemporaryDirectory(prefix="import-", dir=destination) as working, ExitStack() as cleanup:
        def remove_pending():
            if pending.exists():
                for name in ("train.parquet", "validation.parquet", "test.parquet", "manifest.json", "manifest.tmp"):
                    (pending / name).unlink(missing_ok=True)
                pending.rmdir()
        cleanup.callback(remove_pending)
        with connect(Path(working), max(128, memory_mib // 2), 32, threads) as db:
            schemas = {split: [row[0] for row in db.execute(f"DESCRIBE SELECT * FROM {relation(file)}").fetchall()]
                       for split, file in tables.items()}
            header = next(iter(schemas.values()))
            target = target.strip() or next((name for name in ("label", "y", "target") if name in header), "")
            if not target or any(target not in schema for schema in schemas.values()):
                raise ValueError("Specify the exact label column present in every source file")
            order = tuple(features) or tuple(name for name in header if name.casefold() not in METADATA and name != target)
            if (not order or len(order) != len(set(name.casefold() for name in order))
                    or any(name == target or name.casefold() in METADATA or name.startswith("__") for name in order)
                    or any(not set(order) <= set(schema) for schema in schemas.values())):
                raise ValueError("Features must be distinct columns available in every file and exclude labels and reserved metadata")
            keys = ", ".join(map(col, order))
            parts = []
            source_audit = []
            for rank, (split, file) in enumerate(tables.items()):
                bindings = ", ".join(f"nullif(trim(CAST({col(name)} AS VARCHAR)), '') AS {col(name)}" for name in order)
                parts.append(f"""SELECT {bindings}, nullif(trim(CAST({col(target)} AS VARCHAR)), '') AS __label,
                     {lit(split)} AS __split, {rank} AS __rank FROM {relation(file)}""")
                source_audit.append({"file": str(file), "sha256": digest(file)})
            db.execute(f"CREATE TABLE input AS SELECT *, hash({keys}) AS __sig FROM ({' UNION ALL '.join(parts)})")
            counts = db.execute("SELECT count(*), count(*) FILTER(WHERE __label IS NULL) FROM input").fetchall()[0]
            class_names = [row[0] for row in db.execute("SELECT DISTINCT __label FROM input WHERE __label IS NOT NULL ORDER BY 1").fetchall()]
            if not 2 <= len(class_names) <= 100:
                raise ValueError("A classification dataset must contain between 2 and 100 distinct labels")
            db.execute("""CREATE TABLE unique_rows AS
                 SELECT * FROM input WHERE __label IS NOT NULL
                 QUALIFY min(__label) OVER (PARTITION BY __sig) = max(__label) OVER (PARTITION BY __sig)
                  AND row_number() OVER (PARTITION BY __sig ORDER BY __rank) = 1""")
            unique = db.execute("SELECT count(*) FROM unique_rows").fetchall()[0][0]
            if "ALL" in tables:
                db.execute(f"""CREATE VIEW split_rows AS SELECT * EXCLUDE (__split),
                    CASE WHEN pos<=greatest(1,least(n-2,floor(n*.70))) THEN 'TRAIN'
                         WHEN pos<=greatest(2,least(n-1,floor(n*.85))) THEN 'VALIDATION'
                         ELSE 'TEST' END AS __split FROM (
                    SELECT *,row_number() OVER(PARTITION BY __label ORDER BY hash(__sig,{seed}),__sig) AS pos,
                    count(*) OVER(PARTITION BY __label) AS n FROM unique_rows)""")
            else:
                db.execute("CREATE VIEW split_rows AS SELECT * FROM unique_rows")
            covered = db.execute("SELECT DISTINCT __split,__label FROM split_rows").fetchall()
            if set(covered) != {(split, name) for split in SPLITS for name in class_names}:
                raise ValueError("Each label needs rows in train, validation, and test after duplicate removal")
            inferred = ", ".join(f"count(*) FILTER(WHERE {col(name)} IS NOT NULL "
                                  f"AND try_cast({col(name)} AS DOUBLE) IS NULL)" for name in order)
            text_cols = db.execute(f"SELECT {inferred} FROM split_rows WHERE __split='TRAIN'").fetchall()[0]
            transforms, encoders = [], {}
            for name, categorical in zip(order, text_cols):
                quoted = col(name)
                if categorical:
                    values = [row[0] for row in db.execute(f"""SELECT {quoted} FROM split_rows
                        WHERE __split='TRAIN' AND {quoted} IS NOT NULL
                        GROUP BY {quoted} ORDER BY count(*) DESC,{quoted} LIMIT 256""").fetchall()]
                    cases = " ".join(f"WHEN {lit(value)} THEN {i}" for i, value in enumerate(values))
                    expression = f"CASE {quoted} {cases} ELSE -1 END"
                    encoders[name] = {"method": "ordinal", "categories": values, "unknown": -1}
                else:
                    numeric = f"TRY_CAST({quoted} AS FLOAT)"
                    expression = f"CASE WHEN isfinite({numeric}) THEN {numeric} END"
                transforms.append(f"({expression})::FLOAT AS {quoted}")
            labels = dict(zip(class_names, range(len(class_names))))
            codes = " ".join(f"WHEN {lit(name)} THEN {key}" for name, key in labels.items())
            splits = {}
            pending.mkdir()
            for split in SPLITS:
                out = pending / f"{split.lower()}.parquet"
                db.execute(f"""COPY (SELECT {', '.join(transforms)}, __label AS label, __label AS subclass,
                    (CASE __label {codes} END)::INTEGER AS y FROM split_rows WHERE __split={lit(split)})
                    TO {lit(out.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)""")
                totals = dict(db.execute(f"SELECT label,count(*) FROM read_parquet({lit(out.as_posix())}) GROUP BY label").fetchall())
                splits[split] = {"rows": sum(totals.values()), "labels": totals, "subclasses": totals, "sha256": digest(out)}
            total_rows = sum(split["rows"] for split in splits.values())
            for entry in splits.values():
                entry["fraction"] = entry["rows"] / total_rows
            manifest = {"version": "studio-tabular-1", "title": path.name, "source": str(path), "seed": seed,
                        "features": order, "label_mapping": labels, "categorical_encoders": encoders,
                        "total_rows": total_rows, "splits": splits, "source_files": source_audit,
                        "input": {"files": len(tables), "rows": counts[0], "missing_label_rows": counts[1]},
                        "dedup": {"duplicate_or_conflicting_rows_removed": counts[0]-counts[1]-unique}}
            write_json(pending / "manifest.json", manifest)
        pending.rename(final)
    return final, manifest
