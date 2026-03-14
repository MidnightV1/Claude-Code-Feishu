---
name: bilibili-cli
description: Bilibili video platform CLI. TRIGGER when user mentions B站/Bilibili/哔哩哔哩, asks to search videos, check trending/hot/rank, look up UP主, view video details/subtitles/comments, manage watch history/favorites/watch-later, interact with videos (like/coin/triple), post/delete dynamics, or download audio for transcription.
---

# bilibili-cli

Bilibili platform CLI — search, browse, interact, and download audio.

## Tool

```
export PATH="$HOME/.local/bin:$PATH"
bili <command> [args]
```

Always use `--yaml` flag for structured output (agent-friendly).

## Commands

### Search

```bash
bili search "keyword"                        # Search users (default)
bili search "keyword" --type video            # Search videos
bili search "keyword" --type video -n 10      # Limit results
bili search "keyword" --page 2 --yaml         # Paginate, YAML output
```

### Video Details

```bash
bili video BV1xxx --yaml                      # Basic video info
bili video BV1xxx -s                           # With subtitles
bili video BV1xxx -st                          # Subtitles with timeline
bili video BV1xxx --subtitle-format srt        # SRT format subtitles
bili video BV1xxx -c                           # With comments
bili video BV1xxx --ai                         # With AI summary
bili video BV1xxx -r                           # With related videos
bili video "https://bilibili.com/video/BV1xxx" # URL also works
```

### Trending & Rankings

```bash
bili hot --yaml                               # Hot videos (default 20)
bili hot -n 10 -p 2                            # Page 2, 10 results
bili rank --yaml                               # Top ranking (3-day)
bili rank --day 7 -n 10                        # 7-day ranking, top 10
```

### User / UP主

```bash
bili user 12345 --yaml                         # By UID
bili user "username" --yaml                    # By name (searches first match)
bili user-videos 12345 -n 20 --yaml            # UP's video list
```

### Browse (requires login)

```bash
bili feed --yaml                               # Dynamic timeline
bili feed --offset <cursor>                    # Paginate with cursor
bili favorites --yaml                          # List all favorites
bili favorites <fav_id> -p 2 --yaml            # Videos in a favorite folder
bili history -n 50 --yaml                      # Watch history
bili watch-later --yaml                        # Watch later list
bili my-dynamics --yaml                        # My posted dynamics
bili following --yaml                          # Following list
```

### Interact (requires login)

```bash
bili like BV1xxx                               # Like
bili coin BV1xxx                               # Coin
bili triple BV1xxx                             # Like + Coin + Favorite
bili dynamic-post "text content"               # Post dynamic
bili dynamic-post --from-file ./post.txt       # Post from file
bili dynamic-delete <dynamic_id>               # Delete dynamic
bili unfollow <uid>                            # Unfollow user
```

### Audio Download

```bash
bili audio BV1xxx                              # Download + split (25s segments, WAV 16kHz mono)
bili audio BV1xxx --segment 60                 # 60s segments
bili audio BV1xxx --no-split                   # Full audio (m4a)
bili audio BV1xxx -o ~/data/                   # Custom output dir
```

Output: `/tmp/bilibili-cli/{title}/` by default. WAV segments are ASR-ready.

### Account

```bash
bili login                                     # QR code login (interactive, terminal only)
bili status                                    # Check login status
bili whoami --yaml                             # Current user details
bili logout                                    # Clear saved credentials
```

## Auth

- `bili login` generates a QR code in terminal for Bilibili app scan
- Credentials are saved locally after first login
- Browse/interact commands require login; search/video/hot/rank work without login
- Login is interactive (QR scan) — only works in SSH CLI, not in automated pipelines

## Output

- Default: human-readable table/text
- `--json`: JSON output
- `--yaml`: YAML output (recommended for agent consumption)
- Always prefer `--yaml` when parsing results programmatically

## Behavior Notes

- `BV_OR_URL` accepts both BV numbers (`BV1xxx`) and full Bilibili URLs
- `UID_OR_NAME` accepts numeric UID or username string (auto-searches first match)
- Pagination: most list commands support `-p/--page` and `-n/--max`
- `feed` uses cursor-based pagination (`--offset`), not page numbers
- `audio` requires `ffmpeg` installed for audio extraction and splitting
- Subtitle availability depends on the video (uploader or AI-generated)
- `--ai` summary is Bilibili's built-in AI summary, not always available
