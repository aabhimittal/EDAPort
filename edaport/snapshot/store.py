"""Content-addressed design snapshots.

A snapshot is a manifest {"files": {relpath: blob_digest}, "meta": {...}} stored as a blob;
its digest is the design_ref. Identical content always yields the identical ref, so a
replayed run that reproduces the same outputs reproduces the same design_ref.
"""

import json
import os
import re
import shutil
from pathlib import Path, PurePosixPath

from ..util import canonical, sha256

_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
# Paths end up inside tool scripts (Yosys .ys, make vars); a strict charset rules out command injection.
_PATH_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+@-]*$")  # no leading "-"/"+": would read as a tool flag
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
# Fixed mtime for materialized files so make-based flows (ORFS) treat restored outputs as up to date.
MATERIALIZE_MTIME = 1_000_000_000
SKIP_DIRS = {".git", "__pycache__", "obj_dir", ".edaport"}


class StoreError(ValueError):
    pass


def check_relpath(path: str) -> str:
    """Trust boundary: snapshot paths come from agents. Reject anything that escapes the tree."""
    p = PurePosixPath(path)
    if not path or p.is_absolute() or str(p) in (".", "") or not all(_PATH_PART.match(x) for x in p.parts):
        raise StoreError(f"invalid snapshot path: {path!r} (allowed: [A-Za-z0-9_.+@-], must start with [A-Za-z0-9_])")
    return str(p)


class Store:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.branches = self.root / "refs" / "branches"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.branches.mkdir(parents=True, exist_ok=True)

    # ---- blobs ----
    def _blob_path(self, digest: str) -> Path:
        if not _REF.match(digest):
            raise StoreError(f"not a sha256 digest: {digest!r}")
        h = digest[7:]
        return self.objects / h[:2] / h[2:]

    def put_blob(self, data: bytes) -> str:
        digest = sha256(data)
        path = self._blob_path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return digest

    def get_blob(self, digest: str) -> bytes:
        path = self._blob_path(digest)
        if not path.exists():
            raise StoreError(f"unknown object {digest}")
        return path.read_bytes()

    def has(self, digest: str) -> bool:
        return self._blob_path(digest).exists()

    # ---- manifests / design refs ----
    def put_manifest(self, files: dict[str, str], meta: dict | None = None) -> str:
        files = {check_relpath(k): v for k, v in files.items()}
        return self.put_blob(canonical({"files": dict(sorted(files.items())), "meta": meta or {}}))

    def manifest(self, ref: str) -> dict:
        m = json.loads(self.get_blob(self.resolve(ref)))
        if not isinstance(m, dict) or not isinstance(m.get("files"), dict):  # tool results also have "files"
            raise StoreError(f"{ref} is not a design snapshot")
        # A manifest can come from outside (S3 pull, a tampered store): never trust its paths or digests.
        for path, digest in m["files"].items():
            if check_relpath(path) != path or not isinstance(digest, str) or not _REF.match(digest):
                raise StoreError(f"{ref}: unsafe manifest entry {path!r}")
        return m

    def resolve(self, ref_or_label: str) -> str:
        """Accept a design_ref or a branch label."""
        if _REF.match(ref_or_label):
            return ref_or_label
        if _LABEL.match(ref_or_label) and ".." not in ref_or_label:
            path = self.branches / ref_or_label
            if path.is_file():
                return path.read_text().strip()
        raise StoreError(f"unknown design_ref or branch: {ref_or_label!r}")

    def import_dir(self, src: str | os.PathLike, meta: dict | None = None) -> str:
        src = Path(src)
        files = {}
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue  # hidden files (.env, credentials) never enter a snapshot
                full = Path(dirpath) / name
                if full.is_symlink() or not full.is_file():
                    continue
                files[full.relative_to(src).as_posix()] = self.put_blob(full.read_bytes())
        return self.put_manifest(files, meta)

    def read_file(self, ref: str, path: str) -> bytes:
        files = self.manifest(ref)["files"]
        path = check_relpath(path)
        if path not in files:
            raise StoreError(f"{path} not in snapshot")
        return self.get_blob(files[path])

    def write_file(self, ref: str, path: str, data: bytes | None) -> str:
        """New snapshot with `path` replaced (or deleted when data is None)."""
        m = self.manifest(ref)
        files = dict(m["files"])
        path = check_relpath(path)
        if data is None:
            files.pop(path, None)
        else:
            files[path] = self.put_blob(data)
        return self.put_manifest(files, m["meta"])

    def materialize(self, ref: str, dest: str | os.PathLike) -> Path:
        dest = Path(dest)
        for rel, digest in self.manifest(ref)["files"].items():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self._blob_path(digest), out)
            os.utime(out, (MATERIALIZE_MTIME, MATERIALIZE_MTIME))
        return dest

    def capture(self, base_ref: str, workdir: str | os.PathLike, prefixes: list[str], meta_update: dict) -> str:
        """New snapshot = base + every file under `prefixes` in workdir (write scope).

        Files under a prefix that disappeared from workdir are dropped, so the snapshot mirrors the tool's outputs.
        """
        workdir = Path(workdir)
        m = self.manifest(base_ref)
        prefixes = [check_relpath(p).rstrip("/") + "/" for p in prefixes]
        files = {k: v for k, v in m["files"].items() if not any(k.startswith(p) for p in prefixes)}
        for p in prefixes:
            root = workdir / p
            if not root.is_dir():
                continue
            for full in sorted(root.rglob("*")):
                if full.is_file() and not full.is_symlink():
                    files[full.relative_to(workdir).as_posix()] = self.put_blob(full.read_bytes())
        meta = dict(m["meta"])
        for k, v in meta_update.items():
            meta[k] = {**meta.get(k, {}), **v} if isinstance(v, dict) else v
        return self.put_manifest(files, meta)

    # ---- branches ----
    def branch(self, ref: str, label: str) -> str | None:
        """Point `label` at ref; returns the previous head (None if new). Restore == branch to an old ref."""
        if not _LABEL.match(label) or ".." in label:
            raise StoreError(f"invalid branch label: {label!r}")
        ref = self.resolve(ref)
        self.manifest(ref)  # must exist
        path = self.branches / label
        prev = path.read_text().strip() if path.is_file() else None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ref + "\n")
        except OSError:  # "a" and "a/b" cannot both be branches (same rule as git refs)
            raise StoreError(f"branch label {label!r} conflicts with an existing branch name")
        return prev

    def list_branches(self) -> dict[str, str]:
        return {p.relative_to(self.branches).as_posix(): p.read_text().strip()
                for p in sorted(self.branches.rglob("*")) if p.is_file()}

    # ---- diff / gc ----
    def diff(self, a: str, b: str) -> dict:
        ma, mb = self.manifest(a), self.manifest(b)
        fa, fb = ma["files"], mb["files"]
        files_changed = {
            "added": sorted(set(fb) - set(fa)),
            "removed": sorted(set(fa) - set(fb)),
            "modified": sorted(k for k in set(fa) & set(fb) if fa[k] != fb[k]),
        }
        xa, xb = _flat(ma["meta"].get("metrics", {})), _flat(mb["meta"].get("metrics", {}))
        deltas = {}
        for k in sorted(set(xa) | set(xb)):
            va, vb = xa.get(k), xb.get(k)
            if va == vb:
                continue
            num = isinstance(va, (int, float)) and isinstance(vb, (int, float))
            deltas[k] = {"a": va, "b": vb, "delta": (vb - va) if num else None}
        return {"files_changed": files_changed, "metric_deltas": deltas}

    def gc(self, keep: set[str]) -> int:
        """Delete objects unreachable from branches + `keep` (e.g. refs in the replay log). Returns count."""
        live = set()
        for ref in set(self.list_branches().values()) | {k for k in keep if k and _REF.match(k)}:
            if not self.has(ref):
                continue
            live.add(ref)
            try:
                m = self.manifest(ref)
            except (StoreError, ValueError):
                continue  # non-manifest blob (raw log / result): keep the blob itself only
            live.update(m["files"].values())
        removed = 0
        for path in self.objects.glob("*/*"):
            if "sha256:" + path.parent.name + path.name not in live:
                path.unlink()
                removed += 1
        return removed


def _flat(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out
