---
name: brave-web-search
description: USE FOR web search when English authoritative sources or structured result metadata (thumbnails, age, extra snippets) are needed. Returns ranked results with snippets, URLs, thumbnails. Supports freshness filters, SafeSearch, Goggles for custom ranking, pagination. Secondary to gemini for general search — prefer gemini unless user specifically requests Brave or needs English-focused results.
---

# Brave Web Search

## When to Use

Use this skill for **English authoritative sources** — tech docs, official sites, news from reputable outlets. It supplements Gemini: prefer Gemini for general/Chinese-language queries; use Brave when you need precise English source coverage, freshness-filtered results, or custom domain ranking via Goggles.

## Quick Start

```bash
curl -s "https://api.search.brave.com/res/v1/web/search?q=python+web+frameworks&count=10" \
  -H "Accept: application/json" \
  -H "X-Subscription-Token: ${BRAVE_SEARCH_API_KEY}"
```

With freshness filter:
```bash
curl -s "https://api.search.brave.com/res/v1/web/search" \
  -H "Accept: application/json" \
  -H "X-Subscription-Token: ${BRAVE_SEARCH_API_KEY}" \
  -G \
  --data-urlencode "q=rust memory safety" \
  --data-urlencode "count=10" \
  --data-urlencode "freshness=pw" \
  --data-urlencode "safesearch=moderate"
```

**Endpoint**: `GET https://api.search.brave.com/res/v1/web/search`
**Auth**: `X-Subscription-Token: <API_KEY>` header

## Core Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | **required** | Search query (max 400 chars / 50 words) |
| `count` | int | 20 | Results per page (1–20) |
| `freshness` | string | — | `pd` past day · `pw` past week · `pm` past month · `py` past year · `YYYY-MM-DDtoYYYY-MM-DD` custom range |
| `safesearch` | string | `moderate` | `off` / `moderate` / `strict` |
| `goggles` | string | — | Custom re-ranking rules — URL to hosted `.goggle` or inline rules (e.g. `$discard\n$site=docs.python.org`) |
| `offset` | int | 0 | Pagination offset (0–9) |
| `extra_snippets` | bool | false | Return up to 5 additional text excerpts per result |

## Response Fields

Each item in `web.results[]`:

| Field | Description |
|-------|-------------|
| `title` | Page title |
| `url` | Page URL |
| `description` | Snippet text |
| `age` | Human-readable age, e.g. `"2 days ago"` |
| `thumbnail.src` | Thumbnail image URL (if available) |
| `extra_snippets` | Additional excerpts array (requires `extra_snippets=true`) |

Top-level response also contains `news.results[]`, `videos.results[]`, and `discussions.results[]` when available.

## Goggles (Custom Ranking)

Unique to Brave — re-rank results by boosting or discarding domains.

```bash
# Allow-list: only return results from trusted docs
--data-urlencode 'goggles=$discard\n$site=docs.python.org\n$site=developer.mozilla.org'

# Block-list: suppress low-quality sources
--data-urlencode 'goggles=$discard,site=pinterest.com\n$discard,site=quora.com'
```

Syntax: `$boost=N` / `$downrank=N` (1–10), `$discard`, `$site=domain.com`. Combine rules with `\n`.
