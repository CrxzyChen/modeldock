"""Runtime v1 offline contract checker and fixed build-plan generator.

This module does not download, install, start a builder or certify a build report.
Executable builds require independently approved release and preparation hashes,
all offline artifacts and a live, exactly bound private remote builder. These
are release prerequisites, not booleans an operator can override. Plans are NOT
cold/warm build, import, daemon-exit or network/disk measurement evidence.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import socket
import struct
import subprocess
import tarfile
import threading
import gzip
import io
from contextlib import contextmanager
from email.parser import BytesParser
from urllib.parse import urlsplit, unquote
import zipfile

SHA = re.compile(r"[0-9a-f]{64}\Z")
SDK = (
    "mediacenter/__init__.py", "mediacenter/domain.py", "mediacenter/capabilities.py",
    "mediacenter/protocol.py", "mediacenter/worker_common.py", "mediacenter/transport.py",
    "mediacenter/redis_transport.py", "mediacenter/adapter.py",
    "mediacenter/worker_journal.py", "mediacenter/worker_runtime.py",
)
TARGET = {"python": "3.11.15", "implementation": "cpython", "os": "linux",
          "architecture": "x86_64", "glibc": "2.35"}
MARKERS = {"python_version": "3.11", "python_full_version": "3.11.15",
           "implementation_name": "cpython", "implementation_version": "3.11.15",
           "platform_python_implementation": "CPython", "os_name": "posix",
           "sys_platform": "linux", "platform_system": "Linux",
           "platform_machine": "x86_64", "extra": ""}
CORE = {"torch": "2.7.1+cu126", "torchvision": "0.22.1+cu126",
        "diffusers": "0.35.1", "transformers": "4.51.3", "accelerate": "1.6.0",
        "redis": "5.3.1", "pip": "25.1.1", "setuptools": "80.9.0", "wheel": "0.45.1"}
PARENT = "sha256:79676deb51ebb02885b0b9d33788e78a37cf1045ad79d1bb04c6a222c3556b3d"


class ContractError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise ContractError(code)


def normalized(name):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name),
            "invalid_package_name")
    return re.sub(r"[-_.]+", "-", name).lower()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractError("nonfinite_json")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invalid_json") from None


def safe_path(root, relative, directory=False):
    root = Path(root).absolute()
    p = PurePosixPath(relative)
    require(isinstance(relative, str) and "\\" not in relative and not p.is_absolute()
            and p.parts and all(x not in (".", "..") for x in p.parts)
            and ":" not in relative, "unsafe_path")
    # Check the root and every ancestor too; resolve() alone silently accepts links.
    target = root.joinpath(*p.parts)
    for path in [*reversed(target.parents), target]:
        try:
            info = path.lstat()
        except OSError:
            raise ContractError("input_missing") from None
        require(not stat.S_ISLNK(info.st_mode) and not
                (getattr(info, "st_file_attributes", 0) & 0x400), "symlink_input")
    require(target.is_dir() if directory else target.is_file(), "input_type")
    return target


def read_file(path, limit):
    require(type(limit) is int and 0 <= limit <= 64 * 1024**3, "invalid_byte_budget")
    before = path.stat()
    require(before.st_size <= limit, "input_byte_budget")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    after = path.stat()
    require(len(data) <= limit, "input_byte_budget")
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "input_changed")
    return data


def verify_file(root, entry):
    require(type(entry.get("size")) is int and 0 < entry["size"] <= 64 * 1024**3
            and isinstance(entry.get("sha256"), str) and SHA.fullmatch(entry["sha256"]),
            "artifact_identity_missing")
    path = safe_path(root, entry["filename"])
    before = path.stat()
    require(before.st_size == entry["size"], "artifact_size_mismatch")
    count, digest = 0, hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(min(1024**2, entry["size"] - count + 1))
            if not chunk:
                break
            count += len(chunk)
            require(count <= entry["size"], "artifact_size_mismatch")
            digest.update(chunk)
    after = path.stat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "input_changed")
    require(count == entry["size"] and digest.hexdigest() == entry["sha256"],
            "artifact_hash_mismatch")
    return path


def version(value):
    # All approved pins are final releases. Unsupported PEP440 forms fail closed,
    # never fall back to lexical ordering (e.g. 3.9 > 3.11).
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)*)(?:\+([a-z0-9.]+))?", value)
    require(m is not None, "unsupported_version")
    parts = tuple(int(x) for x in m[1].split("."))
    return parts + (0,) * (8 - len(parts)), m[2]


def satisfies(actual, specification):
    if not specification.strip():
        return True
    actual_parts, actual_local = version(actual)
    for item in specification.strip().strip("()").split(","):
        m = re.fullmatch(r"\s*(===|~=|==|!=|>=|<=|>|<)\s*([^\s]+)\s*", item)
        require(m is not None, "unsupported_specifier")
        op, expected = m.groups()
        if expected.endswith(".*"):
            require(op in ("==", "!="), "unsupported_specifier")
            prefix = tuple(map(int, expected[:-2].split(".")))
            match = actual_parts[:len(prefix)] == prefix
            ok = match if op == "==" else not match
        else:
            parts, local = version(expected)
            equal = actual_parts == parts and (local is None or local == actual_local)
            if op == "~=":
                width = len(expected.split(".")) - 1
                require(width > 0 and local is None, "unsupported_specifier")
                ok = actual_parts >= parts and actual_parts[:width] == parts[:width]
            else:
                ok = {"==": equal, "!=": not equal, ">=": actual_parts >= parts,
                      "<=": actual_parts <= parts, ">": actual_parts > parts,
                      "<": actual_parts < parts, "===": actual == expected}[op]
        if not ok:
            return False
    return True


def marker_matches(expression, extra=""):
    if not expression:
        return True
    environment = dict(MARKERS, extra=extra)
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (ValueError, SyntaxError, RecursionError):
        raise ContractError("unsupported_marker") from None
    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name):
            require(node.id in environment, "unsupported_marker_variable")
            return environment[node.id]
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return node.value
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [visit(x) for x in node.values]
            require(all(type(x) is bool for x in values), "unsupported_marker")
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right, op = visit(node.left), visit(node.comparators[0]), node.ops[0]
            require(type(left) is str and type(right) is str, "unsupported_marker")
            if isinstance(op, ast.In):
                return left in right
            if isinstance(op, ast.NotIn):
                return left not in right
            versioned = bool(re.fullmatch(r"\d+(?:\.\d+)*", left) and
                             re.fullmatch(r"\d+(?:\.\d+)*", right))
            if versioned:
                left, right = version(left)[0], version(right)[0]
            choices = {ast.Eq: left == right, ast.NotEq: left != right,
                       ast.Lt: left < right, ast.LtE: left <= right,
                       ast.Gt: left > right, ast.GtE: left >= right}
            require(type(op) in choices, "unsupported_marker")
            return choices[type(op)]
        raise ContractError("unsupported_marker")
    result = visit(tree)
    require(type(result) is bool, "unsupported_marker")
    return result


def requirement(text, extras=("",)):
    require(type(text) is str and len(text) <= 4096, "invalid_requirement")
    declaration, _, marker = text.partition(";")
    if not any(marker_matches(marker, extra) for extra in extras):
        return None
    m = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([A-Za-z0-9,_.-]+)\])?\s*(.*?)\s*",
                     declaration)
    require(m is not None and "@" not in declaration, "unsupported_requirement")
    return normalized(m[1]), m[3], tuple(normalized(x) for x in (m[2] or "").split(",") if x)


def compatible_wheel(filename):
    require(type(filename) is str and "/" not in filename and "\\" not in filename
            and filename.endswith(".whl"), "invalid_wheel_filename")
    parts = filename[:-4].split("-")
    require(len(parts) in (5, 6), "invalid_wheel_filename")
    py, abi, platforms = parts[-3:]
    python_ok = "py3" in py.split(".") or "cp311" in py.split(".")
    if abi == "abi3" and re.fullmatch(r"cp3[0-9]+", py):
        python_ok = 2 <= int(py[3:]) <= 11
    require(python_ok and abi in ("none", "abi3", "cp311"), "wheel_python_mismatch")
    compatible = False
    for tag in platforms.split("."):
        if tag == "any" and abi == "none":
            compatible = True
        elif tag in ("manylinux1_x86_64", "manylinux2010_x86_64", "manylinux2014_x86_64"):
            compatible = True
        else:
            m = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", tag)
            if m and (int(m[1]), int(m[2])) <= (2, 35):
                compatible = True
    require(compatible, "wheel_platform_mismatch")
    return parts[0], parts[1]


def validate_python_lock(lock):
    require(lock.get("schema") == 1 and lock.get("target") == TARGET, "python_target_mismatch")
    packages = {}
    for row in lock["packages"]:
        name = normalized(row["name"])
        require(name not in packages, "duplicate_package")
        wheel_name, wheel_version = compatible_wheel(row["filename"])
        require(normalized(wheel_name) == name and wheel_version == row["version"],
                "wheel_identity_mismatch")
        require(satisfies(TARGET["python"], row["requires_python"] or ""), "requires_python_mismatch")
        require(type(row["size"]) is int and row["size"] > 0 and SHA.fullmatch(row["sha256"]),
                "artifact_identity_missing")
        url = urlsplit(row["url"])
        require(url.scheme == "https" and url.hostname in ("files.pythonhosted.org", "download.pytorch.org", "download-r2.pytorch.org")
                and not url.username and not url.password and not url.query and not url.fragment
                and unquote(url.path.rsplit("/", 1)[-1]) == row["filename"], "artifact_url_mismatch")
        require(row.get("metadata_scope") in ("official-project-release-metadata", "official-exact-wheel-metadata")
                and SHA.fullmatch(row["metadata_sha256"]), "metadata_provenance_missing")
        packages[name] = row
    roots = {normalized(k): v for k, v in lock["required_roots"].items()}
    require(all(roots.get(k) == v for k, v in CORE.items()), "core_version_changed")
    for name, pinned in roots.items():
        require(name in packages and packages[name]["version"] == pinned, "root_missing")
    requested = {name: {""} for name in roots}
    checked, edges = {}, set()
    while any(checked.get(name) != extras for name, extras in requested.items()):
        for name, extras in list(requested.items()):
            if checked.get(name) == extras:
                continue
            checked[name] = set(extras)
            for text in packages[name]["requires_dist"]:
                parsed = requirement(text, extras)
                if parsed is None:
                    continue
                target, spec, added_extras = parsed
                require(target in packages, "dependency_missing")
                require(satisfies(packages[target]["version"], spec), "dependency_version_conflict")
                requested.setdefault(target, {""}).update(added_extras)
                edges.add((name, target, spec))
    require(set(packages) == set(requested), "unreachable_package")
    return {"packages": len(packages), "artifact_bytes": sum(p["size"] for p in packages.values()),
            "edges": [list(x) for x in sorted(edges)], "closure": "declaration_complete",
            "artifact_metadata": "not_verified"}


def verify_wheel(root, row):
    path = verify_file(root, row)
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            require(len(members) <= 100000, "wheel_members_limit")
            names = [m.filename for m in members]
            require(len(set(names)) == len(names), "duplicate_wheel_member")
            for member in members:
                p = PurePosixPath(member.filename)
                require(not p.is_absolute() and ".." not in p.parts and "\\" not in member.filename
                        and not stat.S_ISLNK(member.external_attr >> 16), "unsafe_wheel_member")
            metadata = [m for m in members if m.filename.endswith(".dist-info/METADATA")]
            require(len(metadata) == 1 and metadata[0].file_size <= 2 * 1024**2, "wheel_metadata_limit")
            with archive.open(metadata[0]) as stream:
                raw = stream.read(2 * 1024**2 + 1)
            require(len(raw) <= 2 * 1024**2, "wheel_metadata_limit")
            message = BytesParser().parsebytes(raw)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        raise ContractError("invalid_wheel") from None
    require(normalized(message["Name"]) == normalized(row["name"])
            and message["Version"] == row["version"]
            and (message["Requires-Python"] or "") == (row["requires_python"] or "")
            and sorted(message.get_all("Requires-Dist", [])) == sorted(row["requires_dist"]),
            "wheel_metadata_mismatch")
    return {"name": row["name"], "sha256": row["sha256"], "metadata_sha256": hash_bytes(raw)}


def verify_oci(root, base):
    layout = strict_json(read_file(safe_path(root, "oci-layout"), 4096))
    require(layout == {"imageLayoutVersion": "1.0.0"}, "oci_layout_version")
    index = strict_json(read_file(safe_path(root, "index.json"), 1024**2))
    require(index.get("schemaVersion") == 2, "oci_schema")
    descriptors = index.get("manifests", [])
    matches = [d for d in descriptors if d.get("digest") == base["manifest"]]
    require(len(matches) == 1, "oci_parent_missing")
    def blob(descriptor, limit=None):
        digest = descriptor.get("digest", "")
        require(digest.startswith("sha256:") and SHA.fullmatch(digest[7:]), "oci_digest")
        require(type(descriptor.get("size")) is int and descriptor["size"] > 0, "oci_size")
        entry = {"filename": "blobs/sha256/" + digest[7:], "sha256": digest[7:], "size": descriptor["size"]}
        path = verify_file(root, entry)
        return strict_json(read_file(path, limit)) if limit else None
    manifest = blob(matches[0], 1024**2)
    require(manifest.get("schemaVersion") == 2 and manifest["config"]["digest"] == base["config"], "oci_config_mismatch")
    config = blob(manifest["config"], 1024**2)
    require(config.get("os") == "linux" and config.get("architecture") == "amd64", "oci_platform_mismatch")
    require([{k: item.get(k) for k in ("digest", "size")} for item in manifest["layers"]] ==
            base["layers"], "oci_layers_mismatch")
    for layer in manifest["layers"]:
        blob(layer)
    return {"manifest": base["manifest"], "blobs_verified": 2 + len(manifest["layers"])}


def deb_version_compare(a, b):
    """Debian epoch/upstream/revision ordering, including tilde and digit runs."""
    def split(value):
        require(type(value) is str and re.fullmatch(r"[0-9A-Za-z.+:~_-]+", value), "invalid_deb_version")
        epoch, tail = value.split(":", 1) if ":" in value else ("0", value)
        require(epoch.isdigit(), "invalid_deb_version")
        upstream, revision = tail.rsplit("-", 1) if "-" in tail else (tail, "0")
        return int(epoch), upstream, revision
    def order(char):
        if char == "~": return -1
        if not char: return 0
        if char.isalpha(): return ord(char)
        return ord(char) + 256
    def part(left, right):
        while left or right:
            while (left and not left[0].isdigit()) or (right and not right[0].isdigit()):
                lc = left[0] if left and not left[0].isdigit() else ""
                rc = right[0] if right and not right[0].isdigit() else ""
                if order(lc) != order(rc): return (order(lc) > order(rc)) * 2 - 1
                left, right = left[bool(lc):], right[bool(rc):]
            lm = re.match(r"\d*", left)[0]
            rm = re.match(r"\d*", right)[0]
            li, ri = lm.lstrip("0"), rm.lstrip("0")
            if len(li) != len(ri): return (len(li) > len(ri)) * 2 - 1
            if li != ri: return (li > ri) * 2 - 1
            left, right = left[len(lm):], right[len(rm):]
        return 0
    ae, au, ar = split(a)
    be, bu, br = split(b)
    return ((ae > be) - (ae < be)) or part(au, bu) or part(ar, br)


def deb_groups(text):
    groups = []
    if not text:
        return groups
    for group in text.split(","):
        alternatives = []
        for declaration in group.split("|"):
            m = re.fullmatch(r"\s*([a-z0-9][a-z0-9+.-]*(?::(?:any|native|amd64))?)\s*(?:\((<<|>>|<=|>=|=|<|>)\s*([^\s)]+)\))?\s*", declaration)
            require(m is not None, "unsupported_deb_relation")
            alternatives.append([m[1], m[3] or "", {"<<": "<", ">>": ">"}.get(m[2], m[2] or "")])
        groups.append(alternatives)
    return groups


def deb_satisfies(version_value, required, operator):
    if not operator:
        return True
    c = deb_version_compare(version_value, required)
    return {"=": c == 0, ">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0}[operator]


def validate_system_lock(system):
    require(system.get("schema") == 1 and system["base"]["manifest"] == PARENT, "system_identity")
    require(len(system["repositories"]) == 3 and not system["unresolved"], "system_closure_incomplete")
    packages = {p["Package"]: p for p in system["packages"]}
    require(len(packages) == len(system["packages"]), "duplicate_system_package")
    base = {p["package"]: p for p in system["base_inventory"]["packages"]}
    indices = {r["index"]["decompressed_file"]: r for r in system["repositories"]}
    for p in packages.values():
        require(p["Architecture"] in ("amd64", "all") and int(p["Size"]) > 0
                and SHA.fullmatch(p["SHA256"]) and SHA.fullmatch(p["_stanza_sha256"]), "system_package_identity")
        require(p["_index"] in indices and p["_suite"] == indices[p["_index"]]["suite"], "system_index_identity")
        require(p["URL"] == "https://snapshot.ubuntu.com/ubuntu/20260811T000000Z/" + p["Filename"]
                and re.fullmatch(r"pool/main/[A-Za-z0-9+./_%~-]+\.deb", p["Filename"]), "system_package_url")
    for name, p in base.items():
        require(name in packages and packages[name]["Version"] == p["version"] and
                packages[name]["Architecture"] == p["architecture"], "system_base_changed")
    summary = {}
    for stage_name, stage in system["stages"].items():
        require(stage_name in ("build", "runtime") and not stage["conflicts"] and not stage["replacements"],
                "system_stage_conflict")
        names = stage["packages"]
        require(len(set(names)) == len(names) and set(names) <= packages.keys() and
                base.keys() <= set(names) and set(stage["roots"]) <= set(names), "system_stage_incomplete")
        require(set(stage["added"]) == set(names) - base.keys(), "system_delta_mismatch")
        expected = []
        for name in names:
            for field in ("Depends", "Pre-Depends"):
                for group in deb_groups(packages[name].get(field, "")):
                    expected.append((name, field, group))
        require(len(expected) == len(stage["edges"]), "system_dependency_edges_missing")
        remaining = list(expected)
        for edge in stage["edges"]:
            identity = (edge["from"], edge["field"], edge["alternatives"])
            require(identity in remaining, "system_dependency_edge_changed")
            remaining.remove(identity)
            chosen = edge["selected_dependency"]
            require(chosen in edge["alternatives"] and edge["provider"] in names, "system_provider_missing")
            provider = packages[edge["provider"]]
            require(provider["Version"] == edge["provider_version"], "system_provider_version_changed")
            requested = chosen[0].split(":", 1)[0]
            provided_version = provider["Version"] if requested == provider["Package"] else None
            if provided_version is None:
                provided = [x for group in deb_groups(provider.get("Provides", "")) for x in group if x[0] == requested]
                require(len(provided) == 1, "system_provider_missing")
                provided_version = provided[0][1]
            require((not chosen[2]) or (provided_version and deb_satisfies(provided_version, chosen[1], chosen[2])),
                    "system_dependency_version_conflict")
        count = sum(int(packages[n]["Size"]) for n in stage["added"])
        require(count == stage["incremental_deb_bytes"], "system_delta_mismatch")
        summary[stage_name] = {"packages": len(names), "added": len(stage["added"]), "artifact_bytes": count}
    require(set(system["stages"]["runtime"]["packages"]) <= set(system["stages"]["build"]["packages"]),
            "runtime_not_build_subset")
    expected_final = [{"name": n, "version": packages[n]["Version"], "architecture": packages[n]["Architecture"]}
                      for n in system["stages"]["runtime"]["packages"]]
    require(system["final_inventory"] == expected_final, "final_inventory_mismatch")
    return summary


def verify_system_artifacts(root, system):
    """Check supplied signed-index bytes and each pinned deb; no apt execution.

    The fixed InRelease/Packages hashes were authenticated during lock creation.
    Revalidating these exact bytes does not trust a caller's signature=true flag.
    """
    validate_system_lock(system)
    for repository in system["repositories"]:
        signed = repository["inrelease"]
        verify_file(root, {"filename": "metadata/" + repository["suite"] + ".InRelease",
                           "size": signed["bytes"], "sha256": signed["sha256"]})
        index = repository["index"]
        index_path = verify_file(root, {"filename": "metadata/" + index["decompressed_file"],
                                       "size": index["decompressed_size"], "sha256": index["decompressed_sha256"]})
        body = read_file(index_path, 64 * 1024**2)
        require(hash_bytes(body) == index["decompressed_sha256"], "input_changed")
        verify_package_stanzas(body, [p for p in system["packages"] if p["_index"] == index["decompressed_file"]])
    added = set(system["stages"]["build"]["added"]) | set(system["stages"]["runtime"]["added"])
    for p in system["packages"]:
        if p["Package"] in added:
            verify_file(root, {"filename": p["Filename"].rsplit("/", 1)[-1], "size": int(p["Size"]), "sha256": p["SHA256"]})
    return {"deb_files_verified": len(added), "signature": "bytes_match_declared_preverified_index",
            "authorization": "requires_independent_release_digest"}


def verify_package_stanzas(body, packages):
    wanted = {p["_stanza_sha256"]: p for p in packages}
    found = set()
    for block in body.split(b"\n\n"):
        digest = hash_bytes(block)
        if digest not in wanted: continue
        fields, key = {}, None
        for line in block.decode("utf-8").splitlines():
            if line.startswith((" ", "\t")):
                require(key is not None, "invalid_package_stanza")
                fields[key] += "\n" + line
            else:
                key, separator, value = line.partition(":")
                require(separator and key not in fields, "invalid_package_stanza")
                fields[key] = value.lstrip()
        row = wanted[digest]
        for key in ("Package", "Version", "Architecture", "Source", "Essential", "Priority", "Multi-Arch",
                    "Pre-Depends", "Depends", "Provides", "Conflicts", "Breaks", "Replaces", "Filename", "Size", "SHA256", "Installed-Size"):
            require(fields.get(key) == row.get(key), "package_stanza_mismatch")
        found.add(digest)
    require(found == wanted.keys(), "package_stanza_missing")


def verify_contract(root, artifacts=None, sdk_source=None, parent_oci=None, approved_release_sha256=None):
    root = Path(root)
    release_path = safe_path(root, "containers/runtime-v1/release.json")
    release_bytes = read_file(release_path, 2 * 1024**2)
    if approved_release_sha256 is not None:
        require(type(approved_release_sha256) is str and SHA.fullmatch(approved_release_sha256)
                and hash_bytes(release_bytes) == approved_release_sha256, "release_approval_mismatch")
    release = strict_json(release_bytes)
    locks = {}
    for name in ("python.lock", "system.lock", "Dockerfile"):
        raw = read_file(safe_path(root, "containers/runtime-v1/" + name), 4 * 1024**2)
        require(hash_bytes(raw) == release["inputs"][name], "release_input_changed")
        locks[name] = raw if name == "Dockerfile" else strict_json(raw)
    python_result = validate_python_lock(locks["python.lock"])
    system = locks["system.lock"]
    system_result = validate_system_lock(system)
    require(release["schema"] == 1 and release["runtime"] == "mediacenter-runtime-v1"
            and release["platform"] == "linux/amd64" and system["base"]["manifest"] == PARENT,
            "release_identity_mismatch")
    require(release["final_image"] is None, "unverified_final_image_claim")
    require(tuple(x["path"] for x in release["sdk"]) == SDK, "sdk_allowlist_mismatch")
    sdk_root = Path(sdk_source or root)
    for entry in release["sdk"]:
        verify_file(sdk_root, dict(entry, filename=entry["path"]))
    unresolved = list(system["unresolved"]) + list(release["unresolved"])
    # No marker/status string can supply the missing authenticated apt/daemon
    # evidence. These are explicit capability gaps, not optional checks.
    if not system.get("repositories") or not system.get("packages"):
        unresolved.append("authenticated_system_package_closure_missing")
    wheel_evidence = []
    if artifacts:
        for row in locks["python.lock"]["packages"]:
            wheel_evidence.append(verify_wheel(artifacts, row))
        verify_file(artifacts, release["python_source"])
        verify_system_artifacts(artifacts, system)
    oci = verify_oci(parent_oci, system["base"]) if parent_oci else None
    return {"schema": 1, "mode": "verify_only", "release_sha256": hash_bytes(release_bytes),
            "approval_digest_matched": approved_release_sha256 is not None,
            "python": python_result, "system": system_result, "sdk_files": len(SDK), "wheel_files_verified": len(wheel_evidence),
            "parent_oci": oci, "unresolved": sorted(set(unresolved)),
            "build_ready": False, "build": "not_executed", "offline_import": "not_executed",
            "final_image": None, "network_bytes": None, "disk_peak_bytes": None,
            "cold_warm_comparison": "not_executed", "daemon_exit": "not_observed"}


def fixed_build_argv(buildx, builder, endpoint, context, parent_oci, output, mode):
    """Pure plan, not a runnable authorization or a validated daemon attestation.

    The separately prepared builder MUST be remote with exactly this endpoint.
    The executable entrypoint remains closed until authenticated locks, daemon
    identity/resource observation and post-build inspection are implemented.
    """
    require(re.fullmatch(r"mc-runtime-[a-z0-9-]{1,48}", builder) is not None, "invalid_builder_name")
    require(re.fullmatch(r"unix:///[^\s,?#]+", endpoint) is not None and
            ".." not in PurePosixPath(endpoint[7:]).parts, "invalid_builder_endpoint")
    require(mode in ("cold", "warm"), "invalid_cache_mode")
    for path in (buildx, context, parent_oci, output):
        require(isinstance(path, str) and path.startswith("/") and
                not any(c in path for c in (",", "\n", "\r", "\0")) and
                ".." not in PurePosixPath(path).parts, "invalid_build_path")
    argv = [buildx, "--builder", builder, "build", "--platform", "linux/amd64",
            "--network", "none", "--pull=false", "--provenance=false", "--sbom=false",
            "--build-context", "runtime_parent=oci-layout://" + parent_oci + "@" + PARENT,
            "--output", "type=oci,dest=" + output, "--progress", "plain"]
    if mode == "cold":
        argv.append("--no-cache")
    return {"argv": argv + [context], "endpoint": endpoint, "driver": "remote",
            "cache_mode": mode, "cold_definition": "ignore instruction cache; content store not asserted empty",
            "status": "plan_only", "daemon_exit_on_client_timeout": "unknown"}


def private_output(path):
    """Exclusive directory; callers retain it on every failure, never prune."""
    path = Path(path).absolute()
    safe_path(path.parent.parent, path.parent.name, directory=True)
    require(not path.exists(), "output_exists")
    path.mkdir(mode=0o700)
    return path


def write_exclusive(path, data):
    with path.open("xb") as stream:
        if os.name == "posix": os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def object_identity(path, kind):
    info = Path(path).lstat()
    require(not stat.S_ISLNK(info.st_mode), "preparation_symlink")
    match = {"file": stat.S_ISREG, "directory": stat.S_ISDIR, "socket": stat.S_ISSOCK}[kind]
    require(match(info.st_mode), "preparation_object_type")
    return {"dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}


def proc_identity(pid):
    require(type(pid) is int and pid > 1, "invalid_daemon_pid")
    text = Path("/proc", str(pid), "stat").read_text()
    fields = text[text.rfind(")") + 2:].split()
    return {"pid": pid, "start_ticks": int(fields[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def cgroup_mount_binding(mountinfo, directory, membership):
    """Bind a real filesystem path to the v2 membership through mount root."""
    path = PurePosixPath(directory)
    require(path.is_absolute() and ".." not in path.parts, "invalid_cgroup_path")
    candidates = []
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        require(bool(separator), "invalid_mountinfo")
        fields, fs = left.split(), right.split()
        if len(fields) < 6 or len(fs) < 3: raise ContractError("invalid_mountinfo")
        # Spaces/backslashes in a delegated build root are intentionally not a
        # supported path dialect; never guess an escaped mount correspondence.
        if "\\" in fields[4]: continue
        point = PurePosixPath(fields[4])
        if point == path or point in path.parents:
            candidates.append((len(point.parts), fields, fs))
    require(bool(candidates), "cgroup_mount_missing")
    deepest = max(row[0] for row in candidates)
    require(sum(row[0] == deepest for row in candidates) == 1, "ambiguous_cgroup_mount")
    _, fields, fs = max(candidates, key=lambda row: row[0])
    require(fs[0] == "cgroup2", "not_cgroup2_mount")
    relative = path.relative_to(PurePosixPath(fields[4]))
    actual = str(PurePosixPath(fields[3]) / relative)
    require(actual == membership, "cgroup_membership_path_mismatch")
    return {"mount_id": int(fields[0]), "device": fields[2], "root": fields[3],
            "mountpoint": fields[4], "fs_type": fs[0]}


def validate_resource_limits(limits):
    require(set(limits) == {"memory.max", "pids.max", "cpu.max"}, "invalid_resource_limits")
    for name in ("memory.max", "pids.max"):
        require(re.fullmatch(r"[1-9][0-9]*", limits[name]) is not None, "unbounded_resource_limit")
    parts = limits["cpu.max"].split()
    require(len(parts) == 2 and all(re.fullmatch(r"[1-9][0-9]*", x) for x in parts),
            "unbounded_resource_limit")
    return {"memory_bytes": int(limits["memory.max"]), "pids": int(limits["pids.max"]),
            "cpu_quota": int(parts[0]), "cpu_period": int(parts[1])}


def expected_buildkit_config(prep):
    for value in (prep["storage"]["path"], prep["socket"]["path"], prep["binaries"]["bin/buildkit-runc"]):
        require(re.fullmatch(r"/[A-Za-z0-9_./-]+", value) is not None and ".." not in PurePosixPath(value).parts,
                "invalid_preparation_path")
    return ("debug = false\ntrace = false\nroot = " + json.dumps(prep["storage"]["path"]) +
            "\ninsecure-entitlements = []\n[grpc]\naddress = [" + json.dumps("unix://" + prep["socket"]["path"]) +
            "]\n[cdi]\ndisabled = true\n[worker.oci]\nenabled = true\nplatforms = [\"linux/amd64\"]\n"
            "snapshotter = \"native\"\nrootless = false\nnoProcessSandbox = false\ngc = false\n"
            "max-parallelism = 2\nnetworkMode = \"host\"\nbinary = " + json.dumps(prep["binaries"]["bin/buildkit-runc"]) +
            "\n[worker.containerd]\nenabled = false\n[frontend.\"dockerfile.v0\"]\nenabled = true\n"
            "[frontend.\"gateway.v0\"]\nenabled = false\n").encode()


def clean_environment(config_dir):
    # No inherited DOCKER_HOST, proxy, plugin, OTEL, BUILDKIT_SYNTAX, cache or
    # user home configuration. These are tool-generated dedicated directories.
    return {"PATH": "/usr/bin:/bin", "HOME": str(config_dir), "DOCKER_CONFIG": str(config_dir),
            "BUILDX_CONFIG": str(Path(config_dir) / "buildx"), "LANG": "C.UTF-8",
            "OTEL_SDK_DISABLED": "true", "BUILDX_NO_DEFAULT_ATTESTATIONS": "1"}


def run_bounded(argv, environment, cwd, timeout, max_log_bytes, evidence, observer=None):
    """Only the direct client is stopped on timeout; daemon outcome is unknown."""
    require(0 < timeout <= 7200 and 0 < max_log_bytes <= 64 * 1024**2, "invalid_process_budget")
    evidence = Path(evidence)
    write_exclusive(evidence / "command.json", canonical({"argv": argv, "timeout_seconds": timeout,
                                                        "max_log_bytes": max_log_bytes}))
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        return _collect_client(process, started, timeout, max_log_bytes, evidence, observer)
    finally:
        # Includes disk-full failure while recording the just-spawned PID. Never
        # abandon our direct child merely because evidence writing failed.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        reader = getattr(process, "_runtime_log_reader", None)
        if reader is None or not reader.is_alive():
            process.stdout.close()


def _collect_client(process, started, timeout, max_log_bytes, evidence, observer):
    write_exclusive(evidence / "client-start.json", canonical({"pid": process.pid, "started_ns": time.time_ns()}))
    overflow = threading.Event()
    captured, read_errors = bytearray(), []
    def drain():
        try:
            while True:
                data = process.stdout.read(16384)
                if not data: break
                room = max_log_bytes - len(captured)
                captured.extend(data[:max(room, 0)])
                if len(data) > room: overflow.set()
        except (OSError, ValueError) as exc:
            read_errors.append(type(exc).__name__)
    reader = threading.Thread(target=drain, name="runtime-build-client-log", daemon=True)
    process._runtime_log_reader = reader
    reader.start()
    reason = None
    while process.poll() is None:
        if observer is not None:
            try:
                observer()
            except (OSError, ContractError):
                reason = "builder_observation_failed"
                process.kill()
                break
        if overflow.is_set() or time.monotonic() - started >= timeout:
            reason = "client_log_budget" if overflow.is_set() else "client_timeout"
            process.kill()  # exact Popen child, NOT BuildKit or an execution cgroup
            break
        time.sleep(0.02)
    process.wait(timeout=5)
    reader.join(timeout=5)
    if reader.is_alive():
        reason = reason or "client_log_eof_unconfirmed"
    if overflow.is_set():
        reason = reason or "client_log_budget"
    if read_errors:
        reason = reason or "client_read_failed"
    write_exclusive(evidence / "client.log", bytes(captured))
    result = {"pid": process.pid, "exit_code": process.returncode, "seconds": time.monotonic() - started,
              "log_bytes": len(captured), "log_sha256": hash_bytes(captured), "error": reason,
              "reader_exited": not reader.is_alive(), "daemon_execution": "unknown" if reason else "not_inferred_from_client"}
    write_exclusive(evidence / "client-exit.json", canonical(result))
    require(not reason and not read_errors, reason or "client_read_failed")
    require(process.returncode == 0, "client_failed")
    return bytes(captured), result


def verify_preparation(preparation, approved_digest, release, artifacts):
    """Read-only validation of one separately prepared Linux build domain.

    The approved record is an explicit trust input from the preparation owner;
    caller-provided labels/booleans are never substituted for observed identity.
    This checks current containment configuration, not unperformed kernel ACs.
    """
    require(sys.platform == "linux", "build_requires_linux")
    path = Path(preparation).absolute()
    safe_path(path.parent.parent, path.parent.name, True)
    safe_path(path.parent, path.name)
    prep_stat = path.stat()
    require(prep_stat.st_uid in (0, os.getuid()) and stat.S_IMODE(prep_stat.st_mode) & 0o077 == 0
            and stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0, "preparation_permissions")
    raw = read_file(path, 1024**2)
    require(SHA.fullmatch(approved_digest or "") and hash_bytes(raw) == approved_digest,
            "preparation_approval_mismatch")
    prep = strict_json(raw)
    require(prep["schema"] == 1 and prep["driver"] == "remote" and prep["node_count"] == 1,
            "invalid_preparation")
    require(proc_identity(prep["process"]["pid"]) == prep["process"], "daemon_identity_changed")
    for name, role in (("socket", "socket"), ("storage", "directory"), ("config", "file"), ("cgroup", "directory")):
        entry = prep[name]
        require(Path(entry["path"]).is_absolute(), "invalid_preparation_path")
        if role != "socket": safe_path(Path(entry["path"]).parent, Path(entry["path"]).name, role == "directory")
        require(object_identity(entry["path"], role) == entry["identity"], "preparation_identity_changed")
    require(prep["socket"]["identity"]["mode"] & 0o077 == 0, "preparation_socket_permissions")
    config = read_file(Path(prep["config"]["path"]), 1024**2)
    require(hash_bytes(config) == prep["config"]["sha256"] and config == expected_buildkit_config(prep),
            "builder_config_changed")
    actual_argv = Path("/proc", str(prep["process"]["pid"]), "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    expected_argv = [prep["binaries"]["bin/buildkitd"], "--config", prep["config"]["path"]]
    require([x.decode() for x in actual_argv] == prep["argv"] == expected_argv, "daemon_command_changed")
    buildx = verify_file(artifacts, release["builder"]["buildx"])
    archive = verify_file(artifacts, release["builder"]["buildkit"])
    binaries = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member_name in ("bin/buildkitd", "bin/buildctl", "bin/buildkit-runc"):
            member = tar.getmember(member_name)
            require(member.isfile() and 0 < member.size <= 256 * 1024**2, "buildkit_archive_member")
            stream = tar.extractfile(member)
            digest, count = hashlib.sha256(), 0
            while True:
                chunk = stream.read(1024**2)
                if not chunk: break
                count += len(chunk)
                require(count <= member.size, "buildkit_archive_member")
                digest.update(chunk)
            binary = Path(prep["binaries"][member_name])
            verify_file(binary.parent, {"filename": binary.name, "size": count, "sha256": digest.hexdigest()})
            binaries[member_name] = str(binary)
    require(os.path.samefile(Path("/proc", str(prep["process"]["pid"]), "exe"), binaries["bin/buildkitd"]),
            "daemon_binary_changed")
    group = Path(prep["cgroup"]["path"])
    binding = cgroup_mount_binding(Path("/proc/self/mountinfo").read_text(), str(group), prep["cgroup"]["membership"])
    actual_device = group.stat().st_dev
    require(binding["device"] == "%d:%d" % (os.major(actual_device), os.minor(actual_device)),
            "cgroup_device_changed")
    require(binding == prep["cgroup"]["mount"] and
            Path("/proc/self/ns/cgroup").stat().st_ino == prep["cgroup"]["namespace_inode"], "cgroup_mount_changed")
    limits = {name: read_file(group / name, 4096).decode().strip() for name in ("memory.max", "pids.max", "cpu.max")}
    validate_resource_limits(limits)
    require(limits == prep["limits"], "builder_resource_limits_changed")
    membership = Path("/proc", str(prep["process"]["pid"]), "cgroup").read_text().splitlines()
    process_group = PurePosixPath(prep["process_cgroup"])
    resource_group = PurePosixPath(prep["cgroup"]["membership"])
    require(process_group.is_absolute() and ".." not in process_group.parts and
            resource_group in process_group.parents and "0::" + str(process_group) in membership,
            "daemon_cgroup_changed")
    # Kernel socket peer binds the endpoint to the same process checked above.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(prep["socket"]["path"])
        peer_pid, peer_uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    require(peer_pid == prep["process"]["pid"] and peer_uid == prep["socket"]["identity"]["uid"],
            "builder_socket_peer_changed")
    require(proc_identity(prep["process"]["pid"]) == prep["process"], "daemon_identity_changed")
    return prep, str(buildx), binaries["bin/buildctl"]


@contextmanager
def builder_lock(storage):
    import fcntl
    path = Path(storage) / ".runtime-build.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid()) and
                stat.S_IMODE(info.st_mode) & 0o077 == 0, "builder_lock_identity")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError("builder_busy") from None
        yield
    finally:
        os.close(descriptor)  # OS lock releases, persistent lock file retained


def directory_bytes(path, limit):
    total, entries = 0, 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            info = Path(directory, name).lstat()
            entries += 1
            require(entries <= 200000, "storage_entries_budget")
            total += info.st_size
            require(total <= limit, "storage_byte_budget")
    return total


def inspect_built_oci(path, release, system, python_lock, max_bytes, timeout):
    """Bounded offline OCI inspection; no engine load or image execution.

    Verify every compressed blob and read only two build-generated inventory
    files from layers. Dynamic ELF linkage still needs the separately recorded
    real container inspection; a successful import is not an all-model proof.
    """
    started = time.monotonic()
    require(path.stat().st_size <= max_bytes, "output_byte_budget")
    evidence = {}
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        require(len(members) <= 10000, "oci_members_budget")
        names = {m.name: m for m in members}
        require(len(names) == len(members), "duplicate_oci_member")
        def read_member(name, limit):
            require(name in names and names[name].isfile() and names[name].size <= limit, "oci_member_missing")
            with archive.extractfile(names[name]) as source:
                data = source.read(limit + 1)
            require(len(data) <= limit, "oci_member_budget")
            return data
        require(strict_json(read_member("oci-layout", 4096)) == {"imageLayoutVersion": "1.0.0"}, "oci_layout_version")
        index = strict_json(read_member("index.json", 1024**2))
        require(index.get("schemaVersion") == 2 and len(index["manifests"]) == 1, "output_manifest_count")
        descriptor = index["manifests"][0]
        def descriptor_name(row):
            digest = row.get("digest", "")
            require(digest.startswith("sha256:") and SHA.fullmatch(digest[7:]), "oci_digest")
            name = "blobs/sha256/" + digest[7:]
            require(name in names and names[name].isfile() and names[name].size == row["size"], "oci_size")
            return name
        def json_blob(row):
            data = read_member(descriptor_name(row), 1024**2)
            require(hash_bytes(data) == row["digest"][7:], "oci_hash")
            return strict_json(data)
        manifest = json_blob(descriptor)
        config = json_blob(manifest["config"])
        require(config.get("architecture") == "amd64" and config.get("os") == "linux", "oci_platform_mismatch")
        require(config["config"].get("User") == "1000:1000", "runtime_user_mismatch")
        require([{k: layer[k] for k in ("digest", "size")} for layer in manifest["layers"][:len(system["base"]["layers"])]] ==
                system["base"]["layers"], "parent_layers_not_shared")
        decompressed = 0
        for row in manifest["layers"]:
            name = descriptor_name(row)
            digest = hashlib.sha256()
            with archive.extractfile(names[name]) as stream:
                while True:
                    chunk = stream.read(1024**2)
                    if not chunk: break
                    digest.update(chunk)
                    require(time.monotonic() - started <= timeout, "output_inspection_timeout")
            require(digest.hexdigest() == row["digest"][7:], "oci_hash")
            require(row["mediaType"] in ("application/vnd.oci.image.layer.v1.tar+gzip", "application/vnd.docker.image.rootfs.diff.tar.gzip"),
                    "unsupported_layer_compression")
            with archive.extractfile(names[name]) as stream, gzip.GzipFile(fileobj=stream) as uncompressed:
                with tarfile.open(fileobj=uncompressed, mode="r|") as layer:
                    for member in layer:
                        decompressed += member.size
                        require(decompressed <= max_bytes * 4 and time.monotonic() - started <= timeout,
                                "layer_expansion_budget")
                        clean = member.name.removeprefix("./").lstrip("/")
                        require(".." not in PurePosixPath(clean).parts, "unsafe_layer_path")
                        if clean in ("opt/runtime-evidence/python.json", "opt/runtime-evidence/system.tsv"):
                            require(member.isfile() and member.size <= 1024**2, "inventory_member_invalid")
                            evidence[clean.rsplit("/", 1)[1]] = layer.extractfile(member).read(1024**2 + 1)
                        if "/.wh." in clean or clean.startswith(".wh."):
                            parent, _, name = clean.rpartition("/")
                            if name == ".wh..wh..opq" and parent in ("", "opt", "opt/runtime-evidence"):
                                evidence.clear()
                            elif parent == "opt/runtime-evidence" and name.startswith(".wh."):
                                evidence.pop(name[4:], None)
        require(set(evidence) == {"python.json", "system.tsv"}, "build_inventory_missing")
    python = strict_json(evidence["python.json"])
    require(python["imports"] == "passed" and python.get("uid") == 1000 and
            python["python"].split()[0] == "3.11.15", "build_import_failed")
    actual_python = sorted((normalized(n), v) for n, v in python["packages"])
    expected_python = sorted((normalized(p["name"]), p["version"]) for p in python_lock["packages"])
    require(actual_python == expected_python, "built_python_inventory_mismatch")
    actual_system = sorted(tuple(line.split("\t")) for line in evidence["system.tsv"].decode().splitlines())
    expected_system = sorted((p["name"], p["version"], p["architecture"]) for p in system["final_inventory"])
    require(actual_system == expected_system, "built_system_inventory_mismatch")
    return {"manifest": descriptor["digest"], "config": manifest["config"]["digest"],
            "layers": manifest["layers"], "python": python,
            "dependency_digest": hash_bytes(canonical([actual_python, actual_system])),
            "offline_build_import": "passed", "dynamic_elf_inspection": "not_executed",
            "parent_layers_shared": True, "output_bytes": path.stat().st_size}


def build_runtime(source, artifacts, sdk_source, parent_oci, preparation, release_sha256,
                  preparation_sha256, evidence, mode, max_bytes, timeout, max_log_bytes):
    """One explicitly requested build using a pre-existing private remote daemon.

    No daemon lifecycle, download, image load or cleanup action is implemented.
    All evidence is retained. A client error is never an execution-domain exit.
    """
    started = time.monotonic()
    require(sys.platform == "linux", "build_requires_linux")
    require(release_sha256 and preparation_sha256, "independent_approval_required")
    require(mode in ("cold", "warm") and type(max_bytes) is int and 0 < max_bytes <= 64 * 1024**3,
            "invalid_build_budget")
    require(0 < timeout <= 7200 and 0 < max_log_bytes <= 64 * 1024**2, "invalid_process_budget")
    report = verify_contract(source, artifacts, sdk_source, parent_oci, release_sha256)
    release_raw = read_file(safe_path(source, "containers/runtime-v1/release.json"), 2 * 1024**2)
    require(hash_bytes(release_raw) == release_sha256, "input_changed")
    release = strict_json(release_raw)
    locks = {}
    for name in ("system.lock", "python.lock"):
        raw = read_file(safe_path(source, "containers/runtime-v1/" + name), 4 * 1024**2)
        require(hash_bytes(raw) == release["inputs"][name], "input_changed")
        locks[name] = strict_json(raw)
    prep, buildx, buildctl = verify_preparation(preparation, preparation_sha256, release, artifacts)
    with builder_lock(prep["storage"]["path"]):
        current, _, _ = verify_preparation(preparation, preparation_sha256, release, artifacts)
        require(current == prep, "preparation_changed")
        active = Path(prep["storage"]["path"]) / ".runtime-build-active.json"
        require(not active.exists(), "previous_build_exit_unconfirmed")
        dest = private_output(evidence)
        require(time.monotonic() - started < timeout, "build_timeout")
        write_exclusive(dest / "build-intent.json", canonical({"release_sha256": release_sha256,
                        "preparation_sha256": preparation_sha256, "mode": mode, "phase": "preflight",
                        "max_bytes": max_bytes, "timeout": timeout, "created_ns": time.time_ns()}))
        endpoint = "unix://" + prep["socket"]["path"]
        config = private_output(dest / "client-config")
        write_exclusive(config / "config.json", b"{}")
        env = clean_environment(config)
        ordinal = 0
        observations = []
        def observe():
            require(proc_identity(prep["process"]["pid"]) == prep["process"], "daemon_identity_changed")
            require(object_identity(prep["socket"]["path"], "socket") == prep["socket"]["identity"], "builder_socket_changed")
            require(object_identity(prep["storage"]["path"], "directory") == prep["storage"]["identity"], "builder_storage_changed")
            group = Path(prep["cgroup"]["path"])
            require({name: read_file(group / name, 4096).decode().strip() for name in prep["limits"]} == prep["limits"],
                    "builder_resource_limits_changed")
            size = directory_bytes(prep["storage"]["path"], max_bytes) + directory_bytes(dest, max_bytes)
            require(size <= max_bytes, "storage_byte_budget")
            observations.append({"elapsed": time.monotonic() - started, "logical_bytes": size})
            require(len(observations) <= 400000, "observation_limit")
        def run(argv, build=False):
            nonlocal ordinal
            ordinal += 1
            phase = private_output(dest / ("phase-%02d" % ordinal))
            # Identity is checked before every command, not just at admission.
            current, _, _ = verify_preparation(preparation, preparation_sha256, release, artifacts)
            require(current == prep, "preparation_changed")
            remaining = timeout - (time.monotonic() - started)
            require(remaining > 0, "build_timeout")
            return run_bounded(argv, env, config, remaining, max_log_bytes, phase, observe if build else None)[0]
        try:
            version_text = run([buildx, "version"]).decode("utf-8")
            require(re.search(r"\bv0\.31\.1\b", version_text) is not None, "buildx_version_changed")
            info = strict_json(run([buildctl, "--addr", endpoint, "debug", "info", "--format", "{{json .}}"] ))
            require(info["buildkitVersion"]["version"] == "v0.27.0", "buildkit_version_changed")
            workers = strict_json(run([buildctl, "--addr", endpoint, "debug", "workers", "--format", "{{json .}}"] ))
            require(len(workers) == 1 and workers[0]["id"] == prep["worker_id"] and
                    workers[0]["buildkitVersion"]["version"] == "v0.27.0" and
                    not workers[0].get("gcPolicy") and
                    any(p["os"] == "linux" and p["architecture"] == "amd64" for p in workers[0]["platforms"]),
                    "builder_worker_changed")
            builder = "mc-runtime-" + release_sha256[:16]
            run([buildx, "create", "--name", builder, "--driver", "remote", endpoint])
            inspect = run([buildx, "inspect", builder]).decode("utf-8")
            require(re.search(r"(?m)^Driver:\s+remote\s*$", inspect) and endpoint in inspect,
                    "builder_driver_changed")
            context = dest / "context"
            stage_context(source, artifacts, sdk_source, parent_oci, context, max_bytes, release_sha256)
            observe()
            output = dest / "runtime.oci.tar"
            plan = fixed_build_argv(buildx, builder, endpoint, str(context), str(Path(parent_oci).absolute()), str(output), mode)
            cgroup_parent = prep["cgroup"]["membership"]
            require(re.fullmatch(r"/[A-Za-z0-9_/-]+", cgroup_parent) and cgroup_parent != "/",
                    "invalid_execution_cgroup")
            plan["argv"][-1:-1] = ["--cgroup-parent", cgroup_parent]
            # Persistent same-storage fence BEFORE X/build submission. A crash,
            # timeout or unknown outcome leaves it in place; a copied prep path
            # cannot authorize another build against this still-active daemon.
            lease = {"process": prep["process"], "release_sha256": release_sha256,
                     "preparation_sha256": preparation_sha256, "evidence": str(dest),
                     "created_ns": time.time_ns(), "state": "submitted_or_unknown"}
            lease_raw = canonical(lease)
            write_exclusive(active, lease_raw)
            run(plan["argv"], build=True)
            current, _, _ = verify_preparation(preparation, preparation_sha256, release, artifacts)
            require(current == prep, "preparation_changed")
            observe()
            remaining = timeout - (time.monotonic() - started)
            require(remaining > 0, "build_timeout")
            actual = inspect_built_oci(output, release, locks["system.lock"], locks["python.lock"], max_bytes, remaining)
            require(time.monotonic() - started <= timeout, "build_timeout")
            result = {"schema": 1, "mode": mode, "release_sha256": release_sha256,
                      "preparation_sha256": preparation_sha256, "build": "completed", "actual": actual,
                      "daemon_execution": "build_result_returned_no_daemon_exit_claim",
                      "storage_samples": observations, "disk_peak_observed_bytes": max(x["logical_bytes"] for x in observations),
                      "disk_peak_measurement": "sampled logical file bytes; not kernel quota or exact physical peak",
                      "network_bytes": None, "network_measurement": "not_measured",
                      "downloads": "no downloader implemented; daemon network counters not observed",
                      "all_model_compatibility": "not_tested", "seconds": time.monotonic() - started}
            write_exclusive(dest / "build-result.json", canonical(result))
            require(read_file(active, 1024**2) == lease_raw, "active_build_identity_changed")
            # No delete: completed intent is retained under its unique identity.
            archive = active.with_name(".runtime-build-completed-" + hash_bytes(lease_raw) + ".json")
            require(not archive.exists(), "completed_build_record_exists")
            active.rename(archive)
            sync_directory(archive.parent)
            return result
        except BaseException as exc:
            code = exc.code if isinstance(exc, ContractError) else type(exc).__name__
            write_exclusive(dest / "build-failure.json", canonical({"error": code,
                            "daemon_execution": "unknown", "resources": "retained_no_release_claim",
                            "release_sha256": release_sha256, "preparation_sha256": preparation_sha256}))
            raise


def stage_context(source, artifacts, sdk_source, parent_oci, destination, max_bytes, approved_release_sha256):
    """Create only a whitelisted, individually rehashed build context.

    Verifying the SDK in the source tree does not authorize a later changed
    file: bytes are hashed again while copying, and failure preserves output.
    """
    require(type(max_bytes) is int and 0 < max_bytes <= 64 * 1024**3, "invalid_byte_budget")
    require(approved_release_sha256 is not None, "independent_release_approval_required")
    report = verify_contract(source, artifacts, sdk_source, parent_oci, approved_release_sha256)
    source = Path(source)
    release_bytes = read_file(safe_path(source, "containers/runtime-v1/release.json"), 2 * 1024**2)
    release = strict_json(release_bytes)
    # Bind the reread to the same release validated above, not a fresh baseline.
    require(hash_bytes(release_bytes) == report["release_sha256"], "input_changed")
    locks = {}
    for name in ("python.lock", "system.lock"):
        raw = read_file(safe_path(source, "containers/runtime-v1/" + name), 4 * 1024**2)
        require(hash_bytes(raw) == release["inputs"][name], "input_changed")
        locks[name] = strict_json(raw)
    copies = [(Path(artifacts), row, "wheels/" + row["filename"]) for row in locks["python.lock"]["packages"]]
    copies.append((Path(artifacts), release["python_source"], release["python_source"]["filename"]))
    copies.extend((Path(sdk_source), dict(row, filename=row["path"]), "sdk/" + row["path"]) for row in release["sdk"])
    system = locks["system.lock"]
    for stage_name in ("build", "runtime"):
        for package in system["packages"]:
            if package["Package"] in system["stages"][stage_name]["added"]:
                entry = {"filename": package["Filename"].rsplit("/", 1)[-1], "size": int(package["Size"]), "sha256": package["SHA256"]}
                copies.append((Path(artifacts), entry, stage_name + "-debs/" + entry["filename"]))
    dockerfile = read_file(safe_path(source, "containers/runtime-v1/Dockerfile"), 1024**2)
    require(hash_bytes(dockerfile) == release["inputs"]["Dockerfile"], "input_changed")
    requirements = "\n".join(sorted(normalized(p["name"]) + "==" + p["version"] + " --hash=sha256:" + p["sha256"]
                                      for p in locks["python.lock"]["packages"])) + "\n"
    declared = sum(row["size"] for _, row, _ in copies) + len(dockerfile) + len(requirements.encode())
    require(declared <= max_bytes, "context_byte_budget")
    dest = private_output(destination)
    write_exclusive(dest / "context-intent.json", canonical({"release_sha256": report["release_sha256"],
                    "declared_payload_bytes": declared, "status": "copying", "created_ns": time.time_ns()}))
    copied = []
    for root, row, relative in copies:
        original = safe_path(root, row["filename"])
        output = dest.joinpath(*PurePosixPath(relative).parts)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        count, digest = 0, hashlib.sha256()
        with original.open("rb") as inp, output.open("xb") as out:
            if os.name == "posix": os.fchmod(out.fileno(), 0o600)
            while True:
                data = inp.read(min(1024**2, row["size"] - count + 1))
                if not data: break
                count += len(data)
                require(count <= row["size"], "input_changed")
                digest.update(data)
                out.write(data)
            out.flush()
            os.fsync(out.fileno())
        require(count == row["size"] and digest.hexdigest() == row["sha256"], "input_changed")
        copied.append({"filename": relative, "size": count, "sha256": digest.hexdigest()})
    for name, data in (("Dockerfile", dockerfile), ("requirements.txt", requirements.encode())):
        write_exclusive(dest / name, data)
        copied.append({"filename": name, "size": len(data), "sha256": hash_bytes(data)})
    receipt = {"release_sha256": report["release_sha256"], "status": "staged_not_built",
               "payload_bytes": declared, "files": copied, "final_image": None}
    write_exclusive(dest / "context-receipt.json", canonical(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--sdk-source", type=Path)
    parser.add_argument("--parent-oci", type=Path)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--release-sha256", help="Independent approved release digest, never read from release.json itself")
    parser.add_argument("--preparation", type=Path)
    parser.add_argument("--preparation-sha256")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--cache-mode", choices=("cold", "warm"))
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-log-bytes", type=int)
    args = parser.parse_args(argv)
    try:
        report = verify_contract(args.source, args.artifacts, args.sdk_source, args.parent_oci, args.release_sha256)
        if args.build:
            require(not args.verify_only and all((args.artifacts, args.sdk_source, args.parent_oci,
                    args.preparation, args.preparation_sha256, args.evidence, args.cache_mode,
                    args.release_sha256, args.max_bytes, args.timeout, args.max_log_bytes)), "explicit_build_inputs_required")
            report = build_runtime(args.source, args.artifacts, args.sdk_source, args.parent_oci,
                    args.preparation, args.release_sha256, args.preparation_sha256, args.evidence,
                    args.cache_mode, args.max_bytes, args.timeout, args.max_log_bytes)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        require(args.verify_only, "explicit_mode_required")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (ContractError, KeyError, TypeError, OSError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "invalid_contract"
        print(json.dumps({"error": code, "build": "unknown" if args.build else "not_executed",
                          "daemon_execution": "unknown" if args.build else "not_observed", "final_image": None}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
