"""astrbot_plugin_think_prefix

用户消息以 <think> 开头时：

1. 删掉触发前缀本身，只把正文交给模型（可选，默认开启）；
2. 为本次 LLM 请求打开模型的「原生思考 / 推理」
   - OpenAI 兼容提供商（DeepSeek、智谱、Qwen/百炼、硅基流动、vLLM、OpenRouter …）
     -> 临时写入 provider_config["custom_extra_body"]
   - Anthropic / Claude（含 Kimi Coding Plan、小米/MiniMax Token Plan 等衍生适配器）
     -> 临时写入 provider.thinking_config 与 provider_config["anth_thinking_config"]
   - Google Gemini
     -> 临时写入 provider_config["gm_thinking_config"]
3. 在回复真正发出之前，用正则把思考内容（<think>…</think>、```thinking …```、
   未闭合的 <think> 片段等）从消息链里删掉，用户只会看到最终回答。

关于还原时机：AstrBot 的 OnLLMRequestEvent 每条用户消息只触发一次，之后的
「工具调用循环」都在同一个 ProviderRequest 上跑完，所以在 on_llm_response 里
还原配置会让多步工具调用的后续步骤丢失思考。因此补丁统一在整轮消息发送完成
（after_message_sent）后还原，另有超时兜底与插件卸载兜底。
"""

from __future__ import annotations

import copy
import json
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_think_prefix"
AUTHOR = "DSH"
DESC = "以 <think> 前缀触发模型思考，并自动用正则删减思考内容"
VERSION = "v1.0.0"

TRIGGER_FLAG = "_think_prefix_triggered"
REASONING_BUF = "_think_prefix_reasoning_buf"
REASONING_STEP = "_think_prefix_reasoning_step"

# 会被识别成「思考内容」的标签名
THINK_TAGS = "think|thinking|reasoning|thought|thoughts|analysis|scratchpad|reflection"

# 内置清理正则（统一使用 re.S | re.I 编译）
DEFAULT_STRIP_PATTERNS: tuple[str, ...] = (
    # <think> ... </think> / <thinking> ... </thinking> / <reasoning> ...
    rf"<\s*(?:{THINK_TAGS})\s*>.*?<\s*/\s*(?:{THINK_TAGS})\s*>",
    # ```thinking ... ``` 代码块形式
    rf"```[ \t]*(?:{THINK_TAGS})[ \t]*\r?\n.*?```",
    # 落单的结束标签
    rf"<\s*/\s*(?:{THINK_TAGS})\s*>",
)

# 未闭合思考片段的兜底正则：从开标签一路删到结尾
DEFAULT_UNCLOSED_PATTERN = rf"<\s*(?:{THINK_TAGS})\s*>.*\Z"

DEFAULT_OPENAI_EXTRA_BODY: dict[str, Any] = {"reasoning_effort": "high"}
DEFAULT_ANTHROPIC_THINKING: dict[str, Any] = {
    "type": "",
    "budget": 4096,
    "effort": "",
}
DEFAULT_GEMINI_THINKING: dict[str, Any] = {"budget": -1, "level": "HIGH"}

# 补丁最长存活时间（秒）。超过则视为上一轮异常退出，主动还原。
PATCH_TTL_SECONDS = 600


@register(PLUGIN_NAME, AUTHOR, DESC, VERSION)
class ThinkPrefixPlugin(Star):
    """<think> 前缀 -> 打开模型思考；回复中的思考内容正则清理。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict = config if isinstance(config, dict) else {}
        # id(provider) -> {"provider": prov, "kind": str, "stamp": float, "changes": [...]}
        self._patched: dict[int, dict[str, Any]] = {}
        self._trigger_cache: tuple[Any, re.Pattern[str]] | None = None
        self._strip_cache: tuple[Any, list[re.Pattern[str]]] | None = None
        self._unclosed_cache: tuple[Any, re.Pattern[str] | None] | None = None

    # ------------------------------------------------------------------ 配置

    def _conf(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, None)
        except Exception:
            return default
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            # 空字符串一律视为「未配置」，避免空的 trigger_prefix 命中所有消息
            return default
        return value

    def _conf_bool(self, key: str, default: bool) -> bool:
        value = self._conf(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
        return bool(value)

    def _conf_int(self, key: str, default: int) -> int:
        value = self._conf(key, default)
        if isinstance(value, bool):
            return int(value)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _conf_json(self, key: str, default: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = self.config.get(key, None)
        except Exception:
            raw = None
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return copy.deepcopy(default)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[think-prefix] 配置项 {key} 不是合法 JSON，已使用默认值。错误: {e}")
            return copy.deepcopy(default)
        if not isinstance(parsed, dict):
            logger.warning(f"[think-prefix] 配置项 {key} 必须是 JSON 对象，已使用默认值。")
            return copy.deepcopy(default)
        return parsed

    def _conf_list(self, key: str) -> list[str]:
        value = self._conf(key, [])
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return []

    def _is_debug(self) -> bool:
        return str(self._conf("log_level", "normal")).strip().lower() == "debug"

    # ------------------------------------------------------------- 正则编译

    def _trigger_pattern(self) -> re.Pattern[str]:
        prefix_value = self._conf("trigger_prefix", "<think>")
        regex_value = self._conf("trigger_regex", "")
        ignore_case = self._conf_bool("case_insensitive", True)
        key = (prefix_value, regex_value, ignore_case)
        if self._trigger_cache and self._trigger_cache[0] == key:
            return self._trigger_cache[1]

        flags = re.IGNORECASE if ignore_case else 0
        pattern: re.Pattern[str] | None = None
        raw_regex = str(regex_value or "").strip()
        if raw_regex:
            try:
                pattern = re.compile(raw_regex, flags)
            except re.error as e:
                logger.error(f"[think-prefix] trigger_regex 编译失败，回退到 trigger_prefix。错误: {e}")
        if pattern is None:
            prefix = str(prefix_value or "<think>")
            # 允许前置的唤醒前缀（默认 "/"）与空白
            pattern = re.compile(r"^[\s/]*" + re.escape(prefix) + r"[\s　]*", flags)

        self._trigger_cache = (key, pattern)
        return pattern

    def _strip_patterns(self) -> list[re.Pattern[str]]:
        extra = self._conf_list("extra_strip_patterns")
        key = tuple(extra)
        if self._strip_cache and self._strip_cache[0] == key:
            return self._strip_cache[1]

        compiled: list[re.Pattern[str]] = []
        # 内置规则需要跨行匹配（思考段通常包含换行）
        for raw in DEFAULT_STRIP_PATTERNS:
            try:
                compiled.append(re.compile(raw, re.DOTALL | re.IGNORECASE | re.MULTILINE))
            except re.error as e:
                logger.error(f"[think-prefix] 清理正则编译失败，已跳过: {raw!r} ({e})")
        # 用户自定义规则按「单行」语义编译：^ 和 $ 匹配每一行的行首行尾，
        # 需要跨行时用户可以自己写 (?s)
        for raw in extra:
            try:
                compiled.append(re.compile(raw, re.IGNORECASE | re.MULTILINE))
            except re.error as e:
                logger.error(f"[think-prefix] 自定义正则编译失败，已跳过: {raw!r} ({e})")
        self._strip_cache = (key, compiled)
        return compiled

    def _unclosed_pattern(self) -> re.Pattern[str] | None:
        enabled = self._conf_bool("strip_unclosed", True)
        key = (enabled,)
        if self._unclosed_cache and self._unclosed_cache[0] == key:
            return self._unclosed_cache[1]

        pattern: re.Pattern[str] | None = None
        if enabled:
            try:
                pattern = re.compile(DEFAULT_UNCLOSED_PATTERN, re.DOTALL | re.IGNORECASE)
            except re.error as e:  # pragma: no cover - 内置正则不会失败
                logger.error(f"[think-prefix] 未闭合正则编译失败: {e}")
        self._unclosed_cache = (key, pattern)
        return pattern

    # --------------------------------------------------------------- 文本清理

    def _clean_text(self, text: str) -> tuple[str, bool]:
        """返回 (清理后的文本, 是否发生改动)。"""
        if not text:
            return text, False
        original = text
        for pattern in self._strip_patterns():
            text = pattern.sub("", text)
        unclosed = self._unclosed_pattern()
        if unclosed is not None:
            text = unclosed.sub("", text)
        if text != original:
            if self._conf_bool("tidy_whitespace", True):
                text = re.sub(r"[ \t]+\n", "\n", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
                text = text.strip()
            if self._is_debug():
                logger.debug(
                    f"[think-prefix] 已清理思考内容: {len(original)} -> {len(text)} 字符",
                )
        return text, text != original

    def _clean_chain(self, chain: Any, *, drop_empty: bool = False) -> int:
        """对消息链里所有文本组件做清理，返回被修改的组件数量。"""
        if not isinstance(chain, list):
            return 0
        changed = 0
        for comp in list(chain):
            text = getattr(comp, "text", None)
            if not isinstance(text, str) or not text:
                continue
            new_text, modified = self._clean_text(text)
            if not modified:
                continue
            changed += 1
            if drop_empty and not new_text.strip() and len(chain) > 1:
                try:
                    chain.remove(comp)
                    continue
                except ValueError:  # pragma: no cover - 极少数不可变链
                    pass
            try:
                comp.text = new_text
            except Exception as e:  # pragma: no cover - 组件不可写时忽略
                logger.debug(f"[think-prefix] 文本组件写入失败，已跳过: {e}")
        return changed

    # --------------------------------------------------------------- 触发处理

    @staticmethod
    def _first_text(event: AstrMessageEvent) -> str:
        text = getattr(event, "message_str", "") or ""
        if isinstance(text, str) and text.strip():
            return text
        chain = getattr(getattr(event, "message_obj", None), "message", None)
        if isinstance(chain, list):
            for comp in chain:
                comp_text = getattr(comp, "text", None)
                if isinstance(comp_text, str) and comp_text.strip():
                    return comp_text
        return ""

    def _strip_trigger_everywhere(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        pattern = self._trigger_pattern()

        # 1) 事件本体（影响后续管线与写入历史的用户消息）
        try:
            raw = getattr(event, "message_str", "") or ""
            if isinstance(raw, str) and pattern.match(raw):
                event.message_str = pattern.sub("", raw, count=1)
        except Exception as e:  # pragma: no cover
            logger.debug(f"[think-prefix] 改写 event.message_str 失败: {e}")

        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None)
        if isinstance(chain, list):
            for comp in chain:
                comp_text = getattr(comp, "text", None)
                if isinstance(comp_text, str) and pattern.match(comp_text):
                    try:
                        comp.text = pattern.sub("", comp_text, count=1)
                    except Exception as e:  # pragma: no cover
                        logger.debug(f"[think-prefix] 改写消息组件失败: {e}")
                    break
        if message_obj is not None:
            obj_text = getattr(message_obj, "message_str", None)
            if isinstance(obj_text, str) and pattern.match(obj_text):
                try:
                    message_obj.message_str = pattern.sub("", obj_text, count=1)
                except Exception:  # pragma: no cover
                    pass

        # 2) 本次请求的提示词
        prompt = getattr(req, "prompt", None)
        if isinstance(prompt, str) and pattern.match(prompt):
            try:
                req.prompt = pattern.sub("", prompt, count=1)
            except Exception:  # pragma: no cover
                pass

        # 3) 已经写进上下文的最后一条用户消息
        self._strip_trigger_in_contexts(req, pattern)

    @staticmethod
    def _strip_trigger_in_contexts(req: ProviderRequest, pattern: re.Pattern[str]) -> None:
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return
        for message in reversed(contexts):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                if pattern.match(content):
                    message["content"] = pattern.sub("", content, count=1)
            elif isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                        and pattern.match(part["text"])
                    ):
                        part["text"] = pattern.sub("", part["text"], count=1)
                        break
            break

    # ----------------------------------------------------------- 提供商补丁

    async def _get_provider(self, event: AstrMessageEvent) -> Any:
        umo = getattr(event, "unified_msg_origin", None)
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            try:
                return await getter(umo=umo)
            except TypeError:
                return await getter()
        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            return getter(umo=umo)
        return None

    @staticmethod
    def _provider_kind(provider: Any, provider_type: str) -> str:
        if hasattr(provider, "thinking_config") or "anthropic" in provider_type:
            return "anthropic"
        if "google" in provider_type or "gemini" in provider_type:
            return "gemini"
        return "openai"

    async def _enable_native_thinking(self, event: AstrMessageEvent) -> None:
        self._restore_stale()
        provider = await self._get_provider(event)
        if provider is None:
            logger.warning("[think-prefix] 未找到可用的对话提供商，本次未能开启思考。")
            return

        provider_config = getattr(provider, "provider_config", None)
        if not isinstance(provider_config, dict):
            logger.warning("[think-prefix] 提供商配置不可写，本次未能开启思考。")
            return

        key = id(provider)
        if key in self._patched:
            return  # 同一轮工具调用循环里已经打过补丁

        provider_type = str(provider_config.get("type", "") or "")
        provider_name = str(provider_config.get("provider", "") or provider_type)
        kind = self._provider_kind(provider, provider_type)
        changes: list[tuple[str, str, bool, Any]] = []

        if kind == "anthropic":
            thinking = self._conf_json("anthropic_thinking", DEFAULT_ANTHROPIC_THINKING)
            if not thinking:
                return
            changes.append(
                (
                    "config",
                    "anth_thinking_config",
                    "anth_thinking_config" in provider_config,
                    copy.deepcopy(provider_config.get("anth_thinking_config")),
                ),
            )
            provider_config["anth_thinking_config"] = thinking
            # Anthropic 适配器在 __init__ 里把该配置缓存到了实例属性上
            if hasattr(provider, "thinking_config"):
                changes.append(
                    ("attr", "thinking_config", True, copy.deepcopy(getattr(provider, "thinking_config", None))),
                )
                provider.thinking_config = thinking
        elif kind == "gemini":
            thinking = self._conf_json("gemini_thinking", DEFAULT_GEMINI_THINKING)
            if not thinking:
                return
            changes.append(
                (
                    "config",
                    "gm_thinking_config",
                    "gm_thinking_config" in provider_config,
                    copy.deepcopy(provider_config.get("gm_thinking_config")),
                ),
            )
            provider_config["gm_thinking_config"] = thinking
            if hasattr(provider, "gm_thinking_config"):
                changes.append(
                    ("attr", "gm_thinking_config", True, copy.deepcopy(getattr(provider, "gm_thinking_config", None))),
                )
                provider.gm_thinking_config = thinking
        else:
            extra_body = self._conf_json("openai_extra_body", DEFAULT_OPENAI_EXTRA_BODY)
            if not extra_body:
                return
            old_extra = provider_config.get("custom_extra_body")
            merged = dict(old_extra) if isinstance(old_extra, dict) else {}
            merged.update(extra_body)
            changes.append(
                (
                    "config",
                    "custom_extra_body",
                    "custom_extra_body" in provider_config,
                    copy.deepcopy(old_extra),
                ),
            )
            provider_config["custom_extra_body"] = merged

        self._patched[key] = {
            "provider": provider,
            "kind": kind,
            "umo": getattr(event, "unified_msg_origin", ""),
            "stamp": time.time(),
            "changes": changes,
        }
        # 记下模型名，供思考日志标注
        try:
            model_name = ""
            getter = getattr(provider, "get_model", None)
            if callable(getter):
                model_name = str(getter() or "")
            event.set_extra(
                "_think_prefix_provider_name",
                f"{provider_name}/{model_name}" if model_name else provider_name,
            )
        except Exception:  # pragma: no cover
            pass
        logger.info(
            f"[think-prefix] 已为提供商 {provider_name}({provider_type}) 打开思考模式，"
            f"适配方式: {kind}",
        )

    def _restore_stale(self) -> None:
        now = time.time()
        stale = [
            key
            for key, state in self._patched.items()
            if now - float(state.get("stamp", now)) > PATCH_TTL_SECONDS
        ]
        if stale:
            logger.warning(f"[think-prefix] 发现 {len(stale)} 个超时未还原的提供商补丁，正在还原。")
            self._restore(stale, reason="timeout")

    def _restore(
        self,
        keys: list[int] | None = None,
        reason: str = "",
        umo: str | None = None,
    ) -> None:
        """还原补丁。

        Args:
            keys: 指定要还原的补丁 key；None 表示按 umo 过滤（umo 也为 None 则全部）。
            reason: 日志用。
            umo: 只还原属于该会话的补丁，避免 A 会话结束时误还原 B 会话正在用的补丁。
        """
        if keys is None:
            targets = [
                key
                for key, state in self._patched.items()
                if umo is None or state.get("umo") == umo
            ]
        else:
            targets = list(keys)
        for key in targets:
            state = self._patched.pop(key, None)
            if not state:
                continue
            provider = state.get("provider")
            for target, name, existed, old_value in state.get("changes", []):
                try:
                    if target == "config":
                        provider_config = getattr(provider, "provider_config", None)
                        if not isinstance(provider_config, dict):
                            continue
                        if existed:
                            provider_config[name] = old_value
                        else:
                            provider_config.pop(name, None)
                    elif existed:
                        setattr(provider, name, old_value)
                    else:
                        try:
                            delattr(provider, name)
                        except AttributeError:
                            pass
                except Exception as e:
                    logger.warning(f"[think-prefix] 还原提供商配置 {name} 失败: {e}")
            if self._is_debug():
                logger.debug(f"[think-prefix] 已还原提供商补丁 ({reason or 'done'})")

    # ------------------------------------------------------------------ 钩子

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """检测 <think> 前缀：清前缀 + 开思考。

        Note:
            该钩子每条用户消息只触发一次，工具调用循环中的多次 LLM 请求共用本次
            打好的补丁，所以补丁要一直留到本轮结束。
        """
        try:
            already = bool(event.get_extra(TRIGGER_FLAG, False))
            text = self._first_text(event)
            if not already and not (text and self._trigger_pattern().match(text)):
                return

            event.set_extra(TRIGGER_FLAG, True)
            if not already:
                logger.info(f"[think-prefix] 触发思考模式: {event.unified_msg_origin}")

            if not already and self._conf_bool("strip_trigger_from_message", True):
                self._strip_trigger_everywhere(event, req)

            hint = str(self._conf("thinking_prompt_hint", "") or "").strip()
            if hint:
                req.system_prompt = f"{req.system_prompt or ''}\n{hint}"

            # AstrBot 内置 Agent 支持用事件额外信息临时覆盖「是否展示思考过程」，
            # 这里强制关掉，用户只看到最终回答。
            if self._conf_bool("hide_reasoning_display", True):
                try:
                    event.set_extra("enable_reasoning", False)
                except Exception as e:  # pragma: no cover
                    logger.debug(f"[think-prefix] 关闭思考过程展示失败: {e}")

            if self._conf_bool("enable_native_thinking", True):
                await self._enable_native_thinking(event)
        except Exception as e:
            logger.error(f"[think-prefix] on_llm_request 处理失败: {e}", exc_info=True)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """把思考过程写进日志，并清理模型回复里的思考内容。

        Note:
            这里不还原提供商补丁：工具调用循环的后续步骤还会继续请求模型。
        """
        try:
            self._log_reasoning(event, self._collect_reasoning(event, response))
        except Exception as e:
            logger.error(f"[think-prefix] 思考日志记录失败: {e}", exc_info=True)
        try:
            if self._should_strip(event):
                self._clean_chain(getattr(getattr(response, "result_chain", None), "chain", None))
        except Exception as e:
            logger.error(f"[think-prefix] on_llm_response 清理失败: {e}", exc_info=True)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """消息发出前的最后一道正则过滤。"""
        try:
            if not self._should_strip(event):
                return
            result = event.get_result()
            if result is None:
                return
            changed = self._clean_chain(getattr(result, "chain", None), drop_empty=True)
            if changed and self._is_debug():
                logger.debug(f"[think-prefix] 发送前清理了 {changed} 个文本组件")
        except Exception as e:
            logger.error(f"[think-prefix] on_decorating_result 清理失败: {e}", exc_info=True)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """整轮对话结束：补记思考日志 + 还原提供商配置。"""
        self._flush_reasoning_log(event)
        try:
            if self._conf_bool("restore_after_response", True):
                self._restore(
                    reason="after_message_sent",
                    umo=getattr(event, "unified_msg_origin", ""),
                )
        except Exception as e:  # pragma: no cover
            logger.error(f"[think-prefix] after_message_sent 还原失败: {e}", exc_info=True)

    def _should_strip(self, event: AstrMessageEvent) -> bool:
        if self._conf_bool("always_strip", True):
            return True
        try:
            return bool(event.get_extra(TRIGGER_FLAG, False))
        except Exception:  # pragma: no cover
            return False

    # --------------------------------------------------------- 思考过程日志

    def _collect_reasoning(self, event: AstrMessageEvent, response: LLMResponse) -> str:
        """把流式增量拼起来，返回本次可以落日志的完整思考文本（没有则空串）。

        - 流式 chunk：`reasoning_content` 是增量，先累积到 event 上；
        - 最终响应：`reasoning_content` 通常是全量，直接用它；
          若最终响应没带（部分中转站会丢），就退回用累积的增量。
        """
        chunk = getattr(response, "reasoning_content", None)
        chunk = chunk if isinstance(chunk, str) else ""
        try:
            buf = str(event.get_extra(REASONING_BUF, "") or "")
        except Exception:  # pragma: no cover
            buf = ""

        if bool(getattr(response, "is_chunk", False)):
            if chunk:
                event.set_extra(REASONING_BUF, buf + chunk)
            return ""

        text = chunk if len(chunk) >= len(buf) else buf
        if buf:
            event.set_extra(REASONING_BUF, "")
        return text.strip()

    def _log_reasoning(self, event: AstrMessageEvent, text: str) -> None:
        if not text or not self._conf_bool("log_reasoning", True):
            return
        try:
            step = int(event.get_extra(REASONING_STEP, 0) or 0) + 1
            event.set_extra(REASONING_STEP, step)
        except Exception:  # pragma: no cover
            step = 1

        limit = self._conf_int("log_reasoning_max_chars", 3000)
        body = text
        if limit > 0 and len(body) > limit:
            body = f"{body[:limit]}\n…（思考过长，已截断，共 {len(text)} 字）"

        model_name = ""
        try:
            provider = event.get_extra("_think_prefix_provider_name", "")
            model_name = str(provider or "")
        except Exception:  # pragma: no cover
            model_name = ""

        head = f"model={model_name}, " if model_name else ""
        logger.info(
            f"\n[think-prefix] ===== 思考过程开始（{head}第 {step} 段，{len(text)} 字）=====\n"
            f"{body}\n"
            f"[think-prefix] ===== 思考过程结束 =====",
        )

    def _flush_reasoning_log(self, event: AstrMessageEvent) -> None:
        """兜底：流式结束但最终响应没带思考内容时，把累积的补记到日志。"""
        try:
            buf = str(event.get_extra(REASONING_BUF, "") or "")
            if not buf.strip():
                return
            event.set_extra(REASONING_BUF, "")
            self._log_reasoning(event, buf.strip())
        except Exception as e:  # pragma: no cover
            logger.debug(f"[think-prefix] 补记思考日志失败: {e}")

    async def terminate(self) -> None:
        """插件卸载 / 重载时还原所有补丁。"""
        self._restore(reason="terminate")
