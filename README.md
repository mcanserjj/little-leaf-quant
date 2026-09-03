# 小树叶炒股模拟器 / Little Leaf Quant

一个面向 A 股的本地研究与模拟交易工作台。项目把规则选股、八组策略、五仓模拟、策略联赛、历史回测、AI 复盘和数据源管理集中在一个简洁的 Web 界面中。

> [!IMPORTANT]
> 本项目仅用于学习、研究、回测和模拟交易，不连接任何券商账户，不会提交真实委托，也不构成任何投资建议、收益承诺或证券推荐。市场有风险，所有研究结论与模拟结果都需要使用者独立核验并自行承担风险。

## 项目特点

- 完全独立的 FastAPI + React Web 应用，不依赖其他项目的页面或扩展机制。
- 短线 `S-A`～`S-D`、长线 `L-A`～`L-D` 共八组策略，每组最多五个模拟仓位。
- 规则选股与 AI 解读分离：确定性筛选不依赖大模型，资讯研判和复盘可使用 DeepSeek。
- 候选股票保留到下一次主动运行选股，不会因刷新页面自动清除。
- 按策略计算预期买入区间；模拟执行等待现价进入区间，而不是固定在开盘买入。
- 模拟 A 股 T+1、100 股整数手、涨跌停、停牌、佣金、过户费、印花税和可选滑点。
- 支持“本轮信号价成交”和“下一次快照成交”，可在复盘中比较成交偏差。
- 持仓行情可按 5 秒刷新，策略按 10 分钟评估；数据不可信时停止撮合。
- 支持周度策略联赛、快速/深度 AI 复盘、文档归档、候选策略回测和人工批准。
- 数据缺失时关闭对应策略门禁，不用历史收盘价冒充实时行情，不以当前值回填历史值。
- API Key 在 Web 中录入、测试和清除；Windows 下优先使用 DPAPI，失败时仅保存于进程内存。

## 页面

| 页面 | 主要用途 | AI 是否必需 |
| --- | --- | --- |
| 五仓模拟 | 八组收益、仓位、现价、买入价、成交历史、卖出原因和执行状态 | 否；实时行情需要 HiThink Key |
| 选股研究 | 八组规则选股、数据门禁、候选评分和预期买入区间 | 基础选股不需要；资讯分析需要 |
| 联赛与复盘 | 组间排名、归因、回测、AI 复盘档案和待批准策略版本 | 排名与回测不需要；AI 解说需要 |
| 数据与设置 | 数据覆盖率、后台更新、资讯同步和密钥管理 | 取决于数据源 |

## 策略分组

| 组别 | 研究方向 | 主要数据门禁 |
| --- | --- | --- |
| S-A | 短线趋势与量价延续 | 日 K、均线、动量、流动性 |
| S-B | 短线均值回归 | 日 K、波动和价格偏离 |
| S-C | 短线事件与资讯 | 带来源和发布时间的公告/资讯证据 |
| S-D | 短线强弱与风险约束 | 日 K、成交活跃度和风险过滤 |
| L-A | 长线质量与估值 | 最新 ROE、经营现金流、当前 PE TTM |
| L-B | 长线分红 | 财务数据和近 12 个月已实施分红 |
| L-C | 长线 ROE 稳定性 | 至少两期、建议八期可核验 ROE |
| L-D | 长线综合质量 | 财务质量、估值和趋势确认 |

股票池默认排除 ST、退市风险、科创板、创业板和北交所股票。缺少策略必需字段的公司不会进入候选。

## 数据源与降级原则

| 数据 | 首选来源 | 免费补充/降级 | 处理原则 |
| --- | --- | --- | --- |
| 股票列表、日 K、财务、当前行情 | HiThink Financial API | BaoStock 可覆盖部分历史字段 | 页面录入 Key；不可用时保留旧数据并显示错误 |
| 历史 PE | BaoStock | 禁止用当前值倒填 | 按股票和交易日保存，缺失即排除 |
| 公告与场外资讯 | 巨潮资讯网公开查询 | 本地旧快照 | 保存来源 URL、发布时间、原始 JSON 和内容哈希 |
| AI 研判与复盘 | DeepSeek | 无 AI 时仍可运行规则选股和基础排名 | 只提交已取得的数据和缺失清单 |

本仓库不包含下载后的行情、财务、估值、公告 PDF、AI 复盘、模拟持仓或成交记录。首次启动后请在“数据与设置”页面按需同步。第三方接口的可用性、字段、限频和授权范围可能变化，使用者应遵守对应服务条款。

## 技术架构

```text
浏览器（React + TypeScript）
            │ /api
            ▼
FastAPI ── 选股引擎 / 模拟执行 / 回测 / AI 复盘
            │
            ├── HiThink Financial API
            ├── BaoStock
            ├── 巨潮资讯网公开查询
            ├── DeepSeek API
            └── 本地 JSON / Parquet 数据
```

后端对数据来源、时间水位和缺失字段执行门禁；前端只展示后端确认过的状态。策略版本以 JSON 留存，AI 修改先成为候选版本，完成回测并人工批准后才影响后续新开仓。

## 环境要求

- Windows 10/11（快捷启动脚本和 DPAPI 针对 Windows）
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+
- [pnpm](https://pnpm.io/) 9

## 安装与启动

```powershell
git clone https://github.com/mcanserjj/little-leaf-quant.git
cd little-leaf-quant\backend
uv sync --extra dev

cd ..\frontend
pnpm install
pnpm build

cd ..
.\web-service.bat
```

浏览器会打开 <http://127.0.0.1:8011>。以后可直接双击 `web-service.bat` 启动。

停止或查看状态：

```powershell
.\web-service.bat stop
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\web-service.ps1 status
```

如果端口 `8011` 已被其他程序占用，脚本会拒绝误停无关进程并给出错误。

## 首次使用

1. 打开“数据与设置”，按需录入 HiThink Financial API Key 和 DeepSeek API Key。
2. 点击“测试连接”，确认页面显示实际采用的密钥保存方式。
3. 同步股票列表、日 K、财务、估值和资讯；以覆盖数量和进度判断完整性。
4. 打开“选股研究”，选择数据截止日并运行。门禁未通过的组会列出缺失数据。
5. 在交易时段保持服务运行。候选现价进入所属策略买入区间后，系统才可能生成模拟成交。
6. 在“联赛与复盘”运行历史回测或 AI 复盘；策略改动必须人工批准才生效。

## API Key 与隐私

- 源码没有内嵌任何用户 API Key。
- Key 不写入浏览器本地存储、普通配置文件或日志。
- DPAPI 可用时，密钥加密保存在当前 Windows 用户上下文；失败时只保存在服务进程内存。
- Web 页面支持测试连接和安全清除。
- 密钥、运行日志、缓存和研究结果均由 `.gitignore` 排除。

公开部署前请自行增加身份认证和 HTTPS。本项目默认仅监听 `127.0.0.1`，不应直接暴露到公网。

## 验证

```powershell
cd backend
uv run pytest -q

cd ..\frontend
pnpm build
```

这些测试验证程序逻辑和构建，不代表策略具有未来盈利能力，也不等同于真实交易所、数据供应商或券商环境验证。

## 项目结构

```text
little-leaf-quant/
├── backend/                 FastAPI、选股、执行、回测、AI 与测试
├── frontend/                React Web 界面
├── data/user_data/
│   └── research_league/     可公开的初始策略与模拟规则 JSON
├── scripts/                 Windows 启停和数据迁移脚本
├── web-service.bat          一键启动/停止入口
├── LICENSE                  MIT License
└── README.md
```

除 README 外，内部规划、研究过程和规则类 Markdown 文档不在公开仓库中。公开仓库只保留运行所需源码、测试、静态资源、初始 JSON 配置和许可证。

## 已知限制

- 仅实现研究和模拟成交，没有券商接口或真实下单能力。
- 自动撮合依赖可核验且未过期的实时行情；BaoStock 不能完整替代实时行情。
- 免费数据源可能限频、变更字段或临时不可用。
- 回测高度依赖数据质量、复权、成交假设、费用和样本区间，存在过拟合与幸存者偏差。
- 交易日历只在代码明确覆盖的年份运行；未知年份默认停止撮合。
- 巨潮资讯网公开查询接口没有稳定性承诺，失败时不会伪造或补写资讯。

## 许可证与联系

本项目采用 [MIT License](LICENSE)。欢迎提交 Issue，意见和建议也可发送至 <mcanserjj@gmail.com>。

---

## English

Little Leaf Quant is a local A-share research and paper-trading workbench. It combines rule-based screening, eight strategy groups, five-slot simulated portfolios, strategy leagues, backtesting, AI-assisted reviews, and data-source management in one Web UI.

> [!IMPORTANT]
> This project is for education, research, backtesting, and simulation only. It does not connect to brokerage accounts or place real orders. Nothing in this repository is investment advice, a security recommendation, or a guarantee of profit.

### Highlights

- Four short-horizon groups (`S-A`–`S-D`) and four long-horizon groups (`L-A`–`L-D`).
- Up to five simulated positions per group with A-share T+1, board-lot, price-limit, suspension, fee, and optional slippage constraints.
- Deterministic screening works without AI; DeepSeek is optional for news interpretation and review narratives.
- Strategy-specific entry ranges, 5-second quote refresh, and 10-minute evaluation intervals.
- Current-snapshot and next-snapshot execution assumptions.
- Fail-closed data gates: missing valuation, financial, status, timestamp, or event evidence excludes the candidate instead of fabricating a value.
- Versioned strategy JSON, historical backtests, AI-generated candidate revisions, and manual approval before activation.
- Web-managed HiThink and DeepSeek keys with Windows DPAPI or process-memory fallback.

### Quick start

Requirements: Windows 10/11, Python 3.11+, uv, Node.js 20+, and pnpm 9.

```powershell
git clone https://github.com/mcanserjj/little-leaf-quant.git
cd little-leaf-quant\backend
uv sync --extra dev
cd ..\frontend
pnpm install
pnpm build
cd ..
.\web-service.bat
```

Open <http://127.0.0.1:8011>. Downloaded market/financial/news data, simulated portfolios, trades, reviews, caches, logs, and secrets are intentionally excluded. Populate local research data from Data & Settings.

Run `uv run pytest -q` in `backend` and `pnpm build` in `frontend`. Passing tests verifies software behavior only; it does not validate profitability or real-market execution.

Licensed under the [MIT License](LICENSE). Feedback: <mcanserjj@gmail.com>
