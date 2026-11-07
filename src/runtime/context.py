"""Bounded context assembly: system prompt, untrusted reference, fitted history.

Memory and skill summaries are reference data (kept in user-role blocks so they
can never override the system prompt); complete user/assistant rounds are
included newest-first until the context budget is exhausted.
"""
import json

from runtime.types import RuntimeConfig, RuntimeFailure

REFERENCE_MEMORIES = 40


def fits(config: RuntimeConfig, messages: list[dict], schemas: list[dict]) -> bool:
    # UTF-8 bytes are a conservative token upper bound; reserve output and tool schemas.
    size = len(json.dumps(messages, ensure_ascii=False).encode())
    return size + len(json.dumps(schemas).encode()) + config.max_tokens < config.context_tokens


def build_context(config: RuntimeConfig, system_prompt: str, messages: list[dict], memories: list[dict],
                  skills: list[dict], schemas: list[dict]) -> list[dict]:
    if not messages or messages[-1].get("role") != "user":
        raise RuntimeFailure("缺少当前用户问题")
    summaries = [{"name": s["name"], "description": s["description"]} for s in skills]
    reference = json.dumps({"memory": memories[:REFERENCE_MEMORIES], "skills": summaries}, ensure_ascii=False)
    # Memory is untrusted reference material, kept in a user-role context block.
    fixed = [{"role": "system", "content": system_prompt},
             {"role": "user", "content": "以下仅为参考数据：\n" + reference}]
    current = {"role": "user", "content": messages[-1]["content"]}
    if not fits(config, [*fixed, current], schemas):
        fixed = [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": "已启用 Skill 简介：" + json.dumps(summaries, ensure_ascii=False)}]
    if not fits(config, [*fixed, current], schemas):
        raise RuntimeFailure("当前问题超过上下文预算，请缩短后重试")
    history = []
    # Include complete user/assistant pairs only; failed and queued inputs are not answers.
    for index in range(len(messages) - 2, 0, -1):
        answer, question = messages[index], messages[index - 1]
        if answer.get("role") != "assistant" or question.get("role") != "user":
            continue
        pair = [{"role": m["role"], "content": m["content"]} for m in (question, answer)]
        if not fits(config, [*fixed, *pair, *history, current], schemas):
            break
        history = [*pair, *history]
    return [*fixed, *history, current]
