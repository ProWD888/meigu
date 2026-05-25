# meigu — 美股收盘日报自动化系统

每天北京时间 **08:00** 自动生成一份《美股收盘日报》，覆盖指数、宏观、利率、板块、主题、个股、机构观点、风险与次日交易计划等 15 个维度。

报告由两部分组成：

1. **结构化数据抓取**（`scripts/fetch_market_data.py`）—— 从 Yahoo Finance、FRED、美国财政部、CME FedWatch 等公开来源抓取最新行情与宏观数据，输出为 `data/latest.json`。
2. **LLM 分析合成**（`scripts/generate_report.py`）—— 将上一步的数据 + `prompts/daily_report_prompt.md` 提示词模板送给 LLM（OpenAI 或 Anthropic），生成完整的 Markdown 日报，写入 `reports/YYYY-MM-DD.md`。

GitHub Actions（`.github/workflows/daily-report.yml`）会按计划每天运行整套流水线，并把生成好的报告自动提交到仓库的 `reports/` 目录。

---

## 目录结构

```
meigu/
├── prompts/
│   └── daily_report_prompt.md      # 给 LLM 的提示词模板（用户定义的 15 段结构）
├── scripts/
│   ├── fetch_market_data.py        # 数据抓取（yfinance / FRED / Treasury / FedWatch）
│   └── generate_report.py          # 调用 LLM 生成最终 Markdown
├── data/
│   └── latest.json                 # 最近一次抓到的结构化数据快照（每次运行覆盖）
├── reports/
│   └── YYYY-MM-DD.md               # 每日生成的报告
├── .github/workflows/
│   └── daily-report.yml            # 定时触发
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 快速开始（本地运行）

### 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 仅抓取数据（不调用 LLM）

```bash
python scripts/fetch_market_data.py --output data/latest.json
```

会在 `data/latest.json` 里看到所有抓到的指数、利率、板块 ETF、重点个股、商品、Fed 数据等。

### 3. 生成完整报告

需要先准备 LLM API key。**两种方式择一**：

**A. 使用 OpenAI / OpenAI 兼容端点**

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o            # 可选，默认 gpt-4o
# 如果走自部署 / 第三方代理，可设置：
# export OPENAI_BASE_URL=https://your-proxy.com/v1
```

**B. 使用 Anthropic Claude**

```bash
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL=claude-sonnet-4-5   # 可选
```

**C. 使用 Google Gemini**

```bash
export LLM_PROVIDER=gemini
export GEMINI_API_KEY=...                  # 也接受 GOOGLE_API_KEY
export GEMINI_MODEL=gemini-2.5-pro         # 可选
```

> 申请免费 Gemini API key：https://aistudio.google.com/apikey

然后：

```bash
python scripts/generate_report.py --data data/latest.json --output reports/$(date +%Y-%m-%d).md
```

也可以一步到位：

```bash
python scripts/fetch_market_data.py --output data/latest.json \
  && python scripts/generate_report.py --data data/latest.json
```

不传 `--output` 时，`generate_report.py` 会自动写到 `reports/<对应交易日>.md`。

---

## GitHub Actions 自动化

工作流位于 `.github/workflows/daily-report.yml`，按以下规则运行：

- **定时触发**：UTC `00:00`（= 北京时间 `08:00`）每日运行一次。
- **手动触发**：在仓库的 Actions 页面点击 *Run workflow* 即可。
- 运行步骤：
  1. 安装 Python 依赖
  2. 跑 `fetch_market_data.py`，得到 `data/latest.json`
  3. 跑 `generate_report.py`，得到 `reports/YYYY-MM-DD.md`
  4. 通过 `git commit + push` 把 `data/latest.json` 与新报告提交回主分支

### 必须配置的 Secrets

在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中添加：

| Secret 名称 | 是否必需 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | 必需 | `openai` / `anthropic` / `gemini` |
| `OPENAI_API_KEY` | 当 provider = openai 时必需 | OpenAI / 兼容服务的 API key |
| `OPENAI_MODEL` | 可选 | 默认 `gpt-4o` |
| `OPENAI_BASE_URL` | 可选 | OpenAI 兼容代理地址 |
| `ANTHROPIC_API_KEY` | 当 provider = anthropic 时必需 | Claude API key |
| `ANTHROPIC_MODEL` | 可选 | 默认 `claude-sonnet-4-5` |
| `GEMINI_API_KEY` | 当 provider = gemini 时必需 | Gemini API key（[免费申请](https://aistudio.google.com/apikey)） |
| `GEMINI_MODEL` | 可选 | 默认 `gemini-2.5-pro` |
| `FRED_API_KEY` | 可选 | 抓取 FRED 数据时使用，留空会跳过 FRED 部分 |

> 注：`fetch_market_data.py` 不依赖任何 API key 也能跑（yfinance / 美国财政部 / CME 都是公开数据），只是 FRED 部分会留白。

---

## 提示词模板

`prompts/daily_report_prompt.md` 是这套系统的核心，定义了报告的 15 段结构（一句话总结 / 大盘表现 / 盘中复盘 / 宏观环境 / 板块 / 主题 / 宽度 / 技术面 / 个股 / 财报 / 机构观点 / 板块轮动 / 关注股 / 次日计划 / 风险 / 最终结论）。

修改提示词不需要改代码，直接改 `prompts/daily_report_prompt.md` 即可。

---

## 数据来源说明

| 数据类别 | 主要来源 | 备注 |
| --- | --- | --- |
| 指数行情（SPX / NDX / DJI / Russell / SOX / VIX） | yfinance | 收盘价、涨跌、成交量 |
| 美债收益率 | yfinance（^IRX/^FVX/^TNX/^TYX）+ 美国财政部 XML 备用 | 2Y / 5Y / 10Y / 30Y |
| 商品（金 / 油 / 比特币） | yfinance（GC=F、CL=F、BZ=F、BTC-USD、^DXY） | — |
| 11 板块 ETF + 主题 ETF | yfinance | XLK/XLE/XLF/XLV/XLY/XLP/XLU/XLB/XLI/XLC/XLRE + SMH/IGV/IWM/QQQ/SPY/RSP 等 |
| 重点个股 | yfinance | 七巨头、AI 硬件、软件、电力链 |
| 经济数据 / FRED | FRED API（可选） | CPI / PCE / 失业率等 |
| FedWatch 利率概率 | CME FedWatch 公开页面 | 容错降级：抓不到则在报告中标注"暂无可靠数据" |

> **重要**：`fetch_market_data.py` 不会捏造数据。任何抓不到的字段都会被显式标记为 `null`，提示词里要求 LLM 在这种情况下写"暂无可靠数据"，而不是自由发挥。

---

## 免责声明

本项目生成的内容仅用于市场研究与个人复盘，不构成任何投资建议。所有数据请在做出实际交易决策前到原始来源二次核对。
