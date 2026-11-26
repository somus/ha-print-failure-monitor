# Providers and cost

## Providers

| Provider | Reaches | Base URL | Example model |
|---|---|---|---|
| OpenRouter | About 30 image-capable decision models from Google, OpenAI, Microsoft, Liquid, Cloudflare and others, with one key | default | `google/gemini-decision-1` |
| Google (Gemini API) | Gemini directly, including the free tier | default | `gemini-decision-1` |
| OpenAI format | OpenAI directly | default | `gpt-6-luna` |
| OpenAI format | Vercel AI Gateway | `https://ai-gateway.vercel.sh/v1` | `openai/gpt-6-luna-decisions` |
| OpenAI format | Any other gateway or proxy that serves OpenAI's `/v1/decisions` (Azure Foundry, a self-hosted LiteLLM proxy) | the gateway's `/v1` URL | the gateway's model ID |

OpenAI's format has no separate yes/no criteria, so the integration adds each question's criteria
to its instructions. It also turns off prompt caching. Caching can't help here because every frame
is new, and it would bill cache writes above the normal input price.

## What gets sent

Each check sends one camera frame to the provider, or two when frames are compared with a
[layer sensor](setup.md#layer-sensor). Frames are sent only while a print is running, when you
press Check now or use the `check` action, and when Test settings asks the model again about your
labeled frames. With a [plate area](setup.md#plate-area) set, only that part of the frame is sent.

## Cost

OpenRouter: with the default model, a 1280×720 frame costs about $0.0002, so checking once a
minute costs roughly $0.012 per printing hour, plus a little more in each print's first 15 minutes.
Comparing frames with a layer sensor sends two images, which roughly doubles that. OpenRouter adds
5.5% when you buy credits.

Google free tier: `gemini-decision-1` is free for up to 10,000 requests a day, enough for over 150
hours of printing a day at the default intervals. Google uses free-tier inputs, which here are your
camera frames, to improve its products. Use a Gemini API key from a project without billing.

Google paid tier: $0.10 per million input tokens, the same as OpenRouter without the credit fee,
and Google doesn't use your frames. This applies to any project with billing turned on.

To cap spending, set a [Daily check limit](alerts.md#daily-check-limit).
