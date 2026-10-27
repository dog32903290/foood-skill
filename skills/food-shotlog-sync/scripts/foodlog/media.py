"""Read-only source inventory and independently measured audio preparation."""
from __future__ import annotations

import datetime as dt
from fractions import Fraction
import json
import os
import uuid
from pathlib import Path
import wave
import xml.etree.ElementTree as ET

from .core import digest, require, run, source_unchanged, write_json

EXTENSIONS = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".mkv"}


def rate(value):
    if not value or value in ("0/0", "N/A"):
        return None
    cleaned = str(value).rstrip("pi")
    return {"23.98": 24000 / 1001, "23.976": 24000 / 1001, "29.97": 30000 / 1001,
            "59.94": 60000 / 1001, "119.88": 120000 / 1001}.get(cleaned, float(Fraction(cleaned)))


def sony_xml(path):
    sidecar = path.with_name(path.stem + "M01.XML")
    if not sidecar.exists():
        return {}
    root = ET.parse(sidecar).getroot()
    find = lambda name: root.find(".//{*}" + name)
    frame, creation, mode = find("VideoFrame"), find("CreationDate"), find("RecordingMode")
    st = sidecar.stat()
    result = {"sidecar": str(sidecar), "sidecar_bytes": st.st_size, "sidecar_mtime_ns": st.st_mtime_ns}
    if frame is not None:
        result.update(capture_fps=rate(frame.get("captureFps")), format_fps=rate(frame.get("formatFps")))
    if creation is not None:
        result["creation"] = creation.get("value")
    if mode is not None:
        result["recording_mode"] = mode.get("type")
    return result


def inventory(ctx, limit=None):
    rows = []
    for card, cfg in sorted(ctx.cards.items()):
        files = sorted(p for p in cfg["root"].rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS
                       and not any(part.startswith(".") for part in p.relative_to(cfg["root"]).parts))
        for path in files:
            if limit is not None and len(rows) >= limit:
                break
            info = json.loads(run([ctx.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path]))
            videos = [s for s in info["streams"] if s["codec_type"] == "video"]
            if not videos:
                continue
            video = videos[0]; meta = sony_xml(path)
            fps = rate(video.get("avg_frame_rate")) or rate(video.get("r_frame_rate"))
            require(fps and fps > 0, "invalid_media", "Video has no usable frame rate", path=str(path))
            duration = float(video.get("duration", info["format"].get("duration", 0)))
            require(duration > 0, "invalid_media", "Video has no positive duration", path=str(path))
            frames = int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else round(duration * fps)
            capture_fps = meta.get("capture_fps") or fps
            ratio = capture_fps / fps if meta.get("recording_mode") == "slowAndQuickMotion" else 1.0
            creation = meta.get("creation") or video.get("tags", {}).get("creation_time") or info["format"].get("tags", {}).get("creation_time")
            timestamp = None
            if creation:
                parsed = dt.datetime.fromisoformat(creation.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    timestamp = parsed.timestamp()
            st = path.stat(); clip_id = card + "/" + path.relative_to(cfg["root"]).as_posix()
            fingerprint = digest({"id": clip_id, "bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "metadata": meta})
            rows.append({"id": clip_id, "card": card, "camera": cfg["camera"], "file": path.name, "path": str(path.resolve()),
                         "relative_path": path.relative_to(cfg["root"]).as_posix(), "bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
                         "fingerprint": fingerprint, "key": digest(clip_id)[:20], "duration": duration, "frames": frames, "fps": fps,
                         "capture_fps": capture_fps, "ratio": ratio, "real_duration": duration / ratio,
                         "creation": creation, "timestamp": timestamp,
                         "recording_date": dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date().isoformat() if timestamp is not None else None,
                         "variable_rate_suspected": bool(rate(video.get("avg_frame_rate")) and rate(video.get("r_frame_rate")) and abs(rate(video["avg_frame_rate"]) - rate(video["r_frame_rate"])) > .01),
                         "video": {k: video.get(k) for k in ("codec_name", "width", "height", "pix_fmt", "avg_frame_rate", "r_frame_rate")},
                         "audio_streams": [{k: s.get(k) for k in ("index", "codec_name", "sample_rate", "channels")} for s in info["streams"] if s["codec_type"] == "audio"],
                         "metadata": meta})
    require(rows, "no_media", "No source videos were found")
    previous = ctx.stage("inventory", required=False)
    require(not previous or [(c["id"], c["fingerprint"]) for c in previous["clips"]] == [(c["id"], c["fingerprint"]) for c in rows],
            "inventory_changed", "Source inventory changed in this session", "Start a new work directory; old recognition and timing artifacts stay available.")
    published = ctx.publish("inventory", {"clips": rows, "limited": limit is not None})
    return {**published, "clips": len(rows), "cameras": {camera: sum(r["camera"] == camera for r in rows) for camera in ("A", "B")}}


def read_wav(path):
    import numpy as np
    with wave.open(str(path), "rb") as handle:
        require(handle.getsampwidth() == 2, "invalid_audio", "Expected PCM16 audio")
        channels, sr = handle.getnchannels(), handle.getframerate()
        signal = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").reshape(-1, channels).astype(np.float32) / 32768
    return signal, sr


def prepare_audio(ctx, clip):
    import numpy as np
    source_unchanged(clip)
    target = ctx.work / "cache" / "audio" / clip["fingerprint"] / "audio16k.wav"
    meta = target.with_suffix(".json")
    if meta.exists():
        result = json.loads(meta.read_text(encoding="utf-8"))
        require(target.exists() or not result["has_audio"], "cache_incomplete", "Audio cache is incomplete", path=str(target))
        return result
    if not clip["audio_streams"]:
        result = {"clip_id": clip["id"], "fingerprint": clip["fingerprint"], "has_audio": False, "silent": True, "wav": None, "channels": []}
        write_json(meta, result)
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_name("decode-" + uuid.uuid4().hex + ".wav")
        run([ctx.ffmpeg, "-nostdin", "-v", "error", "-n", "-i", clip["path"], "-map", "0:a:0", "-vn",
             "-ar", "16000", "-acodec", "pcm_s16le", temporary], timeout=ctx.config.get("media_timeout", 1800))
        os.replace(temporary, target)
    data, sr = read_wav(target)
    require(len(data) > 0, "invalid_audio", "Decoded audio is empty", clip_id=clip["id"])
    rms = np.sqrt((data * data).mean(axis=0)); peak = np.max(np.abs(data), axis=0)
    correlation = float(np.corrcoef(data[::16, :2].T)[0, 1]) if data.shape[1] >= 2 and min(rms[:2]) > 1e-9 else None
    if correlation is not None and not np.isfinite(correlation):
        correlation = None
    channels = [{"channel": i + 1, "rms_db": float(20 * np.log10(max(float(rms[i]), 1e-10))),
                 "peak_db": float(20 * np.log10(max(float(peak[i]), 1e-10)))} for i in range(data.shape[1])]
    strong = int(np.argmax(rms)); selected = [strong + 1]
    if correlation is None or abs(correlation) < .98:
        selected.extend(i + 1 for i in range(data.shape[1]) if i != strong and rms[i] >= max(rms[strong] * .1, 1e-5))
    result = {"clip_id": clip["id"], "fingerprint": clip["fingerprint"], "has_audio": True, "wav": str(target),
              "seconds": len(data) / sr, "sample_rate": sr, "silent": bool(float(peak.max()) == 0),
              "channels": channels, "channel_correlation": correlation, "selected_channels": selected}
    write_json(meta, result)
    return result


def audio_stage(ctx):
    rows = [prepare_audio(ctx, c) for c in ctx.stage("inventory")["clips"]]
    return {**ctx.publish("audio", {"clips": rows}), "clips": len(rows), "silent": sum(c["silent"] for c in rows)}
