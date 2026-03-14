---
name: xiaohongshu-cli
description: Xiaohongshu / RED / 小红书 operations — search notes, read content, post images, browse trending, manage comments, follow users, view notifications. TRIGGER when user mentions 小红书/XHS/RED, wants to search/read/post on Xiaohongshu, browse trending content, or manage their XHS account. DO NOT TRIGGER for general social media questions — answer those directly.
---

# xiaohongshu-cli

CLI for Xiaohongshu (小红书/RED) via reverse-engineered API. Search, read, post, interact, and manage account.

## Tool

```
export PATH="$HOME/.local/bin:$PATH"
xhs <command> [args]
```

All commands support `--json` and `--yaml` output flags. Use `--json` for structured parsing.

## Commands

### Search & Discovery

```bash
# Search notes
xhs search "咖啡推荐" --sort general --type all --page 1
# Sort: general | popular | latest
# Type: all | video | image

# Search users
xhs search-user "设计师"

# Search topics/hashtags
xhs topics "露营"

# Browse recommendation feed
xhs feed

# Browse trending by category
xhs hot --category food
# Categories: fashion | food | cosmetics | movie | career | love | home | gaming | travel | fitness
```

### Read & Interact

```bash
# Read a note (by ID, URL, or short index from search results)
xhs read <id_or_url>
xhs read <id_or_url> --xsec-token <token>

# View comments (supports pagination)
xhs comments <id_or_url>
xhs comments <id_or_url> --all          # fetch ALL comments

# View replies to a comment
xhs sub-comments <note_id> <comment_id>

# Like / unlike
xhs like <id_or_url>
xhs like <id_or_url> --undo

# Favorite (bookmark) / unfavorite
xhs favorite <id_or_url>
xhs unfavorite <id_or_url>

# Post a comment
xhs comment <id_or_url> -c "好棒！"

# Reply to a comment
xhs reply <id_or_url> --comment-id <cid> -c "谢谢！"

# Delete a comment
xhs delete-comment <note_id> <comment_id> -y
```

### Publish & Manage

```bash
# Post an image note (images required)
xhs post --title "标题" --body "正文内容" --images /path/to/img1.jpg --images /path/to/img2.jpg
xhs post --title "标题" --body "内容" --images img.jpg --topic "咖啡" --private

# List your own notes
xhs my-notes --page 0

# Delete a note (experimental — web endpoint unstable)
xhs delete <id_or_url> -y
```

### User & Social

```bash
# View user profile
xhs user <user_id>

# View user's posts
xhs user-posts <user_id> --cursor <cursor>

# List favorites (own or other user)
xhs favorites
xhs favorites <user_id>

# Follow / unfollow
xhs follow <user_id>
xhs unfollow <user_id>
```

### Account & Notifications

```bash
# Login status
xhs status
xhs whoami                  # detailed profile (level, fans, likes)

# Notifications
xhs notifications --type mentions    # 评论和@
xhs notifications --type likes       # 赞和收藏
xhs notifications --type connections # 新增关注
xhs unread                           # unread counts
```

## Auth

Authentication uses browser cookies, stored locally after first login.

```bash
# Auto-detect cookies from installed browsers (preferred)
xhs login

# Specify browser
xhs login --cookie-source chrome

# QR code login (scan with Xiaohongshu app)
xhs login --qrcode

# Logout
xhs logout
```

Global option `--cookie-source TEXT` can override browser selection per-command.

**Check status**: `xhs status` to verify login before operations.

## Output

- Default: human-readable formatted text
- `--json`: structured JSON (use for parsing)
- `--yaml`: YAML format
- Search results return short indices that can be passed to `xhs read`
- Paginated commands use `--cursor` or `--page` for navigation

## Behavior Notes

- `PATH` must include `$HOME/.local/bin` — always export before calling
- `xhs post` requires at least one `--images` flag; video posting is not supported
- `xhs delete` is experimental — the public web endpoint is unstable
- `--xsec-token` for `read` is a security token; the CLI caches tokens from search results automatically
- Destructive actions (`delete`, `delete-comment`) require `-y` to skip confirmation
- Rate limits are enforced server-side; no built-in retry — handle failures at caller level
