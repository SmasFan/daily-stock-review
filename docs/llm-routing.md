# LLM 决策路由（2026-11-08 起）

## 结论依据

`2026-11-08` 用 3 张卷 × 3 个模型（本地自动分派组、本地 27B 直连、云端 ds4.1）
跑了 9 次请求，结果在 `/tmp/llm_compare_results.json`：

| 决策类型 | 本地结论 | 云端 ds4.1 结论 | 采用 |
| --- | --- | --- | --- |
| A股 gate（7 条买入复核） | 7/7 allow | 7/7 allow | **纯本地**（结论一致，理由稍简） |
| 币安 bn_trade（是否开仓） | hold / 0 仓位 | hold / 0 仓位 | **纯本地**（结论一致） |
| 宏观 macro（长上下文 + 联网简报） | 中性 55 / 均衡，2 个板块，把 11/5 纳指大跌误写成 11/3 | 防御 42 / 防守，5 个板块，引用 678涨4633跌、创业板 -3.96%、PMI 49.3、地产 -18.1% | **云端 ds4.1** |

延迟：ds4.1 `2.6–5.2s`；本地 27B `33.6–63.6s`（盘中 30s 闸门等不起）。
因此短决策默认用 `qwen3-coder:30b`（~69 tok/s），不用 27B。

## 路由表

配置文件：`config/llm_routing.json`，实现：`llm_router.py`。

| kind | 用途 | model | tier | web |
| --- | --- | --- | --- | --- |
| `macro` | 宏观消息面 | `workbuddy/deepseek-v4.1-flash` | cloud | 是 |
| `gate` | A股盘中成交前复核 | `group/local-code` | local | 否 |
| `bn_gate` | 币安成交前复核 | `group/local-code` | local | 否 |
| `candidates` | 模拟盘候选股批量评审 | `group/local-code` | local | 否 |
| `stock_news` | 6 股池个股消息面 | `group/local-code` | local | 否 |
| `bn_review` | 币安事后复盘 | `group/local-code` | local | 否 |

- 未知 kind 一律按本地短决策处理，**绝不出网**。
- Magpie 不可达 → 直连 `ollama/qwen3-coder:30b`（纯本地）。
- 宏观云端失败 → 本地初稿，backend 显式标 `local-draft: ...`，不会伪装成正式判断。
- 每次调用写 `data/llm_route_log.jsonl`（kind/model/served/backend/耗时/成功与否）。

## 宏观「联网」

模型本身不联网。`web_brief.py` 把联网抓到的数据拼成带时间戳的【联网简报】注入
`macro_llm.py` 的 prompt：

1. `data/external_data.json` — `build_external.py` 每 3 小时抓的美股/纳指/VIX/板块偏好/
   风险偏好（launchd `com.smas.dsr.external`）；超过 24h 标注「可能过期」。
2. `src.macro.fetch_news_international(page_size=30)` — 新浪国际财经，实时抓 16 条。

`SYSTEM` 已要求优先引用简报里的具体数据（指数涨跌幅、VIX、PMI、板块偏好）并在
`summary` 里点明。`data/macro_llm_data.json` 里 `route.web_meta` 记录本次简报是否成功、
外部因子时间戳、国际新闻条数。人工检查：`python3 web_brief.py`。

## 人工接管（Agent 文件队列）

`data/llm_queue/pending/` 只保留「人工预置答案」能力，不再空等：

- 各类决策优先读 `done/` 里已有答案（确定性 id，同问题同日复用）。
- `wait=0`：登记问题但不等待，立即由固定路由兜底，不卡定时任务。
- 宏观默认连登记都跳过（`data/llm_queue/pending/` 此前堆了大量无人应答的 macro 提问）；
  `DSR_MACRO_AGENT=1` 可恢复。
- 付费 commandcode 云端仍是最后兜底，`AGENT_LLM_ALLOW_CLOUD=1` 才开（默认关，且无 key）。

## 开关

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `DSR_LLM_LOCAL_ONLY` | `0` | `1` 时连宏观也强制纯本地（`group/local-all`），断网/隐私场景 |
| `DSR_MACRO_MODEL` | — | 覆盖宏观模型 |
| `DSR_DECISION_MODEL` | — | 覆盖五个短决策模型 |
| `DSR_LOCAL_MODEL` | `qwen3-coder:30b` | Magpie 挂掉时直连 ollama 的模型 |
| `DSR_MAGPIE_URL` | `http://127.0.0.1:3425/v1` | Magpie 端点 |
| `DSR_MACRO_AGENT` | `0` | 恢复宏观的人工队列登记 |
| `BN_LLM_GATE_ROUTER` | `1` | 币安成交前复核是否走本地模型 |
| `BN_LLM_GATE_WAIT` | `0` | 币安 gate 等队列秒数 |
| `BN_REVIEW_ROUTER` | `1` | 币安复盘是否走本地模型 |
| `BN_REVIEW_WAIT` | `0` | 币安复盘等队列秒数（原为 2100） |
| `AGENT_LLM_WAIT_CANDIDATES` | `0` | 模拟盘候选评审等队列秒数 |

`binance-llm-bot/.env` 已写入上述开关；`config/llm_routing.json` 也可用文本直接改模型。

## 接线点

| 文件 | 改动 |
| --- | --- |
| `macro_llm.py` | macro → 云端 ds4.1 + 联网简报；stock_news → 本地；`out.route` 记录路由与简报元数据 |
| `sim_live.py` | `gate` / `candidates` 先走 `llm_router`，失败退 ollama 直连 |
| `binance-llm-bot/llm_gate.py` | 新增 `_call_local()`，队列 → 本地模型 →（可选）云端 |
| `binance-llm-bot/review.py` | 新增 `_review_local()`，队列等 35 分钟改为 0 |
| `binance-llm-bot/trader100.py` / `trader_short.py` | `load_dotenv()` 提前到 `import llm_gate` 之前，`.env` 开关才生效 |
| `scripts/mac/dsr.py` | `DEFAULT_ENV` 补 `DSR_LOCAL_MODEL` / `DSR_MACRO_AGENT` |

## 验证

```bash
python3 llm_router.py                 # 打印当前路由表
python3 web_brief.py                  # 打印联网简报
python3 -m unittest discover -s tests
cd binance-llm-bot && python3 -m unittest discover -s tests
```

测试覆盖：`tests/test_llm_router.py`（路由分流 / 配置覆盖 / 强制纯本地 / 云端失败降级）、
`binance-llm-bot/tests/test_llm_gate.py`（本地通道接线 / 熔断 / 开关）。
