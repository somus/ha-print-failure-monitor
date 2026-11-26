"""Decision-model calls. Kept free of Home Assistant imports so it can be tested standalone."""

from __future__ import annotations

import base64
import io

import aiohttp
from PIL import Image

from .questions import state_text

# provider -> (default base URL, default model)
PROVIDERS = {
    "openrouter": ("https://openrouter.ai/api", "google/gemini-decision-1"),
    "google": ("https://generativelanguage.googleapis.com", "gemini-decision-1"),
    "openai": ("https://api.openai.com/v1", "gpt-6-luna"),
}


# Providers reject images with a shorter side than this (OpenRouter documents 256 px for decision models).
MIN_SHORT_SIDE = 256


def downscale(image: bytes, content_type: str, max_side: int, crop: list[float] | None = None) -> tuple[bytes, str]:
    """Cut the frame to crop ([left, top, right, bottom] as fractions), then shrink it so its longest side is at
    most max_side. The short side is never left below MIN_SHORT_SIDE: a small crop is enlarged to reach it.

    Blocking; run in an executor.
    """
    img = Image.open(io.BytesIO(image))
    if crop:
        left, top, right, bottom = crop
        w, h = img.size
        img = img.crop((round(left * w), round(top * h), round(right * w), round(bottom * h)))
    scale = min(1, max_side / max(img.size))
    scale = max(scale, MIN_SHORT_SIDE / min(img.size))
    if scale == 1 and not crop and content_type == "image/jpeg":
        return image, content_type  # saved and re-sent as JPEG, so anything else is converted
    img = img.convert("RGB")
    if scale != 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


# Each request builder takes images as [(base64, content type)], in the order the state text names them.


def _openrouter_request(base_url, api_key, model, questions, text, images):
    url = f"{base_url.rstrip('/')}/alpha/decisions"
    body = {
        "model": model,
        "state": [text, *({"type": "image_url", "image_url": {"url": f"data:{t};base64,{b}"}} for b, t in images)],
        "questions": {
            qid: {
                "type": "noul",
                "instructions": q["instructions"],
                "criteria": {"true": q["yes_means"], "false": q["no_means"]},
            }
            for qid, q in questions.items()
        },
    }
    return url, body, {"Authorization": f"Bearer {api_key}"}


def _google_request(base_url, api_key, model, questions, text, images):
    url = f"{base_url.rstrip('/')}/v1beta/decisions"
    body = {
        "model": model,
        "input": [{"type": "text", "text": text}, *({"type": "image", "data": b, "mime_type": t} for b, t in images)],
        "questions": {
            qid: {
                "type": "noul",
                "instructions": q["instructions"],
                "criteria": {"true_description": q["yes_means"], "false_description": q["no_means"]},
            }
            for qid, q in questions.items()
        },
    }
    return url, body, {"x-goog-api-key": api_key}


def _openai_request(base_url, api_key, model, questions, text, images):
    url = f"{base_url.rstrip('/')}/decisions"
    # Predicates have no criteria field, so the yes/no meanings go into the instructions.
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": text},
                    *({"type": "input_image", "image_url": f"data:{t};base64,{b}"} for b, t in images),
                ],
            }
        ],
        "questions": [
            {
                "type": "predicate",
                "name": qid,
                "instructions": f"{q['instructions']} Yes means: {q['yes_means']} No means: {q['no_means']}",
            }
            for qid, q in questions.items()
        ],
        # Each frame is new, so a cache never hits; explicit mode without breakpoints avoids cache-write charges.
        "prompt_cache_options": {"mode": "explicit"},
    }
    return url, body, {"Authorization": f"Bearer {api_key}"}


class AuthError(RuntimeError):
    """The provider rejected the API key."""


def _rejected_key(status: int, text: str) -> bool:
    # Google answers an invalid key with 400 API_KEY_INVALID rather than 401.
    return status in (401, 403) or (status == 400 and ("API_KEY_INVALID" in text or "API key not valid" in text))


def _answers(provider: str, questions: dict, data: dict) -> dict[str, float]:
    if provider == "openai":
        by_name = {a["name"]: a for a in data["answers"]}
        return {qid: float(by_name[qid]["probability"]) for qid in questions}
    return {qid: float(data["answers"][qid]["noul"]) for qid in questions}


async def ask(
    session: aiohttp.ClientSession,
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    questions: dict[str, dict],
    camera_description: str,
    image: bytes,
    content_type: str,
    timeout: float,
    earlier: tuple[bytes, str, int] | None = None,
) -> tuple[dict[str, float], dict]:
    """Return (noul probability per question id, usage).

    earlier is (image, content type, layers ago): an earlier frame of the same print, sent first so questions
    can compare the two.
    """
    build = {"openrouter": _openrouter_request, "google": _google_request, "openai": _openai_request}[provider]
    images = [(image, content_type)]
    if earlier:
        images.insert(0, earlier[:2])
    url, body, headers = build(
        base_url,
        api_key,
        model,
        questions,
        state_text(camera_description, earlier[2] if earlier else None),
        [(base64.b64encode(data).decode(), kind) for data, kind in images],
    )
    async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            text = await resp.text()
            error = AuthError if _rejected_key(resp.status, text) else RuntimeError
            raise error(f"HTTP {resp.status}: {text[:300]}")
        data = await resp.json()
    return _answers(provider, questions, data), data.get("usage") or {}
