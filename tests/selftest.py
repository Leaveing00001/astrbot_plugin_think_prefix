"""本地自测：用桩模块模拟 AstrBot，验证 main.py 的触发 / 开思考 / 正则清理 / 日志逻辑。

放在插件仓库里，直接跑：
    python tests/selftest.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------- 桩 AstrBot

calls: dict[str, list] = {"info": [], "warning": [], "error": [], "debug": []}


class _Logger:
    def info(self, msg, *a, **k):
        calls["info"].append(str(msg))

    def warning(self, msg, *a, **k):
        calls["warning"].append(str(msg))

    def error(self, msg, *a, **k):
        calls["error"].append(str(msg))

    def debug(self, msg, *a, **k):
        calls["debug"].append(str(msg))


def _identity_decorator(*a, **k):
    def deco(fn):
        return fn

    return deco


filter_stub = types.SimpleNamespace(
    on_llm_request=_identity_decorator,
    on_llm_response=_identity_decorator,
    on_decorating_result=_identity_decorator,
    after_message_sent=_identity_decorator,
)

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = _Logger()
api.AstrBotConfig = dict

event_mod = types.ModuleType("astrbot.api.event")
event_mod.filter = filter_stub


class AstrMessageEvent:  # noqa: D101
    pass


event_mod.AstrMessageEvent = AstrMessageEvent

provider_mod = types.ModuleType("astrbot.api.provider")


class LLMResponse:  # noqa: D101
    pass


class ProviderRequest:  # noqa: D101
    pass


provider_mod.LLMResponse = LLMResponse
provider_mod.ProviderRequest = ProviderRequest

star_mod = types.ModuleType("astrbot.api.star")


class Context:  # noqa: D101
    pass


class Star:  # noqa: D101
    def __init__(self, context):
        self.context = context


def register(*a, **k):
    def deco(cls):
        return cls

    return deco


star_mod.Context = Context
star_mod.Star = Star
star_mod.register = register

# 包结构
pkg_api = types.ModuleType("astrbot.api")
pkg_api.logger = api.logger
pkg_api.AstrBotConfig = dict
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = pkg_api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.provider"] = provider_mod
sys.modules["astrbot.api.star"] = star_mod

sys.path.insert(0, str(PLUGIN_DIR))
import main as plugin_main  # noqa: E402

# ------------------------------------------------------------------ 测试工具


class Plain:  # noqa: D101
    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return f"Plain({self.text!r})"


class MessageObj:  # noqa: D101
    def __init__(self, text):
        self.message = [Plain(text)]
        self.message_str = text


class Event(AstrMessageEvent):  # noqa: D101
    def __init__(self, text):
        self.message_str = text
        self.message_obj = MessageObj(text)
        self.unified_msg_origin = "aiocqhttp:GroupMessage:12345"
        self._extras = {}
        self._result = None

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def get_result(self):
        return self._result

    def set_result(self, result):
        self._result = result


class Chain:
    def __init__(self, comps):
        self.chain = comps


class Req:
    def __init__(self, prompt="", system_prompt="", contexts=None):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = contexts or []


class FakeProvider:
    def __init__(self, ptype, provider, thinking_attr=False):
        self.provider_config = {"type": ptype, "provider": provider, "id": "p1"}
        if thinking_attr:
            self.thinking_config = {"type": "", "budget": 0}


class FakeContext:
    def __init__(self, provider):
        self._provider = provider

    async def get_using_provider_async(self, umo=None):
        return self._provider


def load_config() -> dict:
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    return {k: v.get("default") for k, v in schema.items()}


PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  -> ' + extra) if extra and not cond else ''}")


async def main() -> int:
    cfg = load_config()
    print("配置默认值:", json.dumps(cfg, ensure_ascii=False))

    # ---------------------------------------------- 1. OpenAI 兼容提供商
    prov = FakeProvider("openai_chat_completion", "deepseek")
    plugin = plugin_main.ThinkPrefixPlugin(FakeContext(prov), dict(cfg))
    ev = Event("<think> 1+1 等于几？")
    req = Req(prompt="<think> 1+1 等于几？", contexts=[{"role": "user", "content": "<think> 1+1 等于几？"}])
    await plugin.on_llm_request(ev, req)
    check("触发前缀从 req.prompt 删除", req.prompt.strip() == "1+1 等于几？", repr(req.prompt))
    check("触发前缀从 event.message_str 删除", ev.message_str.strip() == "1+1 等于几？", repr(ev.message_str))
    check("触发前缀从消息链删除", ev.message_obj.message[0].text.strip() == "1+1 等于几？")
    check("触发前缀从上下文删除", req.contexts[0]["content"].strip() == "1+1 等于几？", repr(req.contexts[0]["content"]))
    check(
        "OpenAI 兼容写入 custom_extra_body",
        prov.provider_config.get("custom_extra_body") == {"reasoning_effort": "high"},
        repr(prov.provider_config.get("custom_extra_body")),
    )
    check("事件标记已写入", ev.get_extra(plugin_main.TRIGGER_FLAG) is True)

    # 回复清理
    resp = LLMResponse()
    resp.result_chain = Chain([Plain("<think>先把 1 和 1 相加……</think>\n\n答案是 2。")])
    await plugin.on_llm_response(ev, resp)
    check("成对 <think> 标签被清理", resp.result_chain.chain[0].text == "答案是 2。", repr(resp.result_chain.chain[0].text))

    # 发送前清理（含未闭合片段 + 落单结束标签）
    ev.set_result(Chain([Plain("前言"), Plain(" 让我想想 <thinking>还没写完"), Plain("</thinking> 结论是 A")]))
    await plugin.on_decorating_result(ev)
    texts = [c.text for c in ev.get_result().chain]
    check("未闭合思考片段被截断", "还没写完" not in "".join(texts), repr(texts))
    check("落单结束标签被清理", "</thinking>" not in "".join(texts), repr(texts))
    check("正常文本保留", "前言" in "".join(texts) and "结论是 A" in "".join(texts), repr(texts))

    await plugin.after_message_sent(ev)
    check("本轮结束后还原 custom_extra_body", "custom_extra_body" not in prov.provider_config, repr(prov.provider_config))

    # ---------------------------------------------- 2. 未触发时不动作
    prov2 = FakeProvider("openai_chat_completion", "deepseek")
    plugin2 = plugin_main.ThinkPrefixPlugin(FakeContext(prov2), dict(cfg))
    ev2 = Event("普通消息，没有前缀")
    req2 = Req(prompt="普通消息，没有前缀")
    await plugin2.on_llm_request(ev2, req2)
    check("无前缀时不打补丁", "custom_extra_body" not in prov2.provider_config)
    # always_strip 默认 true，普通回复也会清理
    resp2 = LLMResponse()
    resp2.result_chain = Chain([Plain("<think>内部推理</think>可见回答")])
    await plugin2.on_llm_response(ev2, resp2)
    check("always_strip=true 时普通回复也清理", resp2.result_chain.chain[0].text == "可见回答", repr(resp2.result_chain.chain[0].text))

    # ---------------------------------------------- 3. Anthropic
    prov3 = FakeProvider("anthropic_chat_completion", "anthropic", thinking_attr=True)
    plugin3 = plugin_main.ThinkPrefixPlugin(FakeContext(prov3), dict(cfg))
    ev3 = Event("<think>讲讲相对论")
    req3 = Req(prompt="<think>讲讲相对论")
    await plugin3.on_llm_request(ev3, req3)
    check(
        "Anthropic 写入 thinking_config",
        prov3.thinking_config == {"type": "", "budget": 4096, "effort": ""},
        repr(prov3.thinking_config),
    )
    check("Anthropic 同步写入 provider_config", "anth_thinking_config" in prov3.provider_config)
    await plugin3.after_message_sent(ev3)
    check("Anthropic 还原实例属性", prov3.thinking_config == {"type": "", "budget": 0}, repr(prov3.thinking_config))
    check("Anthropic 还原 provider_config", "anth_thinking_config" not in prov3.provider_config)

    # ---------------------------------------------- 4. Gemini
    prov4 = FakeProvider("googlegenai_chat_completion", "google")
    plugin4 = plugin_main.ThinkPrefixPlugin(FakeContext(prov4), dict(cfg))
    ev4 = Event("<THINK>讲个笑话")  # 大小写不敏感
    req4 = Req(prompt="<THINK>讲个笑话")
    await plugin4.on_llm_request(ev4, req4)
    check(
        "Gemini 写入 gm_thinking_config",
        prov4.provider_config.get("gm_thinking_config") == {"budget": -1, "level": "HIGH"},
        repr(prov4.provider_config.get("gm_thinking_config")),
    )
    check("大小写不敏感触发", req4.prompt.strip() == "讲个笑话", repr(req4.prompt))

    # ---------------------------------------------- 5. 关闭原生思考 / 自定义正则
    cfg5 = dict(cfg)
    cfg5["enable_native_thinking"] = False
    cfg5["extra_strip_patterns"] = [r"^思考[:：].*$"]
    cfg5["log_level"] = "debug"
    prov5 = FakeProvider("openai_chat_completion", "openai")
    plugin5 = plugin_main.ThinkPrefixPlugin(FakeContext(prov5), dict(cfg5))
    ev5 = Event("<think> hello")
    req5 = Req(prompt="<think> hello")
    await plugin5.on_llm_request(ev5, req5)
    check("enable_native_thinking=false 时不打补丁", "custom_extra_body" not in prov5.provider_config)
    resp5 = LLMResponse()
    resp5.result_chain = Chain([Plain("思考：这里是推理\n真正的回答")])
    await plugin5.on_llm_response(ev5, resp5)
    check(
        "自定义正则生效",
        resp5.result_chain.chain[0].text == "真正的回答",
        repr(resp5.result_chain.chain[0].text),
    )

    # ---------------------------------------------- 6. 非法 JSON 配置回退默认值
    cfg6 = dict(cfg)
    cfg6["openai_extra_body"] = "{这不是 JSON"
    prov6 = FakeProvider("openai_chat_completion", "deepseek")
    plugin6 = plugin_main.ThinkPrefixPlugin(FakeContext(prov6), dict(cfg6))
    ev6 = Event("<think> hi")
    await plugin6.on_llm_request(ev6, Req(prompt="<think> hi"))
    check(
        "非法 JSON 回退到默认值",
        prov6.provider_config.get("custom_extra_body") == {"reasoning_effort": "high"},
        repr(prov6.provider_config.get("custom_extra_body")),
    )

    # ---------------------------------------------- 7. 空 trigger_prefix 安全
    cfg7 = dict(cfg)
    cfg7["trigger_prefix"] = ""
    plugin7 = plugin_main.ThinkPrefixPlugin(FakeContext(None), dict(cfg7))
    ev7 = Event("随便一条消息")
    await plugin7.on_llm_request(ev7, Req(prompt="随便一条消息"))
    check("空 trigger_prefix 不会误触发", ev7.get_extra(plugin_main.TRIGGER_FLAG) is None)

    # ---------------------------------------------- 8. 提示词注入
    cfg8 = dict(cfg)
    cfg8["thinking_prompt_hint"] = "先推理再回答。"
    prov8 = FakeProvider("openai_chat_completion", "deepseek")
    plugin8 = plugin_main.ThinkPrefixPlugin(FakeContext(prov8), dict(cfg8))
    ev8 = Event("<think> 问题")
    req8 = Req(prompt="<think> 问题", system_prompt="你是助手。")
    await plugin8.on_llm_request(ev8, req8)
    check("思考提示词已追加", req8.system_prompt == "你是助手。\n先推理再回答。", repr(req8.system_prompt))
    check("触发时关闭思考过程展示", ev8.get_extra("enable_reasoning") is False, repr(ev8.get_extra("enable_reasoning")))

    # ---------------------------------------------- 9. 还原只作用于本会话
    prov9 = FakeProvider("openai_chat_completion", "deepseek")
    plugin9 = plugin_main.ThinkPrefixPlugin(FakeContext(prov9), dict(cfg))
    ev_a = Event("<think> A 会话")
    await plugin9.on_llm_request(ev_a, Req(prompt="<think> A 会话"))
    ev_b = Event("B 会话的普通消息")
    ev_b.unified_msg_origin = "aiocqhttp:GroupMessage:99999"
    await plugin9.after_message_sent(ev_b)
    check(
        "别的会话结束不会误还原补丁",
        prov9.provider_config.get("custom_extra_body") == {"reasoning_effort": "high"},
        repr(prov9.provider_config.get("custom_extra_body")),
    )
    await plugin9.after_message_sent(ev_a)
    check("本会话结束才还原补丁", "custom_extra_body" not in prov9.provider_config)

    # ---------------------------------------------- 10. 思考过程日志
    prov10 = FakeProvider("openai_chat_completion", "deepseek")
    plugin10 = plugin_main.ThinkPrefixPlugin(FakeContext(prov10), dict(cfg))
    ev10 = Event("<think> 讲讲相对论")
    await plugin10.on_llm_request(ev10, Req(prompt="<think> 讲讲相对论"))

    calls["info"].clear()
    c1 = LLMResponse()
    c1.is_chunk = True
    c1.reasoning_content = "先想第一步，"
    await plugin10.on_llm_response(ev10, c1)
    check(
        "流式思考片段不单独刷日志",
        not any("思考过程开始" in m for m in calls["info"]),
        repr(calls["info"][-1:]),
    )

    c2 = LLMResponse()
    c2.is_chunk = True
    c2.reasoning_content = "再想第二步。"
    await plugin10.on_llm_response(ev10, c2)

    fin = LLMResponse()
    fin.is_chunk = False
    fin.reasoning_content = "先想第一步，再想第二步。"
    fin.result_chain = Chain([Plain("相对论讲的是……")])
    await plugin10.on_llm_response(ev10, fin)

    logs = [m for m in calls["info"] if "思考过程开始" in m]
    check("最终响应刷出完整思考日志", len(logs) == 1, repr(logs))
    check(
        "日志包含完整思考内容与模型名",
        bool(logs) and "再想第二步" in logs[0] and "deepseek" in logs[0],
        repr(logs[0] if logs else ""),
    )
    await plugin10.after_message_sent(ev10)
    check(
        "已落日志后不会重复补记",
        len([m for m in calls["info"] if "思考过程开始" in m]) == 1,
        repr([m for m in calls["info"] if "思考过程开始" in m]),
    )

    # 只有流式片段、最终响应不带思考内容 -> 由 after_message_sent 补记
    prov11 = FakeProvider("openai_chat_completion", "deepseek")
    plugin11 = plugin_main.ThinkPrefixPlugin(FakeContext(prov11), dict(cfg))
    ev11 = Event("<think> 再来一次")
    await plugin11.on_llm_request(ev11, Req(prompt="<think> 再来一次"))
    calls["info"].clear()
    only_chunk = LLMResponse()
    only_chunk.is_chunk = True
    only_chunk.reasoning_content = "只有增量的思考"
    await plugin11.on_llm_response(ev11, only_chunk)
    await plugin11.after_message_sent(ev11)
    check(
        "最终响应没带思考时由兜底补记",
        any("只有增量的思考" in m for m in calls["info"]),
        repr(calls["info"]),
    )

    # 截断 + 关闭开关
    cfg12 = dict(cfg)
    cfg12["log_reasoning_max_chars"] = 5
    prov12 = FakeProvider("openai_chat_completion", "deepseek")
    plugin12 = plugin_main.ThinkPrefixPlugin(FakeContext(prov12), dict(cfg12))
    ev12 = Event("<think> 截断测试")
    await plugin12.on_llm_request(ev12, Req(prompt="<think> 截断测试"))
    calls["info"].clear()
    long_resp = LLMResponse()
    long_resp.is_chunk = False
    long_resp.reasoning_content = "一二三四五六七八九十"
    await plugin12.on_llm_response(ev12, long_resp)
    check(
        "超过上限会截断并注明总字数",
        any("已截断，共 10 字" in m for m in calls["info"]),
        repr(calls["info"]),
    )

    cfg13 = dict(cfg)
    cfg13["log_reasoning"] = False
    prov13 = FakeProvider("openai_chat_completion", "deepseek")
    plugin13 = plugin_main.ThinkPrefixPlugin(FakeContext(prov13), dict(cfg13))
    ev13 = Event("<think> 关闭日志")
    await plugin13.on_llm_request(ev13, Req(prompt="<think> 关闭日志"))
    calls["info"].clear()
    off_resp = LLMResponse()
    off_resp.is_chunk = False
    off_resp.reasoning_content = "不该出现在日志里"
    await plugin13.on_llm_response(ev13, off_resp)
    check(
        "log_reasoning=false 时不写日志",
        not any("不该出现在日志里" in m for m in calls["info"]),
        repr(calls["info"]),
    )

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:", FAIL)
    if calls["error"] or calls["warning"]:
        print("日志(warning/error):", calls["warning"], calls["error"])
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
