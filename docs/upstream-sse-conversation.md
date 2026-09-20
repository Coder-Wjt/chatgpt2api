# 上游 Conversation SSE 与图片结果解析

状态：当前

这是内部协议参考，不是公开 API 契约。上游事件格式会变化；公开调用方只应依赖本项目的 OpenAI 兼容 API 和图片任务接口。

## 输入和解析层

上游 Conversation 链路会返回 SSE `data:` 事件。事件可能是版本标记、`[DONE]`、完整 JSON 消息、JSON patch、文本片段或无法解析的原始内容。解析层保留会话 ID、消息事实、文本、工具信号和原始诊断，而不是假设每个事件都是同一种 JSON。

常见线索包括：

| 线索 | 含义 |
| --- | --- |
| `conversation_id` | 后续读取会话或任务的关联键 |
| `p` / `o` / `v` | 上游 patch 的路径、操作和值 |
| `message.author.role` | 消息角色，如 assistant、tool、user |
| `message.status` / `end_turn` | 该消息是否可能是终态 |
| `tool_invoked` | 上游报告本轮使用了工具的线索 |
| `turn_use_case`、`async_task_type`、`message_type` | 工具或使用场景的附加线索 |

这些字段都只是解析事实。`tool_invoked=true` 或 `async_task_type=image_gen` 说明可能需要继续解析或补查，但本身不等于图片已经成功生成。

普通文本流可能先返回 `resume_conversation_token` 和 `stream_handoff`，而没有消息正文。`OpenAIBackendAPI.stream_conversation` 在上游提供 `resume_sse_endpoint` 时，以同一 Session、账号和代理向 `/backend-api/f/conversation/resume`（匿名路径为 `/backend-anon/f/conversation/resume`）发送 `conversation_id` 与 `offset: 0`，并在 `x-conduit-token` 请求头携带续接凭据。第一段交接流的 `[DONE]` 不代表回答完成；续接流重放的控制事件被消费，不会循环续接或重新提交用户消息。续接凭据不传入业务解析器或日志，缺失或不匹配的交接信息返回受控错误。首段对话请求和续接共用 300 秒预算，结束、异常及取消时关闭响应。

## Codex 图片传输

Codex 图片响应通过 `OpenAIBackendAPI` 已配置的 curl_cffi Session 请求 `/backend-api/codex/responses`，复用账号、账号组优先及全局默认兜底的出口选择；图片重试传入备用代理配置时沿用该出口。SSE 复用共享解析器，保留 Codex 终态事件判定；HTTP 错误保留状态码、限量正文与 `Retry-After`。

## 图片成功判定

图片成功必须得到可用的输出资产。Conversation SSE 只会从受信任的工具消息和 patch 上下文收集有效的 `file_` / `sediment://` 输出指针，随后由后端解析和下载资源；仅仅看到输入附件或工具信号不能当作输出图片。`data:image/...`、base64 或直接结果 URL 属于独立的 Codex 图片响应路径，不能当作一般 Conversation SSE 的结果规则。

SSE 未携带完整结果时，后端会根据已有的 `conversation_id`、任务事实和流状态继续读取会话或图片任务，再决定是否有输出资产、文本结果或失败。这个补查过程属于后端协议层；Studio 和其他页面不自行轮询上游 Conversation。

## 文本、JSON 和失败

普通文本调用通过共享的 `stream_text_deltas` 处理 Chat Completions、Responses 和 Messages 请求。`-wm` 工作模式在初次选择及鉴权重试时跳过已知免费账号；未知订阅类型仍由上游判定，不能仅凭付费类型保证模型可用。没有合适账号时返回 `no_available_account`（503）。

文本调用使用 `TextGenerationError` 输出 HTTP/SSE 错误和调用记录。上游 `work_subscription_required` 保留为订阅权限错误（403），不作为失效凭据触发鉴权重试。HTTP 200 的 SSE 内结构化错误同样会被检查；流结束但没有任何文本增量时返回 `empty_upstream_response`（502），不会生成正常结束结果或记录为成功。已经输出文本后发生错误时发送流式错误，不切换账号重放内容。

没有有效图片资产时，终态 assistant 的普通文本 / 代码内容会分类为 `upstream_text_reply`，按 HTTP 400 的图片文本结果返回。它不是账号失败，也不会触发账号切换。

如果终态内容或任务事件包含结构化错误、明确失败码、工具 `system_error`、限流、鉴权失效或审核信号，后端通过 `ImageFailure` 归类为相应失败。参数 JSON 或其他结构化 JSON 只有在它表达图片工具异常时才会成为 `image_tool_error`；不能因为内容“看起来像 JSON”就把正常结果判错。

当既没有图片结果、也没有可展示文本或明确失败证据时，后端返回受控的空结果 / 上游错误，而不是让前端从 SSE 片段猜测。

## 诊断与边界

图片执行使用 `ImageFailure` 产生对外错误、上游错误、上游文本和结构化失败字段。API 和账号切换使用当前分类；日志、监控和历史尝试使用这些持久化字段生成兼容投影。详情见 [`image-failure-handling.md`](image-failure-handling.md)。

修改解析规则时必须同时覆盖：SSE 事件、完整会话补查、图片任务补查、文本终态、结构化错误终态、限流、鉴权和多图部分成功。不要在前端增加第二套判断逻辑。
