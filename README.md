# 九江银行金融科技专项赛 · 仓库总览

项目已整体迁移至本目录（旧 `D:\金融科技专项赛` 已为空壳并删除）。目录分工：

| 目录 | 角色 |
|---|---|
| `app\` | **开发树（唯一真源）**：改代码只改这里，含全部诊断 / E2E 脚本与工具链 |
| `01_交付包\` | 正式提交物：方案书.docx、路演5min.pptx、`本地运行包\`（交付快照）、模拟数据 |
| `deploy\` | 服务器副本，由 `app\scripts\deploy_to_server.py` 推送 |
| `00_赛务资料\` | 赛事方案、报名表、141 号文等官方文件 |
| `02_工作区\` | 爬虫 `jjccb_crawl\`、行内数据、各类 `_*.py` 补丁脚本、备份 |
| `05_报告\` | 审计 / 扫描 / 修复类报告 |
| `06_会话复原\` | 旧会话解压复原存档 |
| `99_临时\` | 一次性脚本与探针，随时可删 |
| `_archive\` | 历史版本归档（各期 docx / pptx 快照） |
| `_fig_*.svg`、`烽火_*.png` | 方案书与 PPT 的图源 |

## 三副本同步规则

1. `app\` → `01_交付包\本地运行包\`：跑 `app\scripts\_sync_pkg_from_app.py`，同步后必须
   `verify_package.py` 全绿再对外提交。
2. `app\` → `deploy\`：跑 `app\scripts\deploy_to_server.py`。
3. 同步后允许存在的三处交付侧差异，**不要当 bug 修掉**：
   - `本地运行包\README.md`：面向交付读者，多两行前端云守卫说明（比 app 版新）；
   - `本地运行包\data\source_config.json`：已脱敏版（凭据只在 `.env`）；
   - `本地运行包\scripts\ui_verify_deepseek.js`：多云守卫断言版。

## 日常校验（四道门禁，改动后全跑）

```bash
cd app
python -m unittest discover -s tests            # 19 项
python scripts/verify_security.py               # 25 项
python scripts/verify_package.py                # 开发树 13/13（凭据残留按 WARN 降级不计数，系设计）
cd ..\01_交付包\本地运行包
python scripts\verify_package.py                # 交付树 14/14
python scripts\recheck_sensitivity.py           # 36 组 792 次重算，等级翻转应 ≤ 1
```

「中数通」供应商在离线环境会被敏感性脚本跳过（外部源取不到，脚本自行打跳过标记），不是缺陷。

## 凭据安全

- `.env`（app / 本地运行包 / deploy 各一份）与根目录 `.github_token` 均在 `.gitignore` 内，
  不得写入文档、不得入库、不得入交付包明文。
- 交付快照的 `source_config.json` 保持脱敏版。

## 打包注意

- `__pycache__` 每次跑门禁都会重新生成，删除放在所有校验之后的最后一步。
- 本仓库非 git 管理（仅 `.gitignore` + `.github_token` 备用）；如需入库先确认 ignore 生效。
