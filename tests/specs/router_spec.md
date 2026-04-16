# Test Spec: agent.llm.router

## Purpose
LLMRouter manages session persistence, history compression, and degradation recovery.
Bugs here cause silent context loss, infinite retry loops, or session corruption.

## Functions Under Test

### Session CRUD
| ID | Scenario | Method | Expected | Priority |
|----|----------|--------|----------|----------|
| R01 | Get missing session | `get_session_id('missing')` | None | P0 |
| R02 | Get existing session | after set, `get_session_id(key)` | session_id string | P0 |
| R03 | Set LLM config | `set_session_llm(key, config)` | creates entry if absent | P0 |
| R04 | Get LLM config | after set, `get_session_llm(key)` | returns config dict | P0 |
| R05 | Clear session | `clear_session(key)` | removes session_id, preserves history + llm_config | P0 |
| R06 | Clear missing key | `clear_session('missing')` | no crash | P1 |

### History Management
| ID | Scenario | Method | Expected | Priority |
|----|----------|--------|----------|----------|
| R10 | Append history | `_append_history(key, user, asst)` | 2 messages added with timestamps | P0 |
| R11 | Truncation | user_msg > 4000 chars | truncated with "..." | P0 |
| R12 | Round limit | append > 15 rounds | oldest rounds dropped, last 15 kept | P0 |
| R13 | Remove last round | `remove_last_round(key)` | removes last user+assistant pair | P1 |
| R14 | Remove from empty | `remove_last_round('missing')` | no crash | P1 |

### Transient Detection (static)
| ID | Scenario | Input | Expected | Priority |
|----|----------|-------|----------|----------|
| R20 | ld.so crash | `LLMResult(text='ld.so...', is_error=True)` | True | P0 |
| R21 | dl-open.c crash | `LLMResult(text='dl-open.c...', is_error=True)` | True | P0 |
| R22 | Empty error | `LLMResult(text='', is_error=True)` | True | P0 |
| R23 | Normal error | `LLMResult(text='API rate limit', is_error=True)` | False | P0 |
| R24 | Not error | `LLMResult(text='ld.so', is_error=False)` | False | P0 |

### Context Recovery (async)
| ID | Scenario | Input | Expected | Priority |
|----|----------|-------|----------|----------|
| R30 | No history | empty sessions | None | P0 |
| R31 | Short history | < SUMMARY_THRESHOLD rounds | raw format with preamble | P0 |
| R32 | Long history | > SUMMARY_THRESHOLD rounds | compression called, mixed output | P1 |
| R33 | Compression fails | mock returns None | fallback to raw for older + raw for recent | P1 |

### Save Result
| ID | Scenario | Input | Expected | Priority |
|----|----------|-------|----------|----------|
| R40 | Success | valid LLMResult | session_id + history stored | P0 |
| R41 | Error result | is_error=True | nothing saved | P0 |
| R42 | No session key | empty key | nothing saved | P1 |

## Risk: Session Corruption
History management has complex state transitions. Golden files for context construction
(history compress output, recovery prompt) catch regressions in the compression pipeline.
