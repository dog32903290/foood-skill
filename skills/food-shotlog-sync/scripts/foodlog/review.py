"""Produce real frame bundles; import explicit native Codex or human reviews."""
from __future__ import annotations

import base64
from pathlib import Path

from .core import digest, file_hash, inside, read_json, require, run, source_unchanged, stamp, write_json
from .recognition import chat


def make_pack(ctx, clip_ids=None, frames=4, batch_size=12):
    from PIL import Image, ImageDraw, ImageFont
    require(1 <= frames <= 12 and 1 <= batch_size <= 24, "invalid_budget", "Use 1–12 frames per clip and 1–24 clips per contact sheet")
    inventory = ctx.stage("inventory")["clips"]
    selected = [c for c in inventory if not clip_ids or c["id"] in clip_ids]
    if clip_ids:
        require(len(selected) == len(set(clip_ids)), "unknown_clip", "A requested review clip was not found")
    folder = ctx.work / "review-packs" / stamp(); folder.mkdir(parents=True)
    records = []
    for clip in selected:
        source_unchanged(clip); images = []
        for index in range(frames):
            fraction = .5 if frames == 1 else .1 + .8 * index / (frames - 1)
            frame = min(clip["frames"] - 1, max(0, round(clip["duration"] * fraction * clip["fps"])))
            position = frame / clip["fps"]
            path = folder / f"{clip['key']}_{index + 1:02d}.jpg"
            run([ctx.ffmpeg, "-nostdin", "-v", "error", "-n", "-ss", f"{position:.8f}", "-i", clip["path"],
                 "-frames:v", "1", "-vf", "scale=1280:-2", "-update", "1", path], timeout=ctx.config.get("media_timeout", 1800))
            with Image.open(path) as image:
                require(image.width > 0 and image.height > 0, "invalid_preview", "Frame image could not be decoded")
            images.append({"path": str(path), "sha256": file_hash(path), "frame": frame, "time_seconds": position})
        records.append({"clip_id": clip["id"], "clip_key": clip["key"], "fingerprint": clip["fingerprint"], "images": images})
    try:
        font = ImageFont.truetype(ctx.config.get("font_path", "DejaVuSans.ttf"), 15)
    except OSError:
        font = ImageFont.load_default()
    sheets = []
    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]
        page = Image.new("RGB", (320 * frames, len(batch) * 205), "white")
        draw = ImageDraw.Draw(page)
        for row, record in enumerate(batch):
            for col, asset in enumerate(record["images"]):
                image = Image.open(asset["path"]).convert("RGB"); image.thumbnail((320, 180))
                page.paste(image, (col * 320, row * 205 + 25))
                draw.text((col * 320 + 3, row * 205 + 4), f"{record['clip_key']}  {asset['time_seconds']:.2f}s", fill="black", font=font)
        dest = folder / f"contact_{start // batch_size + 1:03d}.jpg"; page.save(dest, quality=90)
        sheets.append({"path": str(dest), "sha256": file_hash(dest), "clip_ids": [x["clip_id"] for x in batch]})
    manifest = {"clips": records, "contact_sheets": sheets, "required_action": "The Codex host must actually open these images. This command does not invoke or impersonate Codex vision."}
    write_json(folder / "manifest.json", manifest)
    return {**ctx.publish("review_pack", manifest), "manifest": str(folder / "manifest.json"), "clips": len(records), "frames": len(records) * frames, "contact_sheets": sheets}


def local_vision(ctx):
    cfg = ctx.config.get("vision_model", {})
    require(cfg.get("endpoint") and cfg.get("model"), "invalid_config", "Configure a local vision endpoint and model")
    pack = ctx.stage("review_pack"); results = []
    for clip in pack["clips"]:
        content = [{"type": "text", "text": "這些是同一原檔依時間排序的截圖。用繁體中文記錄景別、實際動作、食材外觀與環境。不猜姓名、食材品種或不可見的製程；不從畫面推斷對白。回傳簡短描述和不確定處。"}]
        for asset in clip["images"]:
            require(file_hash(asset["path"]) == asset["sha256"], "evidence_changed", "Review image changed")
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(Path(asset["path"]).read_bytes()).decode("ascii")}})
        results.append({"clip_id": clip["clip_id"], "model": cfg["model"], "description": chat(cfg["endpoint"], cfg["model"], [{"role": "user", "content": content}])})
    return {**ctx.publish("local_vision", {"clips": results}), "clips": len(results), "review_status": "machine_draft"}


def import_review(ctx, path):
    data = read_json(path)
    require(data.get("review_mode") in ("codex_native_vision", "human_visual_review"), "invalid_review", "Review must identify a native Codex or human visual reviewer")
    require(isinstance(data.get("reviewer"), str) and data["reviewer"].strip(), "invalid_review", "Record the reviewer or active host model")
    pack = ctx.stage("review_pack"); inventory = {c["id"]: c for c in ctx.stage("inventory")["clips"]}
    assets = {x["path"]: (c["clip_id"], x) for c in pack["clips"] for x in c["images"]}
    previous = ctx.stage("reviews", required=False)
    results = {x["clip_id"]: x for x in previous.get("clips", [])} if previous else {}
    seen = set()
    for review in data.get("clips", []):
        clip_id = review.get("clip_id")
        require(clip_id in inventory and clip_id not in seen, "invalid_review", "Review has an unknown or duplicate clip ID", clip_id=clip_id)
        seen.add(clip_id)
        require(review.get("scene_description") and review.get("evidence"), "invalid_review", "A reviewed shot needs a description and actual viewed image references", clip_id=clip_id)
        for asset in review["evidence"]:
            candidate = assets.get(asset.get("path"))
            require(candidate and candidate[0] == clip_id and asset.get("sha256") == candidate[1]["sha256"], "invalid_evidence", "Evidence must match this clip's current review pack", clip_id=clip_id)
            require(file_hash(asset["path"]) == asset["sha256"], "evidence_changed", "A reviewed image changed after capture")
        for segment in review.get("meaningful_dialogue", []):
            require(0 <= float(segment["start"]) < float(segment["end"]) <= inventory[clip_id]["duration"] + .1,
                    "invalid_timecode", "Dialogue lies outside the source clip", clip_id=clip_id)
            require(segment.get("basis") in ("asr", "asr_consensus", "audio_review"), "invalid_review", "Dialogue needs its own audio basis; vision does not verify spoken words")
        results[clip_id] = {**review, "review_mode": data["review_mode"], "reviewer": data["reviewer"], "fingerprint": inventory[clip_id]["fingerprint"]}
    require(seen, "invalid_review", "Review contains no clips")
    return {**ctx.publish("reviews", {"clips": list(results.values())}), "imported_clips": len(seen), "total_reviewed": len(results)}
