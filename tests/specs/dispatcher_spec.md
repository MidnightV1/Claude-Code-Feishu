# Test Spec: agent.platforms.feishu.dispatcher

## Purpose

`dispatcher.py` is the **only egress point** for all outbound Feishu messages.
Bugs here mean silent drops, duplicate messages, secret leaks to users, or
malformed card JSON that Feishu rejects. The pure helper functions (`_parse_card_directive`,
`_contains_secret`, `_build_card_json`, `_chunk_text`, `build_button_group`) are fully
testable without network access. The async send methods require a mock Lark client.

## Module Location

`agent/platforms/feishu/dispatcher.py`

## Constants Under Test

| Constant | Value | Meaning |
|----------|-------|---------|
| `MAX_MSG_LEN` | 4000 | Max chars per card markdown chunk |
| `MAX_RETRIES` | 3 | Retry attempts for transient network failures |

## Functions Under Test

### _parse_card_directive(text) -> tuple[str, str | None, str | None]

Parses `{{card:header=…,color=…}}` prefix directive from outbound text.
Returns `(remaining_text, header, color)`. If no directive: `(text, None, None)`.

| ID | Scenario | Input | Expected | Priority |
|----|----------|-------|----------|----------|
| D01 | Header + color | `"{{card:header=完成,color=green}}\n内容"` | `("内容", "完成", "green")` | P0 |
| D02 | No directive | `"普通文本"` | `("普通文本", None, None)` | P0 |
| D03 | Header only | `"{{card:header=任务完成}}\n正文"` | `("正文", "任务完成", None)` | P0 |
| D04 | Color only | `"{{card:color=red}}\n警告"` | `("警告", None, "red")` | P0 |
| D05 | Leading whitespace before directive | `"   {{card:header=标题,color=blue}}\n内容"` | parsed correctly | P1 |
| D06 | Unknown extra params ignored | `"{{card:header=标题,color=green,unknown=xxx}}\n内容"` | header and color extracted, unknown dropped | P1 |
| D07 | Empty header value | `"{{card:header=,color=blue}}\n内容"` | `header=""` (empty string, falsy) | P1 |
| D08 | Empty color value | `"{{card:header=标题,color=}}\n内容"` | `color=""` | P1 |
| D09 | Case-insensitive `CARD` keyword | `"{{CARD:header=标题,color=green}}\n内容"` | parsed correctly | P1 |
| D10 | Directive not at start — not matched | `"前缀 {{card:header=标题}}\n内容"` | `(original_text, None, None)` | P0 |
| D11 | Newline after directive consumed | `"{{card:header=标题,color=blue}}\n正文"` | remaining does not start with `\n` | P1 |
| D12 | Empty content after directive | `"{{card:header=标题,color=green}}\n"` | `remaining == ""` | P1 |
| D13 | Multiline content preserved after directive | `"{{card:header=报告,color=blue}}\n第一行\n第二行\n\n第三段"` | all body lines present in remaining | P0 |

### _contains_secret(text) -> str | None

Scans text for hardcoded secret patterns. Returns matched pattern prefix or `None`.
This is a **security gate** — false negatives mean secret leakage to users.

| ID | Pattern | Example | Expected |
|----|---------|---------|----------|
| S01 | Anthropic key | `sk-ant-api03-abcdefghijklmnopqrstuvwxyz…` | not None |
| S02 | OpenAI key | `sk-abcdefghijklmnopqrstuvwxyz12345` | not None |
| S03 | GitHub PAT | `ghp_abcdefghij1234567890ABCDE` | not None |
| S04 | GitHub OAuth | `gho_abcdefghijklmnopqrst` | not None |
| S05 | Slack bot token | `xoxb-1234567890x-abcdefghijklmn` | not None |
| S06 | Slack app token | `xoxa-1234567890x-abcdefghijklmn` | not None |
| S07 | Google API key | `AIzaSyAbcdefghijklmnopqrstuvwxyz123456` | not None |
| S08 | RSA private key header | `-----BEGIN RSA PRIVATE KEY-----` | not None |
| S09 | EC private key header | `-----BEGIN EC PRIVATE KEY-----` | not None |
| S10 | AWS access key | `AKIAIOSFODNN7EXAMPLE` | not None |
| S11 | Clean Chinese text | `"这是普通中文，没有密钥"` | `None` |
| S12 | Clean English text | `"Hello world"` | `None` |
| S13 | Secret embedded in longer text | `"配置: AKIA1234567890ABCDEF 泄漏了"` | not None |
| S14 | Too-short OpenAI prefix | `"sk-short"` (< 20 chars after sk-) | `None` |
| S15 | Too-short GitHub PAT | `"ghp_12345"` (< 10 chars after ghp_) | `None` |

### Dispatcher._build_card_json(text, *, header, color) -> str

Static method. Builds Feishu interactive card JSON 2.0 string. Pure, no network.

| ID | Scenario | Inputs | Expected JSON structure | Priority |
|----|----------|--------|------------------------|----------|
| C01 | Text only | `text="hello"` | `schema=="2.0"`, `body.elements[0].tag=="markdown"`, no `header` key | P0 |
| C02 | Header + color | `text="内容", header="标题", color="green"` | `header.title.content=="标题"`, `header.template=="green"` | P0 |
| C03 | Header only | `header="只有标题"` | `header.title` present, no `template` key | P0 |
| C04 | Color only | `color="red"` | `header.template=="red"`, no `title` key | P0 |
| C05 | All 13 CARD_COLORS | loop over `Dispatcher.CARD_COLORS` | each produces valid JSON with correct template | P1 |
| C06 | Chinese content | `text="你好世界"` | Chinese chars not escaped (ensure_ascii=False) | P0 |
| C07 | Empty text | `text=""` | valid JSON, content `""` | P1 |
| C08 | Returns valid JSON string | any input | `json.loads(result)` succeeds | P0 |
| C09 | Schema is string `"2.0"` | any input | `isinstance(data["schema"], str)` | P0 |

### Dispatcher._chunk_text(text) -> list[str]

Static method. Splits text at paragraph boundaries (`\n\n`) respecting `MAX_MSG_LEN=4000`.
Falls back to hard-split for paragraphs longer than the limit.

| ID | Scenario | Input | Expected | Priority |
|----|----------|-------|----------|----------|
| CH01 | Short text | `"短文本"` | `["短文本"]` (single chunk) | P0 |
| CH02 | Exactly MAX_MSG_LEN | `"a" * 4000` | single chunk | P0 |
| CH03 | MAX_MSG_LEN + 1 | `"a" * 4001` | 2 chunks: `["a"*4000, "a"]` | P0 |
| CH04 | Multiple short paragraphs fit in one chunk | 3 paragraphs totaling < 4000 chars | 1 chunk | P0 |
| CH05 | Two paragraphs each ~2100 chars | `para1 + "\n\n" + para2` | 2 chunks | P0 |
| CH06 | Very long single paragraph (3× MAX) | `"X" * 12000` | 3 chunks, each ≤ MAX_MSG_LEN | P0 |
| CH07 | Empty text | `""` | `[""]` | P1 |
| CH08 | No empty chunks | varied paragraphs | all chunks non-empty | P1 |
| CH09 | All chunks within MAX_MSG_LEN | random paragraphs 50–3000 chars | every `len(chunk) <= 4000` | P0 |
| CH10 | Real-world Chinese with table + code | mixed markdown ~500 chars | ≥1 chunk, all ≤ MAX_MSG_LEN | P1 |
| CH11 | Whitespace-only paragraphs stripped | `"内容\n\n   \n\n另一段"` | no chunk is whitespace-only | P1 |
| CH12 | Three 2100-char paragraphs | `"\n\n".join([para]*3)` | ≥2 chunks, each ≤ MAX_MSG_LEN | P0 |

### Dispatcher.build_button_group(buttons, layout) -> dict | list

Static method. Builds `column_set` element(s) for interactive card buttons.

| ID | Scenario | Buttons | Layout | Expected |
|----|----------|---------|--------|----------|
| BG01 | 2 buttons bisected | 2 buttons | `"bisected"` | single `column_set` with 2 columns |
| BG02 | 3 buttons trisection | 3 buttons | `"trisection"` | single `column_set` with 3 columns |
| BG03 | 4 buttons bisected — wraps | 4 buttons | `"bisected"` | list of 2 `column_set` dicts |
| BG04 | Default layout | 2 buttons, no layout | (default bisected) | single `column_set` |
| BG05 | Button type forwarded | `type="danger"` | any | element has `type=="danger"` |
| BG06 | Button value forwarded | `value={"action":"confirm"}` | any | element has matching `value` |

### Dispatcher.build_confirm_card(text, confirm_value, cancel_value, ...) -> str

Static method. Builds a red-by-default confirmation card with two buttons.

| ID | Scenario | Expected |
|----|----------|----------|
| CF01 | Default — no header | valid JSON, no `header.title` |
| CF02 | With header | `header.title.content` matches |
| CF03 | Default color red | `header.template == "red"` |
| CF04 | Custom color | `header.template` matches custom color |
| CF05 | Confirm button type danger | confirm button `type=="danger"` |
| CF06 | Cancel button type default | cancel button `type=="default"` |
| CF07 | Custom confirm/cancel text | button text matches |
| CF08 | confirm_value forwarded | confirm button `value` matches input |
| CF09 | Valid JSON string | `json.loads(result)` succeeds |

## Async Methods (require mock Lark client)

The async send methods (`send_text`, `send_to_user`, `send_card_return_id`, etc.)
depend on `lark_oapi` — mocked in unit tests via `pytest-asyncio` + `AsyncMock`.

Key behaviours to cover:

| ID | Scenario | Expected |
|----|----------|----------|
| A01 | `send_text` empty text | returns `None` immediately (no send) |
| A02 | `send_text` with secret | returns `None`, logs error, no send |
| A03 | `send_text` ≤ MAX_MSG_LEN | calls `_send_card` once |
| A04 | `send_text` > MAX_MSG_LEN | calls `_send_chunked` (multiple cards) |
| A05 | `send_text` with card directive | directive parsed and forwarded to `_build_card_json` |
| A06 | `_with_retry` success first attempt | returns result, no sleep |
| A07 | `_with_retry` transient failure then success | retries, returns result |
| A08 | `_with_retry` 3 failures | returns `None` |
| A09 | `_with_retry` ValueError | returns `None` immediately (no retry) |
| A10 | `send_card_return_id` 230011 error | falls back to non-reply send |
| A11 | `_ensure_client` before `start()` | raises `RuntimeError` |

## Golden Files

- `golden/rendering/card_basic.json` — `_build_card_json` output for text-only card
- `golden/rendering/card_with_header.json` — card with header + green color
- `golden/rendering/card_confirm.json` — `build_confirm_card` output

## Test File

`tests/unit/test_dispatcher.py` (already partially implemented)

## Risk: LLM Faking Results

`_build_card_json`, `_chunk_text`, `_parse_card_directive`, and `_contains_secret` are
pure functions — deterministic. Golden files and structural assertions are computed by
pytest, not by the LLM. For async tests, mock call counts are recorded by `AsyncMock`
and asserted numerically — the LLM cannot fabricate call counts.
