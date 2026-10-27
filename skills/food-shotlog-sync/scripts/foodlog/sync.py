"""Waveform evidence first; calibrated recording dates for silent/S&Q media."""
from __future__ import annotations

from pathlib import Path
import math

from .core import digest, read_json, require, write_json
from .media import prepare_audio, read_wav

SYNC_RATE = 2000


def signal(ctx, clip):
    import numpy as np
    meta = prepare_audio(ctx, clip)
    if meta["silent"]:
        return None, meta
    cache = ctx.work / "cache" / "sync" / (clip["fingerprint"] + ".npy")
    if cache.exists():
        return np.load(cache, mmap_mode="r"), meta
    audio, sr = read_wav(meta["wav"])
    factor = sr // SYNC_RATE
    y = audio[:len(audio) // factor * factor].reshape(-1, factor, audio.shape[1]).mean(axis=1)
    y = np.diff(y, axis=0, prepend=y[:1]).astype(np.float32)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("xb") as handle:
        np.save(handle, y)
    return y, meta


def correlate(template, target):
    import numpy as np
    template = template.astype(np.float64); target = target.astype(np.float64)
    n, m = len(template), len(target)
    if n < SYNC_RATE * .4 or m < n:
        return None
    template -= template.mean()
    energy = float(template @ template)
    if energy < 1e-16:
        return None
    fft_size = 1 << (m + n - 2).bit_length()
    cross = np.fft.irfft(np.fft.rfft(target, fft_size) * np.fft.rfft(template[::-1], fft_size), fft_size)[n - 1:m]
    sums = np.r_[0, np.cumsum(target)]; squares = np.r_[0, np.cumsum(target * target)]
    variance = np.maximum(squares[n:] - squares[:-n] - (sums[n:] - sums[:-n]) ** 2 / n, 1e-25)
    normalized = cross / np.sqrt(variance * energy + 1e-25)
    best = int(np.argmax(np.abs(normalized)))
    return best / SYNC_RATE, float(normalized[best])


def waveform(ctx, a, b, expected=None):
    import numpy as np
    cfg = ctx.config.get("sync", {})
    # Cache also the expensive unsuccessful comparisons; model/config changes invalidate it.
    cache = ctx.work / "cache" / "matches" / (digest({"a": a["fingerprint"], "b": b["fingerprint"], "expected": expected, "config": cfg, "version": 1}) + ".json")
    if cache.exists():
        return read_json(cache)
    xa, ma = signal(ctx, a); xb, mb = signal(ctx, b)
    if xa is None or xb is None or a["ratio"] != 1 or b["ratio"] != 1:
        result = {"confirmed": False, "anchors": [], "reason": "no_comparable_real_time_audio"}
        write_json(cache, result); return result
    ac = max(ma["channels"], key=lambda c: c["rms_db"])["channel"] - 1
    best_b = max(c["rms_db"] for c in mb["channels"])
    channels = [c["channel"] - 1 for c in mb["channels"] if c["rms_db"] >= best_b - 30]
    duration_a, duration_b = len(xa) / SYNC_RATE, len(xb) / SYNC_RATE
    lo, hi = 0., duration_a
    if expected is not None:
        lo, hi = max(0., -expected) + .1, min(duration_a, duration_b - expected) - .1
    window = min(10., duration_b * .5, (hi - lo) / 3)
    anchors = []
    if window >= .5:
        for position in np.linspace(lo, hi - window, 5):
            position = round(float(position) * SYNC_RATE) / SYNC_RATE
            template = xa[int(position * SYNC_RATE):int((position + window) * SYNC_RATE), ac]
            winner = None
            for bc in channels:
                search_lo, search_hi = 0., duration_b
                if expected is not None:
                    search_lo, search_hi = max(0., position + expected - 4), min(duration_b, position + expected + window + 4)
                match = correlate(template, xb[int(search_lo * SYNC_RATE):int(search_hi * SYNC_RATE), bc])
                if match is None:
                    continue
                lag = match[0] + int(search_lo * SYNC_RATE) / SYNC_RATE - position
                candidate = {"a_time": position, "b_time": position + lag, "lag": lag, "correlation": match[1], "window": window,
                             "a_channel": ac + 1, "b_channel": bc + 1}
                if winner is None or abs(candidate["correlation"]) > abs(winner["correlation"]):
                    winner = candidate
            if winner:
                anchors.append(winner)
    floor = float(cfg.get("anchor_min_correlation", .12)); tolerance = float(cfg.get("lag_tolerance_seconds", .04))
    cluster = []
    for anchor in anchors:
        group = [x for x in anchors if abs(x["lag"] - anchor["lag"]) <= tolerance and abs(x["correlation"]) >= floor]
        if (len(group), sum(abs(x["correlation"]) for x in group)) > (len(cluster), sum(abs(x["correlation"]) for x in cluster)):
            cluster = group
    span = max((x["a_time"] for x in cluster), default=0) - min((x["a_time"] for x in cluster), default=0)
    confirmed = len(cluster) >= 3 and span >= max(1., window) and max((abs(x["correlation"]) for x in cluster), default=0) >= float(cfg.get("peak_min_correlation", .18))
    result = {"confirmed": confirmed, "anchors": anchors, "consistent_anchors": cluster,
              "lag": float(np.median([x["lag"] for x in cluster])) if confirmed else None,
              "lag_spread": max((x["lag"] for x in cluster), default=0) - min((x["lag"] for x in cluster), default=0)}
    write_json(cache, result)
    return result


def nearby_recording(a, b):
    if a["timestamp"] is None or b["timestamp"] is None:
        return True
    return abs(a["timestamp"] - b["timestamp"]) < 36 * 3600


def pair(ctx, explicit_bias=None):
    import numpy as np
    inventory = ctx.stage("inventory")["clips"]
    aa = [c for c in inventory if c["camera"] == "A"]
    bb = [c for c in inventory if c["camera"] == "B"]
    require(aa and bb, "camera_missing", "Inventory must contain A and B camera cards")
    cfg = ctx.config.get("sync", {}); cap = int(cfg.get("max_waveform_comparisons", 10000))
    count = 0; found = {}; seeds = []

    def compare(a, b, expected=None):
        nonlocal count
        count += 1
        require(count <= cap, "comparison_budget", "Waveform comparison budget reached", "Limit the cards to one recording session or supply --clock-bias-seconds; cached work is reusable.", comparisons=count)
        result = waveform(ctx, a, b, expected)
        if result["confirmed"]:
            found[a["id"], b["id"]] = result
        return result

    bias = explicit_bias if explicit_bias is not None else cfg.get("clock_bias_seconds")
    if bias is None:
        candidates = [a for a in aa if a["ratio"] == 1 and a["duration"] >= 8]
        # Spread discovery across the card, rather than relying on a repeated first line.
        order = sorted(range(len(candidates)), key=lambda i: (i % max(1, len(candidates) // 5), i))
        target = int(cfg.get("clock_seed_pairs", 3))
        require(3 <= target <= 20, "invalid_config", "Automatic clock calibration requires 3–20 distinct waveform pairs")
        for index in order:
            a = candidates[index]
            if prepare_audio(ctx, a)["silent"]:
                continue
            for b in bb:
                if not nearby_recording(a, b) or b["ratio"] != 1:
                    continue
                result = compare(a, b)
                if result["confirmed"] and a["timestamp"] is not None and b["timestamp"] is not None:
                    seeds.append(a["timestamp"] - b["timestamp"] - result["lag"])
                    break
            if len(seeds) >= target:
                break
        if len(seeds) >= target:
            median = float(np.median(seeds))
            require(max(abs(v - median) for v in seeds) <= cfg.get("clock_seed_tolerance_seconds", 3),
                    "clock_inconsistent", "Camera clock offsets disagree across confirmed pairs", "Split recording sessions or provide an explicit calibrated clock bias; do not apply one offset across a clock reset.", offsets=seeds)
            bias = median
    for a in aa:
        for b in bb:
            if not nearby_recording(a, b):
                continue
            predicted = None
            if bias is not None and a["timestamp"] is not None and b["timestamp"] is not None:
                predicted = a["timestamp"] - b["timestamp"] - bias
                overlap = min(a["real_duration"], b["real_duration"] - predicted) - max(0., -predicted)
                if overlap < -.5:
                    continue
            if a["ratio"] == 1 and b["ratio"] == 1 and (a["id"], b["id"]) not in found:
                compare(a, b, predicted)
    if bias is not None:
        offsets = [a["timestamp"] - b["timestamp"] - found[a["id"], b["id"]]["lag"]
                   for a in aa for b in bb if (a["id"], b["id"]) in found and a["timestamp"] is not None and b["timestamp"] is not None]
        require(all(abs(v - bias) <= cfg.get("clock_seed_tolerance_seconds", 3) for v in offsets),
                "clock_inconsistent", "Confirmed waveform pairs disagree with the camera clock calibration",
                "Split clock-reset sessions or inspect false waveform matches before using metadata pairs.", offsets=offsets)
    results = []
    for a in aa:
        for b in bb:
            match = found.get((a["id"], b["id"]))
            is_wave = bool(match)
            if is_wave:
                lag = match["lag"]
            elif bias is not None and a["timestamp"] is not None and b["timestamp"] is not None and nearby_recording(a, b):
                lag = a["timestamp"] - b["timestamp"] - bias
            else:
                continue
            # The first release expects B to carry a real-time reference, not a second S&Q clock.
            if b["ratio"] != 1:
                continue
            lo, hi = max(0., -lag), min(a["real_duration"], b["real_duration"] - lag)
            if hi - lo < float(cfg.get("minimum_overlap_seconds", .5)):
                continue
            results.append({"a_id": a["id"], "b_id": b["id"], "a_in": lo * a["ratio"], "a_out": hi * a["ratio"],
                            "b_in": lo + lag, "b_out": hi + lag, "duration": hi - lo,
                            "ratio": a["ratio"], "lag": lag if is_wave else None,
                            "basis": "原音波形" if is_wave else "拍攝時間（無原音）" if a["ratio"] != 1 else "拍攝時間",
                            "clock_bias_seconds": bias, "metadata_uncertainty_seconds": None if is_wave else cfg.get("clock_uncertainty_seconds", 2),
                            "waveform_evidence": match, "subject": ""})
    positions = {x["id"]: x.get("timestamp") or 0 for x in inventory}
    results.sort(key=lambda x: (x["basis"] != "原音波形", positions[x["a_id"]], x["a_id"], positions[x["b_id"]], x["b_id"]))
    for index, item in enumerate(results, 1):
        item["group"] = f"AB{index:03d}"
    require(results, "no_pairs", "No usable simultaneous pairs were found", "Check microphone channels, recording dates, camera groups, and model-independent source timing.")
    return {**ctx.publish("pairs", {"pairs": results, "clock_bias_seconds": bias, "clock_seeds": seeds, "waveform_comparisons": count}),
            "pairs": len(results), "waveform_pairs": sum(x["basis"] == "原音波形" for x in results), "clock_bias_seconds": bias}


def validate_pairs(rows, inventory):
    by_id = {c["id"]: c for c in inventory}; seen = set(); groups = set()
    require(rows, "no_pairs", "Pair table contains no rows")
    for row in rows:
        require(isinstance(row.get("group"), str) and row["group"].strip() and row["group"] not in groups,
                "invalid_group", "Every pair needs a unique nonempty string group ID")
        groups.add(row["group"])
        require(all(isinstance(row.get(k), (int, float)) and not isinstance(row[k], bool) and math.isfinite(row[k]) for k in
                    ("a_in", "a_out", "b_in", "b_out", "duration", "ratio")) and row["ratio"] > 0,
                "invalid_timecode", "Pair times and ratios must be finite numbers with a positive ratio")
        require(row.get("lag") is None or isinstance(row["lag"], (int, float)) and math.isfinite(row["lag"]), "invalid_lag", "Lag must be a finite number or null")
        require(row.get("basis") in ("原音波形", "拍攝時間（無原音）", "拍攝時間"), "invalid_basis", "Unrecognized pairing basis")
        require(row["a_id"] in by_id and row["b_id"] in by_id, "unknown_clip", "Pair references an unknown source ID", group=row.get("group"))
        a, b = by_id[row["a_id"]], by_id[row["b_id"]]
        require(a["camera"] == "A" and b["camera"] == "B", "invalid_pair", "Camera sides are reversed")
        key = row["a_id"], row["b_id"]
        require(key not in seen, "duplicate_pair", "Pair table contains a duplicate A/B pair")
        seen.add(key)
        require(0 <= row["a_in"] < row["a_out"] <= a["duration"] + .05 and 0 <= row["b_in"] < row["b_out"] <= b["duration"] + .05,
                "invalid_timecode", "Pair interval is outside a source clip", group=row.get("group"))
        require(abs(row["ratio"] - a["ratio"]) < 1e-4, "invalid_ratio", "A playback/capture ratio disagrees with inventory")
        require(abs((row["a_out"] - row["a_in"]) / row["ratio"] - (row["b_out"] - row["b_in"])) < .05,
                "duration_mismatch", "A and B do not describe the same real duration", group=row.get("group"))
        require(abs(row["duration"] - (row["b_out"] - row["b_in"])) < .05, "duration_mismatch", "Overlap duration disagrees with in/out points")
        if row["basis"] == "原音波形":
            require(row.get("lag") is not None and abs(row["b_in"] - row["a_in"] - row["lag"]) < .05,
                    "invalid_lag", "Waveform offset sign or units are inconsistent")
    return {"pairs": len(rows), "waveform_pairs": sum(x["basis"] == "原音波形" for x in rows), "valid": True}
