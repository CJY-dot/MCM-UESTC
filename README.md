# MCM/ICM 校赛模拟赛 · B 题钢包调度

**先找到自己的交付目录 `stages/<阶段>/<A|B|C>/`。** A 在聊天软件发布阶段提示词、基线提交和交付文件名。三人从同一基线开工，分别推送各自的分支，由 A 收齐并合入 `main`。

> **当前进度** · B 题《炼钢厂钢包周转与排产优化问题》已完成 Stage 02 模型收敛，待团队审查。下一阶段按[统一模型](stages/02_modeling/model_spec.md)和[实现契约](stages/02_modeling/implementation_contract.md)开展。[模型交接](stages/02_modeling/MODEL_HANDOFF.md) · [选题决定](stages/01_topic_selection/topic_decision.md)

## GitHub 共享目录树

GitHub 仓库当前公开，仅提交获准公开的内容。`stages/` 存放成员交付和阶段定稿，`A/ B/ C/` 是三个并列目录。Workbench 继续使用根目录中的正式产物。

```text
美赛/
├── README.md
├── .gitignore
├── environment.yml
├── .mathodology/config.json           # Workbench 共享配置
├── docs/分工说明.md                    # 初始分工参考，目录以本 README 为准
├── stages/
│   ├── 01_topic_selection/           # 选题
│   │   ├── A/ B/ C/
│   │   ├── topic_decision.md
│   │   └── validation/               # 短验证的共享代码和证据
│   ├── 02_modeling/                  # 独立交付与模型收敛
│   │   ├── A/ B/ C/
│   │   ├── model_spec.md              # 统一模型
│   │   ├── implementation_contract.md
│   │   ├── convergence_report.md
│   │   └── MODEL_HANDOFF.md
│   ├── 03_implementation/            # 实现与验证
│   │   └── A/ B/ C/
│   ├── 04_paper_plan/                # 论文结构与图表规划
│   │   └── A/ B/ C/
│   ├── 05_paper_draft/               # 论文与图表草稿
│   │   └── A/ B/ C/
│   └── 06_final_review/              # 终稿审查
│       └── A/ B/ C/
├── problem/
│   ├── problem_decomposition.md      # 团队确认的子问题与交付要求
│   ├── questions.json                # Workbench 固定读取的题问清单
│   └── data/README.md                # 复跑时如何取得、放置和核对输入
├── code/                             # 正式实现和绘图脚本
├── experiments/                      # 环境锁、实验配置与复现说明
├── results/                          # 核准的程序输出
├── figures/                          # 正式采用的图表
├── references/                       # 共享的来源核对笔记，按需建立
├── state/                            # 跨阶段决定或核验记录，按需建立
└── paper/
    ├── main.tex                       # 校赛 LaTeX 内部草稿模板
    ├── sections/                     # 正式章节和手写表格
    ├── references.bib
    └── ai-use.md                     # 按提交要求汇总的 AI 使用披露
```

## 本地目录树

本地包含上面的共享结构。下列内容由 [`.gitignore`](.gitignore) 排除。

```text
美赛/
├── AGENTS.md、CLAUDE.md               # 各自的本机指令
├── SETUP_REPORT.md、.setup-backups/   # 安装记录和备份
├── .agents/skills/                    # 本机安装的 Mathodology Skills
├── .codex/agents/                     # 本机安装的 Codex 专业 Agent
├── .claude/                          # 本地 Skills 和设置
├── .mathodology/report.json          # Workbench 检查报告
├── docs/                             # 原题、附件和模板子目录留本地
│   ├── 校赛题目/                      # 现有题包的读取入口
│   └── 校赛word模板/                  # 校赛论文 Word 原件
├── problem/data/
│   ├── raw/                          # 新增原始输入按需使用
│   └── processed/                    # 清洗、联表后可重建的数据
├── paper/
│   ├── results.tex、tables/          # Workbench 生成
│   └── main.pdf 和编译缓存
├── references/downloads/             # 下载资料
├── logs/                             # 本地日志和 AI 使用记录
├── runs/                             # Workbench 运行记录
└── local/                            # 个人试跑和临时文件
```

现有附件继续从 `docs/校赛题目/` 读取。将来如需把正式输入集中到 `problem/data/raw/`，统一迁移并更新引用，避免维护两套附件。Workbench 的输入快照覆盖 `code/` 和 `problem/`，读取 `docs/` 的实验应另外记录相对路径与哈希。

`problem/data/processed/` 用于本机派生数据。B 的预处理会写出带炉次和来源定位的事件、任务及校准表。可共享的输入样例放在对应阶段，并随 Git 交付。详见 [数据复现说明](problem/data/README.md)。

B 题可直接用共享算例复跑确定性短验证。阶段二原型提供合成校准表，用于从干净克隆运行 Q1–Q3。复现实测 Q2 数值时需从授权工作簿重建历史校准表。A 题的代码、配置和已有结果均可取用，重新计算需要原始雷达附件。 [短验证复跑](stages/01_topic_selection/validation/README.md) · [B 交接](stages/02_modeling/B/B_HANDOFF.md)

## 提交与收敛

1. **各自交付。** 以建模阶段的 B 为例，在 `stage02/b` 分支只写 `stages/02_modeling/B/`。交付说明写清结论、验证依据和待确认事项。按路径暂存，用 `git diff --cached --name-only` 核对后推送。
2. **由 A 收敛。** A 用 `git fetch origin` 获取成员分支，在集成分支汇总。统一模型写入 `model_spec.md`，采纳的实现、实验结果和论文内容放入相应正式目录。检查通过后合入 `main`，作为下一阶段的基线。

## 环境与检查

使用 Conda 环境 `mcm-modeling`。首次安装可运行 `conda env create -f environment.yml`。Windows 需要按固定版本复现时，使用 `conda create -n mcm-modeling --file experiments/conda-win-64.lock.txt`。

已有 Windows 环境可用 `conda install -n mcm-modeling --file experiments/conda-win-64.lock.txt` 同步锁定包。读取旧版 `.xls` 时显式使用 `pd.read_excel(path, engine="calamine")`，普通 `.xlsx` 可显式使用 `engine="openpyxl"`。

运行下列检查前，各成员先在本机安装 Mathodology v0.19.0，项目固定提交为 `49c639648c614900e4c3d88e1b6a28ffe3fae5b4`。Workbench 命令使用 `.agents/skills/` 下的脚本。 [固定版本安装指南](https://github.com/sweetcornna/mathodology/blob/49c639648c614900e4c3d88e1b6a28ffe3fae5b4/docs/INSTALL_zh.md)

```text
conda activate mcm-modeling
python -B experiments/smoke.py
python -B .agents/skills/mathodology-workbench/scripts/mtd.py status
python -B .agents/skills/mathodology-workbench/scripts/mtd.py check --no-sync
```

在项目根目录运行。PowerShell 无法激活 Conda 时，用 `&` 和本机解释器的完整路径调用 Python。`smoke.py` 使用临时工作区和合成数据，PDF 检查需要 LaTeX。正式结果更新后由 A 运行 `mtd.py sync`，交付前运行 `mtd.py check --compile` 并核对 PDF。

## 校赛论文

`paper/main.tex` 按校赛 Word 模板组织承诺书、Summary Sheet、目录和正文，使用 XeLaTeX 编译。Workbench 的 `contest` 设为 `null`，保留数值引用、图表和编译等通用检查。需要单独编译 XeLaTeX 时，先用 `mtd.py sync` 生成 `paper/results.tex`。

制作 Word 版时，从 [校赛 Word 模板](<docs/校赛word模板/2027年校赛论文模板.docx>) 的副本开始。逐项核对承诺书原文、B 题编号、真实队号、三位成员的中文姓名和学号、签名和日期，以及摘要页、目录、页眉页码和分页。签名方式和最终提交格式按校方通知核对。
