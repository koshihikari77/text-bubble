#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def log(message: str) -> None:
    print(f"[prompt_images] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send local images to llama-server using prompts loaded from system.txt and user.txt."
    )
    parser.add_argument("--dir", default="/notebooks/imgs", help="Directory containing images and prompt files")
    parser.add_argument("--system", help="Path to system prompt file (default: <dir>/system.txt)")
    parser.add_argument("--user", help="Path to user prompt file (default: <dir>/user.txt)")
    parser.add_argument("--server", default="http://127.0.0.1:8080/v1", help="llama-server base URL")
    parser.add_argument("--model", default="heretic", help="Model alias exposed by llama-server")
    parser.add_argument("--temperature", default=0.7, type=float, help="Sampling temperature")
    parser.add_argument("--top-k", default=40, type=int, help="top_k value")
    parser.add_argument("--top-p", default=0.95, type=float, help="top_p value")
    parser.add_argument("--seed", default=-1, type=int, help="Sampling seed (-1 for random)")
    parser.add_argument("--n-predict", default=220, type=int, help="Maximum completion tokens")
    parser.add_argument("--suffix", default=".txt", help="Output suffix for generated text files")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output files")
    parser.add_argument("--cache-prompt", action="store_true", help="Allow llama-server prompt cache reuse")
    parser.add_argument("--include", action="append", default=[], help="Only process files whose name contains this string")
    return parser.parse_args()


def load_text(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def encode_image_as_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def collect_images(directory: Path, include_filters: list[str]) -> list[Path]:
    images = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if include_filters and not any(token in path.name for token in include_filters):
            continue
        images.append(path)
    return images


def request_caption(
    *,
    server: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    image_path: Path,
    temperature: float,
    top_k: int,
    top_p: float,
    seed: int,
    n_predict: int,
    cache_prompt: bool,
) -> str:
    body = {
        "model": model,
        "temperature": temperature,
        "top_k": top_k,
        "top_p": top_p,
        "seed": seed,
        "n_predict": n_predict,
        "cache_prompt": cache_prompt,
        "reasoning_format": "none",
        "chat_template_kwargs": {
            "enable_thinking": False,
        },
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": encode_image_as_data_url(image_path)}},
                ],
            },
        ],
    }

    request = urllib.request.Request(
        url=f"{server.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"server returned HTTP {exc.code} for {image_path.name}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach llama-server: {exc}") from exc

    text = payload["choices"][0]["message"]["content"]
    if isinstance(text, list):
        text = "".join(chunk.get("text", "") for chunk in text if isinstance(chunk, dict))
    if not isinstance(text, str):
        raise RuntimeError(f"unexpected response content type for {image_path.name}")
    return text


def main() -> int:
    args = parse_args()
    directory = Path(args.dir)
    if not directory.is_dir():
        raise RuntimeError(f"directory not found: {directory}")

    system_path = Path(args.system) if args.system else directory / "system.txt"
    user_path = Path(args.user) if args.user else directory / "user.txt"
    system_prompt = load_text(system_path)
    user_prompt = load_text(user_path)
    system_hash = text_fingerprint(system_prompt)
    user_hash = text_fingerprint(user_prompt)

    images = collect_images(directory, args.include)
    if not images:
        raise RuntimeError(f"no image files found in {directory}")

    log(f"server={args.server} model={args.model}")
    log(f"dir={directory}")
    log(f"system={system_path}")
    log(f"user={user_path}")
    log(f"system_hash={system_hash} system_chars={len(system_prompt)}")
    log(f"user_hash={user_hash} user_chars={len(user_prompt)}")
    log(
        f"images={len(images)} overwrite={args.overwrite} "
        f"temperature={args.temperature} top_k={args.top_k} top_p={args.top_p} "
        f"seed={args.seed} n_predict={args.n_predict} cache_prompt={args.cache_prompt}"
    )

    for index, image_path in enumerate(images, start=1):
        output_path = image_path.with_suffix(args.suffix)
        if output_path.exists() and not args.overwrite:
            log(f"[{index}/{len(images)}] skip existing {output_path}")
            continue
        log(f"[{index}/{len(images)}] request {image_path.name} -> {output_path.name}")
        started_at = time.monotonic()
        text = request_caption(
            server=args.server,
            model=args.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_path=image_path,
            temperature=args.temperature,
            top_k=args.top_k,
            top_p=args.top_p,
            seed=args.seed,
            n_predict=args.n_predict,
            cache_prompt=args.cache_prompt,
        )
        output_path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
        elapsed = time.monotonic() - started_at
        log(f"[{index}/{len(images)}] saved {output_path} ({len(text)} chars, {elapsed:.1f}s)")

    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
