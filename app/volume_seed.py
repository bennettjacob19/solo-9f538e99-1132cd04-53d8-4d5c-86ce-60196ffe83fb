"""Volume seeding output for ``POST /volume-seed``.

Given an OCI Image Layout tar (same verification and layer stacking as
``/unpack``), read ``config.Volumes`` from the verified image config and
produce a single tar containing:

* ``rootfs.tar`` -- the final root filesystem;
* one seed tar per declared volume (``<name>.tar``);
* ``mounts.json`` -- the mount list, parent volumes before child volumes.

Semantics
---------

* Every declared volume must be an absolute, non-root directory in the
  *final* tree, and every component of its path -- including the last --
  must be an actual directory (a symlink on the way is rejected, never
  followed).
* A volume seed tar holds the volume's contents with paths relative to the
  volume root, exactly as they must appear after mounting.  Intermediate
  directories of nested volumes are created empty (never populated with
  unrelated rootfs content).
* ``rootfs.tar`` carries the image's own data for empty volumes, but at
  each *next-level* mount point (a volume whose parent chain contains
  another volume) only an empty directory is kept, so the mounted volume
  is never shadowed by image content.
* Every tar can be extracted independently and preserves hardlinks
  *inside* that tar.  When one hardlink group spans several packages, each
  package holding group members stores its own copy of the shared bytes,
  and the link relationship is preserved within the package.
* Symlink targets are copied verbatim.

Only one rootfs tree is staged: packages are produced by streaming from
it, in chunks.
"""

from __future__ import annotations

import json
import os
import posixpath
import stat
import tarfile

from .unpacker import (
    UnpackError,
    build_rootfs_tar,
    build_subtree_tar,
    prepare_rootfs,
)

MOUNTS_NAME = "mounts.json"
ROOTFS_NAME = "rootfs.tar"


# ---------------------------------------------------------------------------
# volume discovery / validation
# ---------------------------------------------------------------------------


def config_volumes(config):
    """Return the declared volume paths from ``config.Volumes``.

    Paths are validated here and returned exactly as declared; duplicates
    (after normalization) keep their first declaration.
    """
    if not isinstance(config, dict):
        raise UnpackError("config is not a JSON object")
    raw = config.get("Volumes")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise UnpackError("config: Volumes is not an object")
    seen = {}
    for value in raw:
        if not isinstance(value, str):
            raise UnpackError(f"config: invalid volume path {value!r}")
        parts = _volume_parts(value)
        seen.setdefault("/".join(parts), value)
    return list(seen.values())


def _volume_parts(value):
    if "\x00" in value:
        raise UnpackError(f"invalid volume path {value!r}")
    if not value.startswith("/"):
        raise UnpackError(f"volume path must be absolute: {value!r}")
    parts = []
    for p in value.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise UnpackError(f"volume path must not contain '..': {value!r}")
        parts.append(p)
    if not parts:
        raise UnpackError("volume path must not be the root directory: '/'")
    return parts


def _real_dir_in_final_tree(rootfs, parts, original):
    """Return *parts* if it is a chain of real directories in the final tree.

    Every component, including the last, must be an actual directory: a
    symlink anywhere along the way is rejected, never followed.
    """
    current = rootfs
    for comp in parts:
        current = os.path.join(current, comp)
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise UnpackError(
                f"volume {original!r} is not a directory in the final rootfs"
            )
        except OSError as e:
            raise UnpackError(f"cannot inspect volume {original!r}: {e}")
        if not stat.S_ISDIR(st.st_mode):
            raise UnpackError(
                f"volume {original!r} is not a directory in the final rootfs"
            )
    return "/".join(parts)


def resolve_volumes(rootfs, config):
    """Validate declared volumes against the final tree.

    Returns ``[(declared_path, resolved_rel), ...]`` in config order, with
    duplicates removed.
    """
    result = []
    seen = set()
    for declared in config_volumes(config):
        resolved = _real_dir_in_final_tree(rootfs, _volume_parts(declared), declared)
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append((declared, resolved))
    return result


# ---------------------------------------------------------------------------
# mount ordering
# ---------------------------------------------------------------------------


def _parent_of(rel):
    parent = posixpath.dirname(rel)
    return parent or None


def mounts_in_order(resolved):
    """Order volumes parents-first.

    A volume sorts after its nearest enclosing volume; unrelated volumes
    keep their declaration order.
    """
    mount_set = set(resolved)

    def depth(rel):
        """Number of enclosing declared volumes."""
        n = 0
        p = _parent_of(rel)
        while p is not None:
            if p in mount_set:
                n += 1
            p = _parent_of(p)
        return n

    indexed = list(enumerate(resolved))
    indexed.sort(key=lambda pair: (depth(pair[1]), pair[0]))
    return [rel for _i, rel in indexed]


def _has_ancestor(rel, candidates):
    p = _parent_of(rel)
    while p is not None:
        if p in candidates:
            return True
        p = _parent_of(p)
    return False


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------


def _seed_name(rel):
    return rel.replace("/", "_") + ".tar"


def build_volume_seed(upload_path, workdir, out_path):
    """Verify *upload_path*, build the seed bundle at *out_path*."""
    rootfs, config = prepare_rootfs(upload_path, workdir)

    resolved_pairs = resolve_volumes(rootfs, config)
    resolved = [r for _declared, r in resolved_pairs]
    ordered = mounts_in_order(resolved)
    mount_set = set(ordered)

    staging = os.path.join(workdir, "packages")
    os.makedirs(staging)

    rootfs_tar = os.path.join(staging, ROOTFS_NAME)
    build_rootfs_tar(rootfs, rootfs_tar, exclude=mount_set)

    packages = []
    for rel in ordered:
        name = _seed_name(rel)
        # A nested volume is mounted on top of its parent, so the parent's
        # seed must not ship content that would shadow it.
        nested = {m for m in mount_set if _has_ancestor(m, {rel})}
        path = os.path.join(staging, name)
        build_subtree_tar(rootfs, rel, path, exclude=nested)
        packages.append((rel, name))

    declared_by_resolved = {r: d for d, r in resolved_pairs}
    mounts = [
        {"path": declared_by_resolved[rel], "file": name} for rel, name in packages
    ]

    # Assemble the single downloadable artifact.
    tmp = out_path + ".tmp"
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tf:
        _add_package(tf, rootfs_tar, ROOTFS_NAME)
        for _rel, name in packages:
            _add_package(tf, os.path.join(staging, name), name)
        raw = json.dumps({"mounts": mounts}, indent=2).encode() + b"\n"
        ti = tarfile.TarInfo(MOUNTS_NAME)
        ti.size = len(raw)
        ti.mtime = int(os.path.getmtime(rootfs_tar))
        tf.addfile(ti, _BytesReader(raw))
    os.replace(tmp, out_path)
    return out_path


def _add_package(tf, path, arcname):
    st = os.stat(path)
    ti = tarfile.TarInfo(arcname)
    ti.size = st.st_size
    ti.mtime = int(st.st_mtime)
    ti.mode = stat.S_IMODE(st.st_mode)
    with open(path, "rb") as f:
        tf.addfile(ti, f)


class _BytesReader:
    """Minimal file-like wrapper so small in-memory members can be streamed."""

    def __init__(self, data):
        self._data = data
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk
