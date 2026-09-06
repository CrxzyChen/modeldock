#!/usr/bin/env python3
"""Restore exact default deployment identities from a verified rollback database."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    source, target = args.source.resolve(strict=True), args.database.resolve(strict=True)
    if source == target or args.evidence.exists(): raise ValueError("restore_paths_invalid")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old:
        legacy = old.execute("SELECT kind,id FROM model_deployments WHERE is_default=1 ORDER BY kind").fetchall()
        if len(legacy) != 4 or len({kind for kind,_ in legacy}) != 4:
            raise ValueError("source_defaults_invalid")
    with sqlite3.connect(target) as db:
        db.execute("BEGIN IMMEDIATE")
        defaults = []
        for kind, identifier in legacy:
            current_identity = db.execute("SELECT incarnation FROM model_deployments WHERE id=? AND kind=?",
                                          (identifier,kind)).fetchone()
            if current_identity is None: raise ValueError("default_identity_changed")
            incarnation = current_identity[0]; defaults.append((kind,identifier,incarnation))
            row = db.execute("SELECT incarnation FROM model_deployments WHERE id=? AND kind=?", (identifier,kind)).fetchone()
            if row is None or row[0] != incarnation: raise ValueError("default_identity_changed")
            db.execute("UPDATE model_deployments SET is_default=0 WHERE kind=?", (kind,))
            if db.execute("UPDATE model_deployments SET is_default=1 WHERE id=? AND incarnation=?", (identifier,incarnation)).rowcount != 1:
                raise ValueError("default_restore_failed")
        current = db.execute("SELECT kind,id,incarnation FROM model_deployments WHERE is_default=1 ORDER BY kind").fetchall()
        if current != defaults or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("default_restore_verification_failed")
    evidence = {"schema":"mc.deployment-default-restore/1", "status":"passed",
                "source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
                "database":str(target), "defaults":[{"kind":k,"id":i,"incarnation":n} for k,i,n in defaults]}
    raw=(canonical(evidence)+"\n").encode(); args.evidence.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(args.evidence,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,"wb") as stream: stream.write(raw);stream.flush();os.fsync(stream.fileno())
    print(canonical({"status":"passed","defaults":len(defaults)}))


if __name__ == "__main__": main()
