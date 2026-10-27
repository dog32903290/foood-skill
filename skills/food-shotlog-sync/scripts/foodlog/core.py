"""Configuration, immutable artifacts, session locks, and typed JSON failures."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.parse
import uuid

PROTOCOL = "foodlog/v1"


class HarnessError(Exception):
    def __init__(self, kind, message, hint="", **context):
        super().__init__(message)
        self.kind, self.hint, self.context = kind, hint, context


def require(condition, kind, message, hint="", **context):
    if not condition:
        raise HarnessError(kind, message, hint, **context)


def read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value, *, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if replace:
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    else:
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError:
            raise HarnessError("output_exists", "Refusing to overwrite an output", "Use a new output path.", path=str(path))
    return str(path)


def digest(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]


def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def run(arguments, *, timeout=180, text=True):
    try:
        result = subprocess.run([str(a) for a in arguments], capture_output=True, text=text, timeout=timeout,
                                encoding="utf-8" if text else None, errors="replace" if text else None)
    except FileNotFoundError as exc:
        raise HarnessError("backend_missing", str(exc), "Install the named executable or set its configured path.")
    except subprocess.TimeoutExpired:
        raise HarnessError("backend_timeout", "Backend exceeded its time limit", "Inspect the input and retry this stage with an appropriate timeout.", executable=str(arguments[0]))
    if result.returncode:
        detail = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise HarnessError("backend_failed", "Backend returned a nonzero exit code", "Inspect stderr and fix the input or backend; no successful result was recorded.", executable=str(arguments[0]), returncode=result.returncode, stderr=detail[-2500:])
    return result.stdout


def dependency(module, extra):
    if importlib.util.find_spec(module) is None:
        raise HarnessError("dependency_missing", f"Python module {module} is unavailable", f"Install the {extra} optional dependencies from this repository.")


def local_endpoint(url):
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme in ("http", "https") and parsed.hostname in ("localhost", "127.0.0.1", "::1") and not parsed.username,
            "endpoint_not_local", "Model endpoints must be on localhost and contain no credentials", "Use a local OpenAI-compatible model server. Codex native vision runs in the host, not through this endpoint.")
    return url.rstrip("/")


class Context:
    def __init__(self, config):
        self.config_path = Path(config).expanduser().resolve()
        self.config = read_json(self.config_path)
        require(self.config.get("schema_version") == 1, "schema_mismatch", "Unsupported configuration schema")
        base = self.config_path.parent
        self.source = (base / Path(self.config["source_root"]).expanduser()).resolve()
        self.work = (base / Path(self.config["work_dir"]).expanduser()).resolve()
        require(self.source.is_dir(), "source_missing", "Source root is not mounted or does not exist", path=str(self.source))
        self.cards = {}
        for name, value in self.config["cards"].items():
            require(name and "/" not in name and "\\" not in name, "invalid_card", "Card IDs cannot contain path separators")
            root = (self.source / Path(value["path"]).expanduser()).resolve()
            require(root.is_dir() and inside(root, self.source), "invalid_card", "Card path is missing or outside the source root", card=name)
            require(not inside(self.work, root), "unsafe_output", "Work directory must be outside every source card", card=name)
            require(value["camera"] in ("A", "B"), "invalid_camera", "Camera must be A or B", card=name)
            self.cards[name] = {**value, "root": root}
        self.session_path = self.work / "session.json"
        if self.session_path.exists():
            session = read_json(self.session_path)
            require(session.get("protocol") == PROTOCOL, "session_conflict", "This work directory belongs to a different tool")
            require(session.get("source_root") == str(self.source), "session_conflict", "This session belongs to another source root", "Use a new work directory when remapping the source root.")

    @property
    def ffmpeg(self):
        return self.config.get("ffmpeg", "ffmpeg")

    @property
    def ffprobe(self):
        return self.config.get("ffprobe", "ffprobe")

    def head(self):
        return read_json(self.session_path) if self.session_path.exists() else {"protocol": PROTOCOL, "source_root": str(self.source), "stages": {}}

    def stage(self, name, *, required=True):
        path = self.head()["stages"].get(name)
        if path:
            require(inside(path, self.work), "unsafe_artifact", "Stage path escapes this work directory")
            return read_json(path)
        if required:
            raise HarnessError("stage_missing", f"Run the {name} stage first")
        return None

    def publish(self, name, data):
        bundle = self.work / "bundles" / stamp()
        path = bundle / f"{name}.json"
        payload = {"protocol": PROTOCOL, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), **data}
        write_json(path, payload)
        write_json(bundle / "manifest.json", {"protocol": PROTOCOL, "stage": name, "config_fingerprint": digest(self.config), "artifacts": [{"path": str(path), "sha256": file_hash(path)}]})
        head = self.head()
        head["stages"][name] = str(path)
        head["current_bundle"] = str(bundle)
        write_json(self.session_path, head, replace=True)
        with (self.work / "trajectory.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"stage": name, "bundle": str(bundle), "artifact": str(path)}, ensure_ascii=False) + "\n")
        return {"stage": name, "artifact": str(path), "bundle": str(bundle)}

    @contextlib.contextmanager
    def lock(self):
        self.work.mkdir(parents=True, exist_ok=True)
        with (self.work / ".session.lock").open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0"); handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise HarnessError("session_busy", "Another command holds this session lock")
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise HarnessError("session_busy", "Another command holds this session lock")
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def source_unchanged(row):
    path = Path(row["path"])
    require(path.is_file(), "source_missing", "Source file is missing", path=str(path))
    current = path.stat()
    require(current.st_size == row["bytes"] and current.st_mtime_ns == row["mtime_ns"],
            "source_changed", "Source changed after inventory", "Re-inventory into a new session before continuing.", clip_id=row["id"])

    meta = row.get("metadata", {})
    if meta.get("sidecar"):
        sidecar = Path(meta["sidecar"])
        require(sidecar.is_file(), "source_changed", "Camera sidecar is missing", clip_id=row["id"])
        st = sidecar.stat()
        require(st.st_size == meta["sidecar_bytes"] and st.st_mtime_ns == meta["sidecar_mtime_ns"],
                "source_changed", "Camera metadata changed after inventory", clip_id=row["id"])


def status(ctx=None):
    modules = ["numpy", "openpyxl", "PIL", "faster_whisper", "mlx_whisper", "transformers", "torch"]
    result = {"python": sys.version.split()[0], "ffmpeg": shutil.which(ctx.ffmpeg if ctx else "ffmpeg"),
              "ffprobe": shutil.which(ctx.ffprobe if ctx else "ffprobe"),
              "modules": {name: importlib.util.find_spec(name) is not None for name in modules}}
    if ctx:
        result.update(source_root=str(ctx.source), work_dir=str(ctx.work), cards=list(ctx.cards), session=ctx.head())
    return result
