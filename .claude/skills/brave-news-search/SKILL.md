---
name: brave-news-search
description: USE FOR news search when English news sources or structured article metadata (age, thumbnail) are needed. Returns news articles with title, URL, description, age, thumbnail. Supports freshness and date range filtering, SafeSearch filter and Goggles for custom ranking. For Chinese news or general news search, prefer gemini. Use Brave news for English-language authoritative news sources.
---

# brave-news-search

Brave News Search API — English-language authoritative news sources with structured article metadata.

**When to use**: English news sources, breaking news monitoring, or when you need structured metadata (age, thumbnail). For Chinese news or general web search, prefer `gemini`.

**Requires**: `BRAVE_SEARCH_API_KEY` env var.

---

## Quick Start

```bash
curl -s "https://api.search.brave.com/res/v1/news/search?q=space+exploration&count=10&freshness=pd" \
  -H "Accept: application/json" \
  -H "X-Subscription-Token: ${BRAVE_SEARCH_API_KEY}"
```

---

## Core Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `q` | — | Search query (required, max 400 chars / 50 words) |
| `count` | 20 | Number of results (1–50) |
| `freshness` | — | Time filter: `pd` past day, `pw` past week, `pm` past month, `py` past year, or `YYYY-MM-DDtoYYYY-MM-DD` |
| `safesearch` | `strict` | Adult content filter: `off` / `moderate` / `strict` |

---

## Response Format

Each item in `results[]` contains:

| Field | Description |
|-------|-------------|
| `title` | Article headline |
| `url` | Source URL |
| `description` | Article summary/excerpt |
| `age` | Human-readable age (e.g. "2 hours ago") |
| `thumbnail.src` | Article image URL |

Full response shape: `{ type, query: { original }, results: [...] }`
