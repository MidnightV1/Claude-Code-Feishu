---
name: twitter-cli
description: Twitter/X CLI for reading, searching, and posting tweets (推特/Twitter/X/tweet/timeline/发推/推文/书签). TRIGGER when user asks to search Twitter (搜推特/Twitter上找), read timeline/feed (推特首页/时间线), view tweets or profiles (看推文/看用户), post/reply/quote tweets (发推/回复推文/转推), manage bookmarks (推特书签), or check followers/following. Also triggers for any Twitter data retrieval task (e.g. briefing pipelines). DO NOT TRIGGER for general social media discussion — only when actual Twitter operations are needed.
---

# twitter-cli

Twitter/X CLI tool. Read, search, post, and interact with Twitter — cookie-based auth, no API keys needed.

**Installed via**: `uv tool install twitter-cli` (binary at `~/.local/bin/twitter`)

## Tool

```
export PATH="$HOME/.local/bin:$PATH"
twitter <command> [args]
```

## Commands

### Search

```bash
# Basic search
twitter search "AI agents" -n 20 --json

# Advanced filters
twitter search "python" --from elonmusk --json
twitter search "AI" --lang en --since 2026-01-01 --until 2026-03-01
twitter search "rust" --has links --min-likes 100
twitter search --from bbc --exclude retweets -n 50

# Search types: top (default), latest, photos, videos
twitter search "breaking news" -t latest --json
```

Options: `--from`, `--to`, `--lang`, `--since YYYY-MM-DD`, `--until YYYY-MM-DD`, `--has [links|images|videos|media]`, `--exclude [retweets|replies|links]`, `--min-likes N`, `--min-retweets N`, `-n/--max N`, `--filter` (score-based).

### Feed (Timeline)

```bash
twitter feed -n 20 --json                  # algorithmic (for-you)
twitter feed -t following -n 30 --json     # chronological
twitter feed -o timeline.json              # save to file
twitter feed -i timeline.json --filter     # reload + filter
```

### View Tweet & Replies

```bash
twitter tweet 1234567890 --json            # by ID
twitter tweet "https://x.com/user/status/1234567890" --json  # by URL
twitter tweet 1234567890 -n 50 --json      # with up to 50 replies
```

### Post / Reply / Quote / Retweet

```bash
twitter post "Hello world!" --json
twitter post "With image" -i photo.jpg --json
twitter post "Gallery" -i a.png -i b.png -i c.jpg   # up to 4 images

twitter reply 1234567890 "Great point!" --json
twitter quote 1234567890 "Adding context" --json
twitter retweet 1234567890 --json
```

### User Profiles & Posts

```bash
twitter user elonmusk --json               # profile info
twitter user-posts elonmusk -n 20 --json   # user's tweets
twitter whoami --json                      # current auth user
```

### Social Graph

```bash
twitter followers elonmusk -n 100 --json
twitter following elonmusk -n 100 --json
twitter follow username --json
twitter unfollow username --json
```

### Engagement

```bash
twitter like 1234567890 --json
twitter unlike 1234567890 --json
twitter bookmark 1234567890 --json
twitter unbookmark 1234567890 --json
```

### Bookmarks & Likes

```bash
twitter bookmarks -n 50 --json
twitter likes myhandle -n 50 --json        # own likes only (others' likes are private since June 2024)
```

### Articles

```bash
twitter article 1234567890 --markdown      # render as markdown
twitter article 1234567890 -o article.md   # save to file
twitter article 1234567890 --json
```

### Delete

```bash
twitter delete 1234567890 --json
```

### Diagnostics

```bash
twitter status                             # check auth
twitter whoami --json                      # current user info
twitter doctor                             # full diagnostics (cookie extraction, auth)
```

## Auth

Cookie-based authentication. The CLI extracts cookies from the local browser automatically (no API keys or OAuth needed).

- Run `twitter doctor` to diagnose auth issues
- Run `twitter status` to check if authenticated
- Cookies are extracted from the system browser — user must be logged into twitter.com/x.com

## Output

| Flag | Format | Use case |
|------|--------|----------|
| (none) | Table (human-readable) | Interactive display |
| `--json` | JSON | Agent parsing (preferred) |
| `--yaml` | YAML | Alternative structured output |
| `-c/--compact` | Minimal fields | LLM-friendly, reduced tokens |
| `--full-text` | Full tweet text in table | Avoid truncation in table mode |

For agent usage, always use `--json` for structured parsing.

## Behavior Notes

- Always prepend `export PATH="$HOME/.local/bin:$PATH"` before calling `twitter`
- Use `--json` for all programmatic access — parse the JSON output
- `-n/--max` controls result count; omitting it uses the CLI default
- `--filter` enables score-based relevance filtering (search, feed, bookmarks)
- `-o/--output` saves raw JSON to file for later reprocessing (`-i/--input`)
- Tweet IDs and full URLs are interchangeable for `tweet`, `article` commands
- `likes` only works for the authenticated user (Twitter made likes private June 2024)
- Write operations (`post`, `reply`, `like`, `follow`, etc.) require user confirmation before executing
- Images: up to 4 per post/reply/quote via `-i/--image`
