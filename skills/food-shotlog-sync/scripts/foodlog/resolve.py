"""A real Resolve backend with new-project isolation and resumable placement."""
from __future__ import annotations

import importlib
import math
import os
from pathlib import Path
import sys
import unicodedata

from .core import HarnessError, digest, file_hash, read_json, require, source_unchanged, stamp, write_json
from .media import rate
from .sync import validate_pairs


def connect(ctx=None):
    settings = ctx.config.get("resolve", {}) if ctx else {}
    candidates = [settings.get("script_modules_path"), os.environ.get("RESOLVE_SCRIPT_API")]
    if sys.platform == "darwin":
        candidates.append("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules")
    elif os.name == "nt":
        candidates.append(str(Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules"))
    else:
        candidates.append("/opt/resolve/Developer/Scripting/Modules")
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            if (path / "Modules").is_dir():
                path = path / "Modules"
            if (path / "DaVinciResolveScript.py").is_file():
                sys.path.insert(0, str(path)); break
    try:
        module = importlib.import_module("DaVinciResolveScript")
        app = module.scriptapp("Resolve")
    except Exception as exc:
        raise HarnessError("resolve_api_unavailable", str(exc), "Start Resolve, enable external scripting if your edition supports it, and configure the installed SDK module path. Do not copy proprietary SDK libraries into this repository.")
    require(app is not None, "resolve_not_connected", "Resolve scripting API did not connect", "Start Resolve and check its scripting settings and edition.")
    return app


def status(ctx=None):
    app = connect(ctx); pm = app.GetProjectManager(); project = pm.GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    return {"version": app.GetVersionString(), "page": app.GetCurrentPage(), "project": project.GetName() if project else None,
            "timeline": timeline.GetName() if timeline else None,
            "timelines": [project.GetTimelineByIndex(i).GetName() for i in range(1, project.GetTimelineCount() + 1)] if project else [],
            "backend_connected": True, "ui_review_complete": False}


def make_plan(ctx):
    inventory = ctx.stage("inventory")["clips"]; pairs = ctx.stage("pairs")["pairs"]
    validate_pairs(pairs, inventory)
    cfg = ctx.config.get("resolve", {}); fps = rate(cfg.get("fps", "30000/1001"))
    require(fps and fps > 0, "invalid_config", "Resolve timeline FPS must be positive")
    by_id = {c["id"]: c for c in inventory}; placements = []; cursor = 0
    gap = int(cfg.get("gap_frames", round(fps)))
    require(0 <= gap <= fps * 60, "invalid_config", "Review gaps must be between zero and one minute")
    for row in pairs:
        a, b = by_id[row["a_id"]], by_id[row["b_id"]]
        require(b["ratio"] == 1 and b["audio_streams"], "unsupported_reference", "B must be a real-time reference clip with an audio stream")
        require(not a.get("variable_rate_suspected") and not b.get("variable_rate_suspected"),
                "variable_rate_unsupported", "Frame-accurate Resolve placement requires constant-frame-rate media", "Inspect source timestamps and create separately named CFR intermediates before using this adapter.")
        source_unchanged(a); source_unchanged(b)
        ai, bi = round(row["a_in"] * a["fps"]), round(row["b_in"] * b["fps"])
        a_fps = a["fps"] * row["ratio"]
        count = min(round(row["duration"] * fps), math.floor((a["frames"] - ai) / a_fps * fps + 1e-7),
                    math.floor((b["frames"] - bi) / b["fps"] * fps + 1e-7))
        require(count >= 1, "empty_placement", "Pair has no complete frame after quantization", group=row["group"])
        placements.append({**row, "relative_frame": cursor, "frames": count, "a_source_in": ai, "b_source_in": bi,
                           "a_source_frames": min(a["frames"] - ai, round(count * a_fps / fps)),
                           "b_source_frames": min(b["frames"] - bi, round(count * b["fps"] / fps)),
                           "a_target_fps": a_fps, "a_has_audio": bool(a["audio_streams"]), "a_path": a["path"], "b_path": b["path"]})
        cursor += count + gap
    required = {r[k] for r in pairs for k in ("a_id", "b_id")}
    payload = {"project_name": cfg.get("project_name", "Food_AB_Review_" + stamp()[:15]),
               "timeline_name": cfg.get("timeline_name", "AB雙機校對_A下B上"), "start_timecode": cfg.get("start_timecode", "01:00:00:00"),
               "fps": fps, "fps_setting": cfg.get("fps_setting", "29.97" if abs(fps - 30000 / 1001) < .001 else str(round(fps, 6))),
               "width": int(cfg.get("width", 1920)), "height": int(cfg.get("height", 1080)), "placements": placements,
               "required_media": [c for c in inventory if c["id"] in required], "total_frames": cursor - gap,
               "source_pair_fingerprint": digest(pairs)}
    return {**ctx.publish("resolve_plan", payload), "pairs": len(pairs), "media": len(required), "duration_minutes": (cursor - gap) / fps / 60,
            "project_name": payload["project_name"], "will_create_new_project": True}


def checked_plan(ctx):
    plan = ctx.stage("resolve_plan")
    require(plan["source_pair_fingerprint"] == digest(ctx.stage("pairs")["pairs"]),
            "plan_changed", "Pair table changed after Resolve planning", "Make a new plan before first apply; use a new session if construction already began.")
    current = {c["id"]: c for c in ctx.stage("inventory")["clips"]}
    for source in plan["required_media"]:
        require(source["id"] in current and source["fingerprint"] == current[source["id"]]["fingerprint"],
                "plan_changed", "Resolve plan references a different source inventory")
        source_unchanged(source)
    return plan


def canonical(path):
    return unicodedata.normalize("NFC", os.path.normcase(os.path.realpath(path)))


def media_items(folder):
    result = {}
    for item in folder.GetClipList() or []:
        path = item.GetClipProperty("File Path")
        if path:
            result[canonical(path)] = item
    for child in folder.GetSubFolderList() or []:
        result.update(media_items(child))
    return result


def apply(ctx, *, execute=False, resume=False):
    require(execute, "execution_not_authorized", "Resolve apply requires --execute after an explicit user request to create the timeline", "Run resolve plan and inspect the result first.")
    plan = checked_plan(ctx); app = connect(ctx); pm = app.GetProjectManager()
    state_path = ctx.work / "resolve_state.json"
    state = read_json(state_path) if state_path.exists() else None
    require(not state or resume, "session_exists", "This session already has Resolve construction state", "Use --resume to continue the owned project without duplicating clips.")
    if state:
        require(state["plan_fingerprint"] == digest(plan), "plan_changed", "Resolve plan changed after construction began", "Use a new work directory and project; do not append a different plan into the existing review.")
        current = pm.GetCurrentProject()
        if current and current.GetName() != state["project_name"]:
            require(pm.SaveProject(), "save_failed", "Could not save the currently open project before switching")
            backup = ctx.work / "resolve-backups" / stamp() / "before_resume.drp"; backup.parent.mkdir(parents=True)
            require(pm.ExportProject(current.GetName(), str(backup), False), "backup_failed", "Could not back up the currently open project")
        project = pm.LoadProject(state["project_name"])
        require(project and project.GetUniqueId() == state["project_id"], "project_identity_mismatch", "Owned Resolve project could not be safely identified")
    else:
        require(plan["project_name"] not in (pm.GetProjectListInCurrentFolder() or []), "project_exists", "Refusing to reuse a pre-existing Resolve project", "Choose a new project name or resume an existing owned session.")
        current = pm.GetCurrentProject()
        if current:
            require(pm.SaveProject(), "save_failed", "Could not save the current project")
            backup = ctx.work / "resolve-backups" / stamp() / "before_switch.drp"; backup.parent.mkdir(parents=True)
            require(pm.ExportProject(current.GetName(), str(backup), False), "backup_failed", "Current project backup failed; construction stopped")
        project = pm.CreateProject(plan["project_name"])
        require(project is not None, "project_create_failed", "Could not create the review project")
        state = {"project_name": project.GetName(), "project_id": project.GetUniqueId(), "plan_fingerprint": digest(plan), "groups": {}}
        write_json(state_path, state, replace=True)
    if not state.get("configured"):
        require(project.SetSettings({"timelineFrameRate": plan["fps_setting"], "timelineResolutionWidth": str(plan["width"]),
                                     "timelineResolutionHeight": str(plan["height"]), "timelineInputResMismatchBehavior": "scaleToFit", "timelineSampleRate": "48000"}),
                "settings_failed", "Resolve rejected review-project settings")
        state["configured"] = True
        write_json(state_path, state, replace=True)
    require(abs(float(project.GetSettings()["timelineFrameRate"]) - plan["fps"]) < .01, "settings_changed", "Review-project frame rate changed")
    pool = project.GetMediaPool(); root = pool.GetRootFolder()
    for card in sorted({x["card"] for x in plan["required_media"]}):
        folder = next((f for f in root.GetSubFolderList() if f.GetName() == card), None) or pool.AddSubFolder(root, card)
        require(folder and pool.SetCurrentFolder(folder), "bin_failed", "Could not select the card bin", card=card)
        existing = media_items(root)
        wanted = [c for c in plan["required_media"] if c["card"] == card and canonical(c["path"]) not in existing]
        for c in wanted:
            source_unchanged(c)
        if wanted:
            require(pool.ImportMedia([c["path"] for c in wanted]) is not None, "import_failed", "Resolve media import failed", card=card)
    items = media_items(root)
    for c in plan["required_media"]:
        require(canonical(c["path"]) in items, "import_missing", "Imported source was not found by full path", clip_id=c["id"])
    # Do this BEFORE the timeline is created. SetSpeed on a trimmed S&Q clip can
    # reset its out point in some Resolve versions; media FPS conform avoids it.
    conformed = {}
    for row in plan["placements"]:
        if row["ratio"] == 1:
            continue
        item = items[canonical(row["a_path"])]; target = row["a_target_fps"]
        if abs(float(item.GetClipProperty("FPS")) - target) > .001:
            require(not state["groups"], "late_conform", "A source FPS changed after timeline placement began", "Stop and inspect this owned project rather than changing speeds underneath existing clips.")
            require(item.SetClipProperty("FPS", str(round(target, 6))), "conform_failed", "Resolve rejected capture-FPS conform", clip_id=row["a_id"])
        require(abs(float(item.GetClipProperty("FPS")) - target) < .001, "conform_failed", "Capture-FPS readback disagrees")
        conformed[row["a_id"]] = target
    folder = next((f for f in root.GetSubFolderList() if f.GetName() == "TIMELINES"), None) or pool.AddSubFolder(root, "TIMELINES")
    require(folder and pool.SetCurrentFolder(folder), "bin_failed", "Could not select the timeline bin")
    timeline = next((project.GetTimelineByIndex(i) for i in range(1, project.GetTimelineCount() + 1)
                     if project.GetTimelineByIndex(i).GetName() == plan["timeline_name"]), None)
    if timeline is None:
        timeline = pool.CreateEmptyTimeline(plan["timeline_name"])
        require(timeline is not None and timeline.SetStartTimecode(plan["start_timecode"]), "timeline_failed", "Could not create the review timeline")
        for kind in ("video", "audio"):
            while timeline.GetTrackCount(kind) < 2:
                require(timeline.AddTrack(kind) if kind == "video" else timeline.AddTrack(kind, "stereo"), "track_failed", "Could not add a camera track")
        for kind, index, name in (("video", 1, "A機（下）"), ("video", 2, "B機（上）"), ("audio", 1, "A機原音（參考）"), ("audio", 2, "B機原音")):
            require(timeline.SetTrackName(kind, index, name), "track_failed", "Could not name a review track")
        require(timeline.SetTrackEnable("audio", 1, False), "track_failed", "Could not mute reference A audio")
    require(not state.get("timeline_id") or state["timeline_id"] == timeline.GetUniqueId(), "timeline_identity_mismatch", "Owned timeline identity changed")
    state["timeline_id"] = timeline.GetUniqueId()
    write_json(state_path, state, replace=True)
    require(project.SetCurrentTimeline(timeline), "timeline_failed", "Could not activate the review timeline")
    app.OpenPage("edit"); start = int(timeline.GetStartFrame())

    def append(item, source_in, frames, record, track, media_type):
        kind = "video" if media_type == 1 else "audio"
        existing = [c for c in timeline.GetItemListInTrack(kind, track) if int(c.GetStart()) == record]
        if existing:
            require(len(existing) == 1 and canonical(existing[0].GetMediaPoolItem().GetClipProperty("File Path")) == canonical(item.GetClipProperty("File Path")),
                    "ambiguous_resume", "An unexpected clip occupies the planned position", record_frame=record, track=track)
            require(abs(existing[0].GetSourceStartFrame() - source_in) <= 1 and abs(existing[0].GetDuration() - frames / float(item.GetClipProperty("FPS")) * plan["fps"]) <= 1,
                    "ambiguous_resume", "An existing item has different trim or duration", record_frame=record, track=track)
            return existing[0]
        placed = pool.AppendToTimeline([{"mediaPoolItem": item, "startFrame": source_in, "endFrame": source_in + frames,
                                        "mediaType": media_type, "trackIndex": track, "recordFrame": record}])
        require(placed and len(placed) == 1, "append_failed", "Resolve rejected a source range", record_frame=record, track=track)
        return placed[0]

    for row in plan["placements"]:
        if row["group"] in state["groups"]:
            continue
        record = start + row["relative_frame"]
        a, b = items[canonical(row["a_path"])], items[canonical(row["b_path"])]
        av = append(a, row["a_source_in"], row["a_source_frames"], record, 1, 1)
        bv = append(b, row["b_source_in"], row["b_source_frames"], record, 2, 1)
        ba = append(b, row["b_source_in"], row["b_source_frames"], record, 2, 2)
        aa = append(a, row["a_source_in"], row["a_source_frames"], record, 1, 2) if row["ratio"] == 1 and row["a_has_audio"] else None
        for item in (av, bv, ba):
            require(int(item.GetStart()) == record and abs(item.GetDuration() - row["frames"]) <= 1,
                    "placement_mismatch", "Readback disagrees with the planned timeline range", group=row["group"])
        require(abs(av.GetSourceStartFrame() - row["a_source_in"]) <= 1 and abs(bv.GetSourceStartFrame() - row["b_source_in"]) <= 1,
                "source_in_mismatch", "Resolve changed an in point", group=row["group"])
        if aa:
            require(timeline.SetClipsLinked([av, aa], True), "link_failed", "Could not link A video and audio")
        require(timeline.SetClipsLinked([bv, ba], True), "link_failed", "Could not link B video and audio")
        color = "Blue" if row["basis"] == "原音波形" else "Orange"
        marker = "Blue" if color == "Blue" else "Yellow"  # Orange is NOT a Resolve marker color.
        require(av.SetClipColor(color) and bv.SetClipColor(color), "color_failed", "Could not color-code camera clips")
        note = f"A {row['a_id']}\nB {row['b_id']}\n同步依據：{row['basis']}\n{row.get('subject', '')}"
        if not timeline.GetMarkerByCustomData(row["group"]):
            require(timeline.AddMarker(row["relative_frame"], marker, row["group"] + " " + row.get("subject", "")[:45], note, 1, row["group"]),
                    "marker_failed", "Could not add a review marker", group=row["group"])
        state["groups"][row["group"]] = {"record_frame": record, "frames": row["frames"]}
        write_json(state_path, state, replace=True)
        require(pm.SaveProject(), "save_failed", "Could not save placement checkpoint")
    require(timeline.SetCurrentTimecode(plan["start_timecode"]), "position_failed", "Could not locate the beginning of the review")
    require(pm.SaveProject(), "save_failed", "Could not save the completed review")
    return {"project": project.GetName(), "timeline": timeline.GetName(), "pairs": len(state["groups"]), "conformed_A_clips": len(conformed),
            "ui_review_complete": False, "next": "Capture Resolve, actually view the screenshot, then register UI observations. API counts alone are not visual proof."}


def inspect(ctx):
    plan = checked_plan(ctx); app = connect(ctx); project = app.GetProjectManager().GetCurrentProject()
    require(project and project.GetName() == plan["project_name"], "wrong_project", "The active Resolve project is not this review")
    timeline = project.GetCurrentTimeline()
    require(timeline and timeline.GetName() == plan["timeline_name"], "wrong_timeline", "The active timeline is not this review")
    state = read_json(ctx.work / "resolve_state.json")
    require(project.GetUniqueId() == state["project_id"] and timeline.GetUniqueId() == state["timeline_id"] and state["plan_fingerprint"] == digest(plan),
            "project_identity_mismatch", "Active project, timeline or construction plan identity changed")
    for source in plan["required_media"]:
        source_unchanged(source)
    counts, tracks = {}, {}
    start = int(timeline.GetStartFrame())
    for kind in ("video", "audio"):
        for index in range(1, timeline.GetTrackCount(kind) + 1):
            entries = []
            for item in timeline.GetItemListInTrack(kind, index):
                media = item.GetMediaPoolItem()
                entries.append({"start": item.GetStart(), "end": item.GetEnd(), "duration": item.GetDuration(),
                                "source_in": item.GetSourceStartFrame(), "path": media.GetClipProperty("File Path"),
                                "fps": float(media.GetClipProperty("FPS")), "online": media.GetClipProperty("Online Status")})
            key = f"{kind}{index}"; counts[key] = len(entries)
            tracks[key] = {"name": timeline.GetTrackName(kind, index), "enabled": timeline.GetIsTrackEnabled(kind, index), "items": entries}
    require(counts.get("video1") == counts.get("video2") == counts.get("audio2") == len(plan["placements"]), "count_mismatch", "Review track counts disagree with the plan")
    require(counts.get("audio1", 0) == sum(r["ratio"] == 1 and r["a_has_audio"] for r in plan["placements"]), "count_mismatch", "A reference-audio count disagrees with the plan")
    for row in plan["placements"]:
        record = start + row["relative_frame"]
        a = next((x for x in tracks["video1"]["items"] if x["start"] == record), None)
        b = next((x for x in tracks["video2"]["items"] if x["start"] == record), None)
        audio = next((x for x in tracks["audio2"]["items"] if x["start"] == record), None)
        require(a and b and audio and canonical(a["path"]) == canonical(row["a_path"]) and canonical(b["path"]) == canonical(row["b_path"]),
                "source_mismatch", "A source clip is missing or on the wrong track", group=row["group"])
        require(abs(a["duration"] - row["frames"]) <= 1 and abs(b["duration"] - row["frames"]) <= 1 and b["end"] == audio["end"],
                "duration_mismatch", "A/B or B audio duration is wrong", group=row["group"])
        require(a["online"] == b["online"] == "Online", "offline_media", "Resolve reports offline media", group=row["group"])
        require(abs(a["source_in"] - row["a_source_in"]) <= 1 and abs(b["source_in"] - row["b_source_in"]) <= 1,
                "source_in_mismatch", "Source trim does not match the pairing table", group=row["group"])
        require(canonical(audio["path"]) == canonical(row["b_path"]) and abs(audio["source_in"] - row["b_source_in"]) <= 1, "source_mismatch", "B audio trim or media disagrees with B video")
        require(timeline.GetMarkerByCustomData(row["group"]), "marker_missing", "A pair marker is missing", group=row["group"])
        require(abs(a["fps"] - row["a_target_fps"]) < .001, "fps_mismatch", "A capture-FPS conform is wrong")
    require(tracks["video1"]["enabled"] and tracks["video2"]["enabled"] and tracks["audio2"]["enabled"], "track_disabled", "A required review track is disabled")
    return {"project": project.GetName(), "timeline": timeline.GetName(), "api_valid": True, "counts": counts, "tracks": tracks,
            "start_frame": start, "start_timecode": timeline.GetStartTimecode(), "current_timecode": timeline.GetCurrentTimecode(),
            "ui_review_complete": bool((ctx.stage("resolve_ui_review", required=False) or {}).get("plan_fingerprint") == digest(plan))}


def export(ctx, directory):
    report = inspect(ctx); app = connect(ctx); pm = app.GetProjectManager(); project = pm.GetCurrentProject(); timeline = project.GetCurrentTimeline()
    directory = Path(directory).expanduser().resolve()
    require(not directory.exists(), "output_exists", "Use a new export directory")
    directory.mkdir(parents=True)
    require(pm.SaveProject(), "save_failed", "Could not save before export")
    drt, drp = directory / "AB_review.drt", directory / "AB_review.drp"
    require(timeline.Export(str(drt), app.EXPORT_DRT, app.EXPORT_NONE), "export_failed", "DRT export failed")
    require(pm.ExportProject(project.GetName(), str(drp), False), "export_failed", "DRP export failed")
    for path in (drt, drp):
        require(path.is_file() and path.stat().st_size > 100, "invalid_export", "Resolve export is missing or empty", path=str(path))
    write_json(directory / "api_verification.json", report)
    return {"exports": [{"path": str(x), "bytes": x.stat().st_size, "sha256": file_hash(x)} for x in (drt, drp)],
            "ui_review_complete": report["ui_review_complete"], "note": "UI review is a separately recorded observation; exports reference the original source media."}


def capture(ctx, output, window_id=None):
    from PIL import Image
    from .core import run
    require(sys.platform == "darwin", "capture_backend_missing", "Built-in window capture is currently macOS-only", "Use your host's real computer-use screenshot tool on Windows/Linux, then pass that image to resolve ui-review.")
    if window_id is None:
        try:
            import Quartz
        except ImportError:
            raise HarnessError("capture_dependency_missing", "Automatic window discovery requires PyObjC Quartz", "Install the capture extra or supply --window-id from your desktop tooling.")
        windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        candidates = [w for w in windows if "Resolve" in w.get("kCGWindowOwnerName", "") and w.get("kCGWindowBounds", {}).get("Height", 0) > 500]
        require(candidates, "window_missing", "No visible Resolve window was found", "Bring Resolve to the foreground and grant screen-recording access if necessary.")
        window_id = max(candidates, key=lambda w: w["kCGWindowBounds"]["Height"] * w["kCGWindowBounds"]["Width"])["kCGWindowNumber"]
    output = Path(output).resolve()
    require(not output.exists(), "output_exists", "Refusing to overwrite a screenshot")
    output.parent.mkdir(parents=True, exist_ok=True)
    run(["/usr/sbin/screencapture", "-x", "-l", str(int(window_id)), output])
    with Image.open(output) as image:
        require(image.width > 0 and image.height > 0, "invalid_capture", "Screenshot could not be decoded")
    return {"screenshot": str(output), "sha256": file_hash(output), "ui_review_complete": False, "next": "Actually open this image in the Codex host and record observations; do not infer success from capture alone."}


def ui_review(ctx, path):
    from PIL import Image
    report = inspect(ctx)
    review = read_json(path); plan = ctx.stage("resolve_plan")
    require(review.get("project") == plan["project_name"] and review.get("timeline") == plan["timeline_name"], "wrong_ui_review", "UI observations refer to a different project/timeline")
    require(review.get("reviewer") and review.get("observations") and review.get("screenshots"), "invalid_ui_review", "Record the actual reviewer, observations, and viewed screenshots")
    for asset in review["screenshots"]:
        require(Path(asset["path"]).is_file() and file_hash(asset["path"]) == asset["sha256"], "invalid_evidence", "Screenshot hash does not match")
        with Image.open(asset["path"]) as image:
            require(image.width > 0 and image.height > 0, "invalid_evidence", "Screenshot is not a real image")
    checks = review.get("checks", {})
    required = ("correct_timeline_visible", "A_below_B", "video_clip_blocks_visible", "audio_waveform_visible", "saved_and_reopened")
    require(all(checks.get(k) is True for k in required), "ui_review_incomplete", "One of the required visible UI checks is incomplete", required=list(required))
    return {**ctx.publish("resolve_ui_review", {"review": review, "plan_fingerprint": digest(plan), "api_counts": report["counts"]}), "ui_review_complete": True,
            "note": "This records the reviewer's explicit observation. The harness does not claim that image hashes alone prove UI correctness."}
