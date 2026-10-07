# gptimg — a GPT Image 2 CLI and a local image API proxy

`gptimg` is one Python file with two jobs. As a **CLI** it draws and edits images with GPT Image 2 (or 2.5 Flare / Sunburst) from your terminal. As a **local proxy** (`gptimg serve`) it gives your notebooks, scripts and other tools an OpenAI-style Images endpoint on `127.0.0.1` that adds your real key on the way out, forwards only the models you allow, stops at a daily budget and writes a ledger of what every request cost.

The upstream is any OpenAI-compatible Images API. The default is [ArgoLink](https://argolink.io/gh-gpt-image-2-cli-proxy), where GPT Image 2 costs $0.015 per image up to 2K and $0.02 at 4K.

<sub>Last checked: 2026-10-07. Python 3.8+, standard library only. An independent tool, not affiliated with OpenAI.</sub>

## Install

```bash
git clone https://github.com/delphinewoodrow7/gpt-image-2-cli-proxy
cd gpt-image-2-cli-proxy
export GPT_IMAGE_API_KEY=your-key                 # an ArgoLink key, or your upstream's
# export GPT_IMAGE_BASE_URL=https://other-host/v1  # any OpenAI-style Images API
alias gptimg="python3 $PWD/gptimg.py"
```

## The CLI

```bash
gptimg draw "A koi pond seen from directly above, lily pads, 1:1 composition" -o koi.png
gptimg draw "Minimal poster for a jazz night, title \"BLUE NOTES\", 3:4 composition" --size 1440x1920 --quality high -n 3 -o poster.png
gptimg edit "Same pond at night, lanterns on the water, keep the fish" -i koi.png -o koi-night.png
gptimg edit "Put the logo from the second image on the mug in the first" -i mug.png -i logo.png --model gpt-image-2.5-sunburst
gptimg models                                     # image model IDs the upstream lists
```

| Option | Default | Meaning |
|---|---|---|
| `-o, --output` | `image.png` | file name; with `-n` above 1 you get `name-1.png`, `name-2.png`, … |
| `--model` | `gpt-image-2` | or `gpt-image-2.5-flare`, `gpt-image-2.5-sunburst` |
| `--size` | `1024x1024` | pixels, `WIDTHxHEIGHT`; write the ratio in the prompt as well ("16:9 composition") |
| `--quality` | `auto` | `auto`, `low`, `medium`, `high` |
| `-n` | 1 | 1–7 images in one request, each billed |
| `-i, --image` | — | `edit` only: a reference file, repeat up to 16 times (sent as `image[]` parts) |
| `--mask` | — | `edit` only: a PNG with alpha marking what to change in the first image |

Useful sizes: `1024x1024` (1:1), `2048x1152` (16:9), `1152x2048` (9:16), `1440x1920` (3:4), `3840x2160` (16:9 at 4K). The CLI prints the estimated cost of each run on stderr.

## The proxy

```bash
gptimg serve --budget 5 --allow gpt-image-2,gpt-image-2.5-flare
# proxy on http://127.0.0.1:8787/v1 -> https://argolink.io/v1; ledger gptimg-ledger.jsonl
```

Point anything that speaks the OpenAI Images API at it, with any placeholder key:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="local")   # the real key stays in the proxy
image = client.images.generate(model="gpt-image-2", prompt="A paper crane on a desk, 1:1 composition",
                               size="1024x1024", response_format="b64_json")
```

```bash
curl -s http://127.0.0.1:8787/v1/images/generations -H "Content-Type: application/json" \
  -d '{"model":"gpt-image-2","prompt":"A paper crane on a desk, 1:1 composition","size":"1024x1024","response_format":"b64_json"}'
```

What it forwards and what it refuses:

| Request | Result |
|---|---|
| `POST /v1/images/generations`, `POST /v1/images/edits` (JSON or multipart) | forwarded unchanged, plus `Authorization: Bearer <your key>` |
| `GET /v1/models` | forwarded |
| a model not in `--allow` | `403`, never sent upstream |
| a request whose estimate would pass `--budget` for today | `402`, never sent upstream |
| no `Authorization: Bearer <token>` when started with `--token` | `401` |
| any other path | `404` |

Every forwarded request appends a line to the ledger:

```json
{"time": "2026-10-07T14:03:11", "endpoint": "/v1/images/generations", "model": "gpt-image-2", "size": "3840x2160", "n": 2, "status": 200, "returned": 2, "cost": 0.04}
```

The budget check uses an estimate from `size` and `n` before sending; the ledger records the real cost afterwards from the pixels each returned image has (PNG and JPEG headers are read; URL results are priced by the requested size). Prompts are not written unless you pass `--log-prompts`. Prices in the code are the default upstream's GPT Image prices; edit `PRICES` if you point it elsewhere.

### Why run a proxy for one key

- Tools that only take a base URL and a key never see the real key, so it isn't copied into ten config files and notebooks.
- A runaway loop stops at the budget instead of at the end of your balance.
- One ledger answers "which script spent what" without logging into a dashboard.
- Swapping upstreams is one environment variable, not a change in every tool.

## What a budget buys

| Daily budget | 1K or 2K images ($0.015) | 4K images ($0.02) |
|---:|---:|---:|
| $1 | 66 | 50 |
| $5 | 333 | 250 |
| $20 | 1,333 | 1,000 |

<sub>Default upstream prices from the [GPT Image 2 model page](https://argolink.io/en/models/gpt-image-2), checked 2026-10-07; the tier follows the returned image's longest edge (above 2048 px is 4K) and quality doesn't change it. For comparison, OpenAI's own API lists GPT Image 2 at $0.006 / $0.053 / $0.211 per 1024×1024 image at low / medium / high, output tokens only ([image generation guide](https://developers.openai.com/api/docs/guides/image-generation)).</sub>

## Security

- The proxy listens on loopback by default and refuses another interface unless you also set `--token`.
- It forwards three routes only; everything else gets a 404.
- The real key is read from `GPT_IMAGE_API_KEY` at start-up and sent only to `GPT_IMAGE_BASE_URL`.
- The ledger holds no image data and, by default, no prompts.

## FAQ

**Is this related to CLIProxyAPI?**
No. CLIProxyAPI turns coding and chat CLIs (Codex, Claude Code, Gemini and others) into OpenAI-, Gemini- and Claude-compatible endpoints; its README, checked 2026-10-07, lists no Images endpoint. `gptimg` does the reverse for images: a CLI and a proxy in front of an Images API you already have.

**Can the proxy forward Nano Banana or Grok image models?**
Yes, add them to `--allow`; the requests pass through untouched. The ledger's prices are for GPT Image models, though, so its cost column will be wrong for others.

**Does it stream or cache images?**
Neither. Each request waits for the finished images (up to 600 seconds) and nothing is stored except the ledger line.

**Why are edits multipart?**
Local files travel as repeated `image[]` parts (and an optional `mask` part), the same way the official OpenAI SDKs send a list of files. Up to 16 references per edit.

**Who refused my request, the proxy or the upstream?**
If the error body has `"type": "proxy_error"`, the proxy did (token, budget, model or path); anything else came from the upstream unchanged, for example a 401 for a wrong key in `GPT_IMAGE_API_KEY`.

## Tests

```bash
python3 -m unittest discover -s tests
```

A fake upstream runs on localhost, so the tests need no key and spend nothing.

## License

MIT.
