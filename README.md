# astrbot_plugin_think_prefix

给 AstrBot 用的「前缀触发思考」插件。

在消息开头打上 `<think>`，这一轮的模型就会**打开原生思考（推理）**；同时插件会在回复发出前
用正则把思考内容删干净，用户只会看到最终答案。

```
你：<think> 帮我推导一下等差数列求和公式
机器人：（模型内部思考 3.2 秒…） 设首项为 a1，公差为 d …… 所以 Sn = n·a1 + n(n-1)d/2
```

## 关于本项目：DeepSeek Harness 的 VibeCoding 产物

> 本插件是 **[DeepSeek Harness](https://deepseek.com)** 的 **VibeCoding 产物**。
>
> 从需求理解、AstrBot 源码接口核对（`ProviderRequest` / `custom_extra_body` / `thinking_config` /
> `gm_thinking_config` / 事件钩子签名）、插件实现、单元自测，到装进真实 AstrBot 实例并用真实模型
> 跑通「触发 → 开思考 → 落日志」，全过程由 AI 编码代理 DeepSeek Harness 完成，人类只负责提需求和拍板。
> 代码里的每一处适配结论都对应一次源码阅读或一次线上实测，而不是猜测。

## 安装

1. 把整个 `astrbot_plugin_think_prefix` 文件夹放进 AstrBot 的 `data/plugins/` 目录；
2. 打开 WebUI → 插件 → 重载插件（或重启 AstrBot）；
3. 在插件卡片上点「管理」即可修改配置。

无第三方依赖，不需要 `requirements.txt`。

## 它到底做了什么

一次带 `<think>` 前缀的对话，会依次发生三件事：

| 步骤 | 钩子 | 行为 |
| --- | --- | --- |
| 1 | `on_llm_request` | 删掉触发前缀（默认开启），让模型和历史只看到正文 |
| 2 | `on_llm_request` | 临时给当前提供商写入「思考参数」，打开原生思考 |
| 3 | `on_llm_response` / `on_decorating_result` | 正则删除回复里的思考内容 |
| 4 | `after_message_sent` | 还原提供商配置（提供商实例是全局共享的） |

顺带说明：AstrBot 的 `OnLLMRequestEvent` 每条用户消息只触发一次，之后的工具调用循环共用同一个
`ProviderRequest`，所以补丁会保留到整轮结束才还原。

## 各提供商的思考开关

| 提供商类型 | 插件写哪里 | 默认值 |
| --- | --- | --- |
| OpenAI 兼容（DeepSeek / 智谱 / 硅基流动 / vLLM / OpenRouter / OpenAI …） | `provider_config["custom_extra_body"]` | `{"reasoning_effort": "high"}` |
| Anthropic / Claude（以及 Kimi Coding Plan、小米、MiniMax Token Plan 等衍生适配器） | `provider.thinking_config` + `provider_config["anth_thinking_config"]` | `{"type": "", "budget": 4096, "effort": ""}` |
| Google Gemini | `provider_config["gm_thinking_config"]` | `{"budget": -1, "level": "HIGH"}` |

常用的 `openai_extra_body` 取值：

```jsonc
// 通用推荐：OpenAI GPT-5 / o 系列、xAI Grok、DeepSeek（Chat Completions 与 Responses 都认）
{"reasoning_effort": "high"}     // 拉满写 "max"

// DeepSeek V4 实测结论（见下）
{"reasoning_effort": "max"}

// 需要显式开关的模型
{"thinking": {"type": "enabled"}}                      // DeepSeek / 智谱 GLM / Kimi
{"enable_thinking": true}                              // 阿里云百炼 Qwen3
{"chat_template_kwargs": {"enable_thinking": true}}    // vLLM / SGLang 自建
{"reasoning": {"enabled": true}}                       // OpenRouter
```

> 换了取值之后如果模型报 400，说明该服务商不认这个字段，换成上面对应的那一种即可。

### DeepSeek V4 的真实情况（2026-09 实测）

按 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode) 实测本机两个提供商：

| 提供商类型 | 官方开关字段 | `{"thinking":{"type":"enabled"}}` | `{"reasoning_effort":"max"}` |
| --- | --- | --- | --- |
| Responses API（`deepseek-responses`） | `{"reasoning": {"effort": "none/low/high/max"}}` | HTTP 200，但**只是被忽略** | HTTP 200，真正生效 |
| Chat Completions（`deepseek`） | `{"thinking": {"type": "enabled/disabled"}}` + `reasoning_effort` | HTTP 200，生效 | HTTP 200，生效 |

两条重要结论：

1. **DeepSeek V4 的思考默认就是打开的，effort 默认 `high`**。所以 `<think>` 前缀的价值不是「从关到开」，
   而是「把思考强度拉到 `max`」+ 隐藏思考过程 + 正则清理思考内容。
2. Responses API 不认 `thinking` 字段（不报错，但也不起作用），要用 `reasoning.effort`。
   AstrBot 会把 `reasoning_effort` 自动转换过去，所以**两种适配器都写 `reasoning_effort` 即可**。

### 想做到「平时不思考，只有 `<think>` 才思考」

因为插件是「合并 + 用后还原」`custom_extra_body` 的，所以只要把提供商自己的默认值调成关闭，
插件在触发时会临时覆盖它：

1. WebUI → 服务提供商 → 选中 `deepseek-v4-pro` / `deepseek-flash`；
2. 展开「自定义请求体 (`custom_extra_body`)」，加一项 `reasoning_effort` = `none`
   （Responses API 会转换成 `{"reasoning": {"effort": "none"}}`，即关闭思考）；
3. 保持插件里 `openai_extra_body` = `{"reasoning_effort": "max"}`。

这样普通消息不思考、带 `<think>` 的消息全力思考，回复结束后自动还原成「不思考」。

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `trigger_prefix` | `<think>` | 触发前缀，消息以此开头即生效 |
| `trigger_regex` | 空 | 高级用法，填了就忽略前缀；需匹配消息开头的触发标记 |
| `case_insensitive` | `true` | `<THINK>` 也能触发 |
| `strip_trigger_from_message` | `true` | 把触发前缀从消息/提示词/上下文里删掉 |
| `enable_native_thinking` | `true` | 关掉就只做「清理思考内容」，不碰提供商配置 |
| `openai_extra_body` | `{"reasoning_effort":"high"}` | OpenAI 兼容提供商的思考参数 |
| `anthropic_thinking` | `{"type":"","budget":4096,"effort":""}` | Claude 思考预算；Claude 4.5+ 可用 `{"type":"adaptive","effort":"high"}` |
| `gemini_thinking` | `{"budget":-1,"level":"HIGH"}` | Gemini 2.5 用 `budget`（-1 动态思考），Gemini 3 用 `level` |
| `thinking_prompt_hint` | 空 | 追加到 system prompt，例如 `请在 <think></think> 中先推理再回答`，适合没有原生开关的模型 |
| `hide_reasoning_display` | `true` | 触发时给事件打上 `enable_reasoning=False`，本轮不把思考发给用户（只影响发给用户的消息，不影响写日志） |
| `log_reasoning` | `true` | 把模型的 `reasoning_content` 写进 AstrBot 日志 |
| `log_reasoning_max_chars` | `3000` | 单段思考日志的字符上限，超出会截断并注明总字数；填 `0` 不截断 |
| `always_strip` | `true` | 始终清理思考内容；关闭后只清理被 `<think>` 触发的那一轮 |
| `strip_unclosed` | `true` | 处理只有 `<think>` 没有 `</think>` 的半截输出 |
| `extra_strip_patterns` | `[]` | 追加自定义清理正则（每行一个，按单行语义匹配，`^` `$` 匹配行首行尾；要跨行自己加 `(?s)`） |
| `tidy_whitespace` | `true` | 清理后折叠多余空行、去掉首尾空白 |
| `restore_after_response` | `true` | 本轮结束后还原提供商配置 |
| `log_level` | `normal` | 改成 `debug` 会打印正则清理细节 |

## 内置清理规则

```python
<\s*(think|thinking|reasoning|thought|thoughts|analysis|scratchpad|reflection)\s*>.*?</\s*\1?\s*>   # 成对标签
```[ \t]*(think|thinking|reasoning|thought|...)[ \t]*\n.*?```                                          # 代码块形式
<\s*/\s*(think|thinking|...)\s*>                                                                      # 落单结束标签
<\s*(think|thinking|...)\s*>.*$                                                                       # 未闭合片段（strip_unclosed）
```

## 怎么看思考过程

思考内容默认**只写日志、不发到聊天里**（`hide_reasoning_display` 管的是"发给用户"，`log_reasoning` 管的是"写日志"）。
带 `<think>` 的一轮结束后，`backend.log` 里会出现这样一段：

```
[18:12:03.123] [astrbot_plugin_think_prefix] [INFO] [astrbot_plugin_think_prefix.main:641]: 
[think-prefix] ===== 思考过程开始（deepseek/deepseek-flash, 第 1 段，842 字）=====
用户想要一个等差数列求和公式的推导。先确认首项和公差……
（模型的完整思考）
[think-prefix] ===== 思考过程结束 =====
```

Windows 下查日志：

```powershell
# 只看思考过程（含上下文各 2 行）
Select-String "C:\Users\21739\.astrbot\logs\backend.log" -Pattern "think-prefix" -Context 1,3 |
  Select-Object -Last 40
```

几点说明：

- 流式输出时，chunk 里的 `reasoning_content` 是**增量**，插件会先累积、等整轮结束再一次性写一条完整日志，
  不会把思考切成几十行碎片；如果最终响应没带思考内容（部分中转站会丢），会在 `after_message_sent` 兜底补记。
- 工具调用循环里每一步的思考会各写一段，用「第 N 段」区分。
- 日志级别需要是 `INFO` 或更低（WebUI → 配置 → 日志级别）。AstrBot 自己的 `display_reasoning_text`
  和它内置的 debug 日志都不需要开。

## 已知限制

- **流式输出**：如果服务商把思考内容混在正文里逐字吐出（而不是放在 `reasoning_content` 字段），
  流式片段可能已经先发到消息平台，正则只能拦住还没发出的部分。想要严格干净，建议在 WebUI 里
  关闭 `provider_settings.streaming_response`，或者使用会把思考放在 `reasoning_content` 的提供商
  （DeepSeek、Claude、Gemini 等，AstrBot 本身就会把思考与正文分开）。
- **思考过程单独发送**：AstrBot 的 `provider_settings.display_reasoning_text` 打开时会把思考
  作为单独消息发出。本插件触发的那一轮会用 `enable_reasoning=False` 覆盖掉（`hide_reasoning_display`），
  未触发的轮次仍按你的全局设置走。
- **提供商实例是全局共享的**：同一时间另一个会话发起请求时，可能也会短暂带上思考参数。
  还原只针对发起补丁的那个会话，触发轮本身不受影响（`always_strip` 默认开启，思考内容照样会被清理）。
- **回退提供商**：一轮对话中途切到 fallback provider 时，补丁只作用于主提供商。
- 插件只改「发给模型的请求」和「发给用户的消息」，不会破坏 AstrBot 保存的历史结构。

## 本地自测

## 目录结构

```
astrbot_plugin_think_prefix/
├── main.py            # 插件本体
├── metadata.yaml      # 插件元数据
├── _conf_schema.json  # WebUI 配置面板
├── README.md
├── LICENSE
├── .gitignore
└── tests/
    └── selftest.py    # 桩模块自测，不需要装 AstrBot 就能跑
```

## 本地自测

`tests/selftest.py` 用桩模块模拟了 AstrBot 的事件 / 请求 / 提供商对象，
可以在不启动 AstrBot 的情况下跑一遍全部逻辑（触发、三家提供商的开思考、正则清理、
会话级还原、思考日志的增量累积与截断）：

```bash
python tests/selftest.py
```

当前 34 项断言全部通过。

## 开源协议

MIT License，见 [LICENSE](LICENSE)。
