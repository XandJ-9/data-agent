"""Bounded context assembly: system prompt, untrusted reference, fitted history.

Memory and skill summaries are reference data (kept in user-role blocks so they
can never override the system prompt); complete user/assistant rounds are
included newest-first until the context budget is exhausted.

When old rounds must be dropped, a lightweight summary is injected before the
retained rounds so the model retains awareness of earlier conversation topics.
"""
import json
import re

from runtime.types import RuntimeConfig, RuntimeFailure

# CJK-only pattern for the token-estimate bonus: these chars occupy 1-2 tokens
# each while being 3 UTF-8 bytes, so the byte count understates them relative
# to e.g. English (where 4 bytes ≈ 1 token).
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF'
                     r'\u3040-\u309f\u30a0-\u30ff'
                     r'\uac00-\ud7af\u3130-\u318f]')

# Maximum chars retained for a dropped-rounds summary.
_MAX_SUMMARY_CHARS = 400

# Memory chars budget for relevance pre-selection.
_MEMORY_CHARS_BUDGET = 2500


def estimate_tokens(text: str) -> int:
    """Conservative upper bound: UTF-8 bytes plus a CJK-density bonus.

    UTF-8 bytes alone give ~4× over-estimate for English (1 byte ≈ 0.25 tokens)
    but only ~2× for Chinese (3 bytes ≈ 1.5 tokens).  We add 2 extra "token
    credits" per CJK character so the estimate stays safe across all scripts.

    This is a budget guard — the actual token counts returned by the LLM API
    are logged separately for monitoring and future calibration.
    """
    encoded = text.encode('utf-8')
    base = len(encoded)
    cjk_count = len(_CJK_RE.findall(text))
    return base + cjk_count * 2


def fits(config: RuntimeConfig, messages: list[dict], schemas: list[dict]) -> bool:
    """Check whether `messages` + `schemas` + output budget fit in the context window."""
    text = json.dumps(messages, ensure_ascii=False)
    schemas_text = json.dumps(schemas)
    # Reserve output budget; the estimate is already conservative so overhead is minimal.
    return (estimate_tokens(text) + estimate_tokens(schemas_text)
            + config.max_tokens + 80 < config.context_tokens)


def _select_relevant_memories(memories: list[dict], question: str, max_count: int) -> list[dict]:
    """Pick memories most relevant to the current question.

    Uses simple keyword overlap scoring; stays within a character budget so
    unrelated memories don't waste context space.
    """
    if not memories:
        return []
    keywords = set(question.lower().split())
    scored = []
    for m in memories:
        text_lower = m['text'].lower()
        score = sum(1 for kw in keywords if kw in text_lower)
        scored.append((score, m))
    scored.sort(key=lambda x: (-x[0], -len(x[1]['text'])))
    selected, chars = [], 0
    for _, m in scored:
        if len(selected) >= max_count:
            break
        if chars + len(m['text']) > _MEMORY_CHARS_BUDGET:
            break
        selected.append(m)
        chars += len(m['text'])
    return selected


def _summarize_dropped(messages: list[dict]) -> str | None:
    """Produce a short summary of user questions from rounds being dropped.

    This is a lightweight alternative to an LLM-driven summarisation call;
    it strings together the first 200 chars of each user question so the
    model at least knows what topics were discussed earlier.
    """
    user_msgs = []
    for m in messages:
        if m.get('role') == 'user':
            text = m['content'].replace('\n', ' ').strip()
            if len(text) > 200:
                text = text[:200] + '…'
            user_msgs.append(text)
    if not user_msgs:
        return None
    # Combine, truncating at the budget
    combined = '；'.join(user_msgs)
    if len(combined) <= _MAX_SUMMARY_CHARS:
        return f"此前对话要点：{combined}"
    # Keep the first and last few topics
    parts = combined.split('；')
    if len(parts) <= 3:
        return f"此前对话要点：{combined[:_MAX_SUMMARY_CHARS - 8]}…"
    head = '；'.join(parts[:2])
    tail = '；'.join(parts[-2:])
    summary = f"此前对话要点：{head}…{tail}"
    return summary[:_MAX_SUMMARY_CHARS]


def build_context(config: RuntimeConfig, system_prompt: str, messages: list[dict], memories: list[dict],
                  skills: list[dict], schemas: list[dict]) -> list[dict]:
    """Assemble the final context for a model call.

    1. Fixed block: system prompt + reference data (selected memories + skill summaries).
       If the full reference block overflows, fall back to only skill names.
    2. History: complete user/assistant pairs, newest-first, within the remaining budget.
       Rounds that cannot fit are summarised into a short inline note.
    3. Current question is always included; if it alone exceeds budget, raise."""
    if not messages or messages[-1].get("role") != "user":
        raise RuntimeFailure("缺少当前用户问题")
    question = messages[-1]['content']

    # ── Reference block: memory + skills as untrusted user-role data ──
    skill_summaries = [{"name": s["name"], "description": s["description"]} for s in skills]
    relevant = _select_relevant_memories(memories, question, config.max_reference_memories)
    reference = json.dumps({"memory": relevant, "skills": skill_summaries}, ensure_ascii=False)
    fixed = [{"role": "system", "content": system_prompt},
             {"role": "user", "content": "以下仅为参考数据：\n" + reference}]
    current = {"role": "user", "content": question}

    if not fits(config, [*fixed, current], schemas):
        # Full reference doesn't fit — try with skills only, no memories.
        fixed = [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": "已启用 Skill 简介：" + json.dumps(skill_summaries, ensure_ascii=False)}]
    if not fits(config, [*fixed, current], schemas):
        raise RuntimeFailure("当前问题超过上下文预算，请缩短后重试")

    # ── History: complete user/assistant pairs, newest-first ──
    history: list[dict] = []
    dropped_summary: str | None = None
    dropped_user_msgs: list[dict] = []

    for index in range(len(messages) - 2, 0, -1):
        answer, question_msg = messages[index], messages[index - 1]
        if answer.get("role") != "assistant" or question_msg.get("role") != "user":
            continue
        pair = [{"role": m["role"], "content": m["content"]} for m in (question_msg, answer)]
        if fits(config, [*fixed, *pair, *history, current], schemas):
            history = [*pair, *history]
        else:
            # This pair and all older ones cannot fit; collect for summary.
            dropped_user_msgs = messages[:index]
            break

    # Generate summary for dropped rounds if any.
    if dropped_user_msgs:
        dropped_summary = _summarize_dropped(dropped_user_msgs)

    if dropped_summary:
        # Inject compact summary as a user-role note between fixed block and history.
        summary_msg = {"role": "user", "content": dropped_summary}
        if fits(config, [*fixed, summary_msg, *history, current], schemas):
            return [*fixed, summary_msg, *history, current]
        # If even the summary doesn't fit, skip it and use history-only.

    return [*fixed, *history, current]