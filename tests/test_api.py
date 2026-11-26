"""Request formats for each provider, against a mocked HTTP layer."""

import base64
import io

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from PIL import Image

from custom_components.print_failure_monitor.api import AuthError, ask, downscale
from custom_components.print_failure_monitor.questions import build_questions

QUESTIONS, _ = build_questions({})
ANSWERS = {"plate_visible": 0.9, "spaghetti": 0.8, "detached": 0.1, "nozzle_blob": 0.2}


async def _ask(hass, provider, base_url, jpeg):
    return await ask(
        async_get_clientsession(hass),
        provider=provider,
        base_url=base_url,
        api_key="secret",
        model="m",
        questions=QUESTIONS,
        camera_description="A camera.",
        image=jpeg,
        content_type="image/jpeg",
        timeout=5,
    )


async def test_openrouter(hass: HomeAssistant, aioclient_mock, jpeg):
    aioclient_mock.post(
        "https://openrouter.ai/api/alpha/decisions",
        json={"answers": {q: {"type": "noul", "noul": p} for q, p in ANSWERS.items()}, "usage": {"cost": 0.0002}},
    )
    answers, usage = await _ask(hass, "openrouter", "https://openrouter.ai/api", jpeg)
    assert answers == ANSWERS
    assert usage["cost"] == 0.0002

    _, _, body, headers = aioclient_mock.mock_calls[-1]
    assert headers["Authorization"] == "Bearer secret"
    assert body["state"][0] == "A camera. The image below is `frame`."
    assert body["state"][1]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    assert body["questions"]["spaghetti"]["type"] == "noul"
    assert set(body["questions"]["spaghetti"]["criteria"]) == {"true", "false"}


async def test_google(hass: HomeAssistant, aioclient_mock, jpeg):
    aioclient_mock.post(
        "https://generativelanguage.googleapis.com/v1beta/decisions",
        json={"answers": {q: {"type": "noul", "noul": p, "confidence": p} for q, p in ANSWERS.items()}},
    )
    answers, _ = await _ask(hass, "google", "https://generativelanguage.googleapis.com", jpeg)
    assert answers == ANSWERS

    _, _, body, headers = aioclient_mock.mock_calls[-1]
    assert headers["x-goog-api-key"] == "secret"
    assert body["input"][1] == {"type": "image", "data": base64.b64encode(jpeg).decode(), "mime_type": "image/jpeg"}
    assert set(body["questions"]["detached"]["criteria"]) == {"true_description", "false_description"}


async def test_openai_format_via_gateway(hass: HomeAssistant, aioclient_mock, jpeg):
    aioclient_mock.post(
        "https://ai-gateway.vercel.sh/v1/decisions",
        json={"answers": [{"type": "predicate", "name": q, "probability": p} for q, p in reversed(ANSWERS.items())]},
    )
    answers, _ = await _ask(hass, "openai", "https://ai-gateway.vercel.sh/v1", jpeg)
    assert answers == ANSWERS  # matched by name, not position

    _, _, body, _ = aioclient_mock.mock_calls[-1]
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    spaghetti = next(q for q in body["questions"] if q["name"] == "spaghetti")
    assert spaghetti["type"] == "predicate"
    assert "Yes means:" in spaghetti["instructions"] and "No means:" in spaghetti["instructions"]


async def test_http_error_raises(hass: HomeAssistant, aioclient_mock, jpeg):
    aioclient_mock.post("https://openrouter.ai/api/alpha/decisions", status=402, text="no credits")
    with pytest.raises(RuntimeError, match="HTTP 402"):
        await _ask(hass, "openrouter", "https://openrouter.ai/api", jpeg)


def test_downscale(jpeg):
    assert downscale(jpeg, "image/jpeg", 1280) == (jpeg, "image/jpeg")  # already small enough
    small, content_type = downscale(jpeg, "image/png", 320)
    assert content_type == "image/jpeg" and len(small) < len(jpeg)


def test_downscale_keeps_the_short_side_usable():
    out = io.BytesIO()
    Image.new("RGB", (1920, 1080)).save(out, "JPEG")
    small, _ = downscale(out.getvalue(), "image/jpeg", 280)
    assert Image.open(io.BytesIO(small)).size == (455, 256)


@pytest.mark.parametrize(
    ("provider", "base", "path", "status", "text", "rejected"),
    [
        ("openrouter", "https://openrouter.ai/api", "/alpha/decisions", 401, "invalid key", True),
        (
            "google",
            "https://generativelanguage.googleapis.com",
            "/v1beta/decisions",
            400,
            '{"error": {"message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}',
            True,
        ),
        ("google", "https://generativelanguage.googleapis.com", "/v1beta/decisions", 400, "Unknown parameter", False),
        ("openai", "https://api.openai.com/v1", "/decisions", 429, "rate limited", False),
    ],
)
async def test_rejected_key_is_an_auth_error(hass, aioclient_mock, jpeg, provider, base, path, status, text, rejected):
    aioclient_mock.post(base + path, status=status, text=text)
    with pytest.raises(RuntimeError) as err:
        await _ask(hass, provider, base, jpeg)
    assert isinstance(err.value, AuthError) is rejected


def test_downscale_converts_other_formats(jpeg):
    png = io.BytesIO()
    Image.open(io.BytesIO(jpeg)).save(png, "PNG")
    out, kind = downscale(png.getvalue(), "image/png", 1280)  # small enough, but saved and re-sent as JPEG
    assert kind == "image/jpeg" and Image.open(io.BytesIO(out)).format == "JPEG"


def test_downscale_crops_before_scaling(jpeg):
    # jpeg is 640x360; the right half, top two thirds is 320x240, below the 256 px short-side floor
    out, kind = downscale(jpeg, "image/jpeg", 1280, [0.5, 0, 1, 2 / 3])
    assert kind == "image/jpeg"
    assert Image.open(io.BytesIO(out)).size == (341, 256)  # enlarged to keep the short side at 256


async def test_earlier_frame_is_sent_first(hass: HomeAssistant, aioclient_mock, jpeg):
    aioclient_mock.post(
        "https://generativelanguage.googleapis.com/v1beta/decisions",
        json={"answers": {q: {"type": "noul", "noul": p} for q, p in ANSWERS.items()}},
    )
    earlier = b"earlier-bytes"
    await ask(
        async_get_clientsession(hass),
        provider="google",
        base_url="https://generativelanguage.googleapis.com",
        api_key="secret",
        model="m",
        questions=QUESTIONS,
        camera_description="A camera.",
        image=jpeg,
        content_type="image/jpeg",
        timeout=5,
        earlier=(earlier, "image/png", 20),
    )
    _, _, body, _ = aioclient_mock.mock_calls[-1]
    assert "`earlier`, taken 20 printed layers ago" in body["input"][0]["text"]
    assert body["input"][1] == {"type": "image", "data": base64.b64encode(earlier).decode(), "mime_type": "image/png"}
    assert body["input"][2]["data"] == base64.b64encode(jpeg).decode()
