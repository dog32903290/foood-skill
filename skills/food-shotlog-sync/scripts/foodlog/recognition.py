"""Optional local recognition backends. No hidden calls to paid cloud APIs."""
from __future__ import annotations

import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import urllib.request
import wave

from .core import HarnessError, dependency, digest, local_endpoint, read_json, require, write_json
from .media import prepare_audio, read_wav

ASR_MODELS = {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"}
MLX_MODELS = {"mlx-community/whisper-large-v3-turbo"}
AST_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"


def download_model(kind, name):
    if kind == "faster-whisper":
        require(name in ASR_MODELS, "model_not_allowlisted", "Choose a documented Whisper model name")
        dependency("faster_whisper", "asr")
        from faster_whisper.utils import download_model as fetch
        with contextlib.redirect_stdout(sys.stderr):
            path = fetch(name)
    elif kind == "mlx-whisper":
        require(name in MLX_MODELS, "model_not_allowlisted", "Choose an allowlisted MLX Whisper repository")
        dependency("huggingface_hub", "mlx")
        from huggingface_hub import snapshot_download
        path = snapshot_download(name)
    elif kind == "ast":
        require(name == AST_MODEL, "model_not_allowlisted", "Use the official MIT AST AudioSet model")
        dependency("huggingface_hub", "environment")
        from huggingface_hub import snapshot_download
        path = snapshot_download(name)
    else:
        raise HarnessError("unknown_backend", "Unknown model download backend")
    return {"backend": kind, "model": name, "cache_path": str(path)}


def pcm_bytes(signal, sample_rate=16000):
    import numpy as np
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(sample_rate)
        handle.writeframes((np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def chat(endpoint, model, messages, *, max_tokens=1200, timeout=180):
    endpoint = local_endpoint(endpoint)
    payload = {"model": model, "messages": messages, "temperature": 0.0, "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(endpoint + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            reply = json.load(response)
        return reply["choices"][0]["message"]["content"]
    except Exception as exc:
        raise HarnessError("local_model_failed", str(exc), "Check the local server, selected model, modality support, and context length.")


def transcribe(ctx, clip_ids=None):
    import numpy as np
    cfg = ctx.config.get("asr", {})
    backend = cfg.get("backend", "faster-whisper")
    name = cfg.get("model", "small")
    language = cfg.get("language", "zh")
    # Explicit download is a separate command. Recognition itself is offline.
    with contextlib.redirect_stdout(sys.stderr):
        if backend == "faster-whisper":
            dependency("faster_whisper", "asr")
            require(name in ASR_MODELS or Path(name).is_dir(), "model_not_allowlisted", "ASR model must be an allowlisted name or an existing local model folder")
            from faster_whisper import WhisperModel
            model = WhisperModel(name, device=cfg.get("device", "cpu"), compute_type=cfg.get("compute_type", "int8"),
                                 cpu_threads=cfg.get("threads", 4), local_files_only=True)
        elif backend == "mlx-whisper":
            dependency("mlx_whisper", "mlx")
            from huggingface_hub import snapshot_download
            import mlx_whisper
            require(name in MLX_MODELS or Path(name).is_dir(), "model_not_allowlisted", "MLX model must be allowlisted or already on disk")
            model = name if Path(name).is_dir() else snapshot_download(name, local_files_only=True)
        else:
            raise HarnessError("unknown_backend", "ASR backend must be faster-whisper or mlx-whisper")
    previous = ctx.stage("transcripts", required=False)
    results = {x["clip_id"]: x for x in previous.get("clips", [])} if previous else {}
    rows = ctx.stage("inventory")["clips"]
    if clip_ids:
        rows = [c for c in rows if c["id"] in clip_ids]
        require(len(rows) == len(set(clip_ids)), "unknown_clip", "An ASR clip ID was not in the inventory")
    for clip in rows:
        audio = prepare_audio(ctx, clip)
        cache = ctx.work / "cache" / "asr" / digest({"source": clip["fingerprint"], "config": cfg})
        segments = []
        if not audio["silent"]:
            signal, sr = read_wav(audio["wav"])
            chunk_seconds = int(cfg.get("chunk_seconds", 60))
            require(10 <= chunk_seconds <= 300, "invalid_config", "ASR chunk length must be between 10 and 300 seconds")
            for channel in audio["selected_channels"]:
                for begin in range(0, len(signal), chunk_seconds * sr):
                    # A small overlap avoids dropping a word at a chunk boundary.
                    lo = max(0, begin - sr // 2); hi = min(len(signal), begin + chunk_seconds * sr + sr // 2)
                    dest = cache / f"ch{channel}_{lo}_{hi}.json"
                    if dest.exists():
                        segments.extend(read_json(dest)["segments"]); continue
                    mono = signal[lo:hi, channel - 1].copy()
                    peak = float(np.max(np.abs(mono)))
                    mono *= min(8., .85 / max(peak, 1e-8))
                    with contextlib.redirect_stdout(sys.stderr):
                        if backend == "faster-whisper":
                            gen, _ = model.transcribe(mono, language=language, beam_size=5, vad_filter=True,
                                                      condition_on_previous_text=False, no_speech_threshold=.55)
                            raw = [{"start": s.start, "end": s.end, "text": s.text, "avg_logprob": s.avg_logprob,
                                    "no_speech_prob": s.no_speech_prob} for s in gen]
                        else:
                            reply = mlx_whisper.transcribe(mono, path_or_hf_repo=model, language=language, verbose=None,
                                                           condition_on_previous_text=False, no_speech_threshold=.55)
                            raw = reply["segments"]
                    chunk = []
                    for s in raw:
                        a, b = lo / sr + float(s["start"]), min(hi / sr, lo / sr + float(s["end"]))
                        if b <= a or float(s.get("no_speech_prob", 0)) > .8:
                            continue
                        chunk.append({"start": a, "end": b, "channel": channel, "text": s["text"].strip(),
                                      "avg_logprob": s.get("avg_logprob"), "no_speech_prob": s.get("no_speech_prob")})
                    write_json(dest, {"segments": chunk, "model": name, "backend": backend})
                    segments.extend(chunk)
        dedup = []
        for segment in sorted(segments, key=lambda s: (s["start"], s["channel"])):
            if any(s["channel"] == segment["channel"] and s["text"] == segment["text"] and abs(s["start"] - segment["start"]) < 1 for s in dedup[-8:]):
                continue
            dedup.append(segment)
        results[clip["id"]] = {"clip_id": clip["id"], "fingerprint": clip["fingerprint"], "backend": backend, "model": name,
                                "silent": audio["silent"], "segments": dedup}
    return {**ctx.publish("transcripts", {"clips": list(results.values())}), "clips": len(rows), "segments": sum(len(results[c["id"]]["segments"]) for c in rows)}


def environment(ctx, backend="ast", clip_ids=None):
    import numpy as np
    cfg = ctx.config.get("environment", {})
    if backend == "ast":
        dependency("transformers", "environment"); dependency("torch", "environment")
        import torch
        from transformers import AutoFeatureExtractor, AutoModelForAudioClassification
        name = AST_MODEL
        extractor = AutoFeatureExtractor.from_pretrained(name, local_files_only=True)
        model = AutoModelForAudioClassification.from_pretrained(name, local_files_only=True).eval()
        torch.set_num_threads(cfg.get("threads", 3))
    else:
        name = cfg.get("model")
        require(backend == "local-audio" and name and cfg.get("endpoint"), "invalid_config", "A local audio model and endpoint are required")
    previous = ctx.stage("environment", required=False)
    results = {x["clip_id"]: x for x in previous.get("clips", [])} if previous else {}
    rows = [c for c in ctx.stage("inventory")["clips"] if not clip_ids or c["id"] in clip_ids]
    if clip_ids:
        require(len(rows) == len(set(clip_ids)), "unknown_clip", "An environmental-audio clip ID was not in the inventory")
    seconds = 10 if backend == "ast" else 60
    for clip in rows:
        audio = prepare_audio(ctx, clip); windows = []
        if not audio["silent"]:
            data, sr = read_wav(audio["wav"])
            # Never average independent microphones or opposite-phase channels.
            best = max(audio["channels"], key=lambda x: x["rms_db"])["channel"] - 1
            signal = data[:, best]
            for lo in range(0, len(signal), seconds * sr):
                hi = min(len(signal), lo + seconds * sr)
                cache = ctx.work / "cache" / "environment" / digest({"source": clip["fingerprint"], "backend": backend, "model": name, "config": cfg}) / f"{lo}_{hi}.json"
                if cache.exists():
                    windows.append(read_json(cache)); continue
                mono = signal[lo:hi]
                if backend == "ast":
                    x = np.pad(mono, (0, max(0, sr - len(mono))))
                    inputs = extractor(x, sampling_rate=sr, return_tensors="pt")
                    with torch.inference_mode():
                        probabilities = torch.sigmoid(model(**inputs).logits[0])
                    scores, indexes = torch.topk(probabilities, 8)
                    record = {"start": lo / sr, "end": hi / sr, "tags": [{"label": model.config.id2label[int(i)], "score": float(v)} for v, i in zip(scores, indexes)]}
                else:
                    encoded = base64.b64encode(pcm_bytes(mono, sr)).decode("ascii")
                    content = [{"type": "text", "text": "用繁體中文描述實際聽到的環境聲和人聲。不要猜畫面、人物或時間碼。只列確實聽懂的短句，聽不清寫［聽不清］。"},
                               {"type": "input_audio", "input_audio": {"data": encoded, "format": "wav"}}]
                    text = chat(cfg["endpoint"], name, [{"role": "user", "content": content}], timeout=cfg.get("timeout", 180))
                    record = {"start": lo / sr, "end": hi / sr, "description": text}
                write_json(cache, record); windows.append(record)
        results[clip["id"]] = {"clip_id": clip["id"], "fingerprint": clip["fingerprint"], "backend": backend, "model": name, "windows": windows}
    return {**ctx.publish("environment", {"clips": list(results.values())}), "clips": len(rows), "backend": backend}


def polish(ctx):
    cfg = ctx.config.get("text_model", {})
    require(cfg.get("endpoint") and cfg.get("model"), "invalid_config", "Configure a local text model first")
    results = []
    for clip in ctx.stage("transcripts")["clips"]:
        results.append({"clip_id": clip["clip_id"], "raw_segments": clip["segments"], "suggestion": chat(cfg["endpoint"], cfg["model"], [
            {"role": "system", "content": "你是逐字稿校對員。只提議斷句和明顯錯字修正，不增加原稿沒有的句子、姓名、材料或工序。聽不清處保留。保留每段時間和聲道，回傳 JSON。這是機器建議，不是音訊驗證。"},
            {"role": "user", "content": json.dumps(clip["segments"], ensure_ascii=False)}])})
    return {**ctx.publish("polish", {"clips": results, "model": cfg["model"], "applied_to_raw": False}), "clips": len(results)}
