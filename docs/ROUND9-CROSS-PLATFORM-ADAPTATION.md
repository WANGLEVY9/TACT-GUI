# 第九轮：跨平台环境适配工程记录

2026-11-25。本轮只做研究范畴界定与环境适配，不新增设备试验、不改动既有 Android 归档结果、不产生效果结论。

## 改动范围

- 新增 [跨平台研究范畴与范围限定](../research/跨平台研究范畴与范围限定-2026-11-25.md)：明确 Android GUI 与 WebUI 的纳入/排除、平台内与跨平台分析口径、统一预算/判定契约、版本冻结清单与最小推进顺序。
- 抽出 [ui_nodes.py](../src/budgetmas/ui_nodes.py)：平台无关的可见节点字段、标签定位、结构指纹、oracle 判定与决策前聚合特征。Android 的 `pilot_device.py` 改为复用该契约，旧冻结结果可重算。
- 新增 [environments.py](../src/budgetmas/environments.py)：`AndroidCheckpoint`、`BrowserCheckpoint`（Playwright，可选依赖）与 `FixtureCheckpoint`（纯内存 Web fixture）。三者共享初态恢复、截图、受限动作、可见节点、结构指纹和判定接口。
- [mechanism.py](../src/budgetmas/mechanism.py) 按 `protocol.platform` 选择后端；未写 `platform` 的旧 Android 协议继续读取冻结 ADB 清单。Web 协议可用 `--fixture` 离线运行，或用 `--device-map` 接 Playwright。`trajectory_audit.py` 通过兼容包装复用同一契约。
- 新增 `configs/web-mechanism-smoke.json`、控制台 `web-smoke` profile、`fixture` 设备选项、`[web]` 可选依赖，以及 `tests/test_environments.py` 与控制台 Web 运行测试。`offline.py` 提供零凭据的确定性 provider：CLI `--fixture` 与控制台都通过它结算模型调用，不发起任何外部模型请求。

## 可执行验收

在本机 Python 3.12 下 69 项 unittest 通过（含原有 62 项）。离线 Web 协议无需 ADB、浏览器、密钥或网络：三个检查点各跑直接续跑、自我复核、独立诊断，全部完成初态核验与独立重放，账本无未决预留。控制台的 Web fixture profile 可在无设备时生成同样结构的报告。README 给出复现命令。

```sh
PYTHONPATH=src python -m budgetmas.mechanism configs/web-mechanism-smoke.json --fixture --out results/web-fixture-smoke
```

## 明确限制

Web 的离线 fixture 是工程验收资产，不是 benchmark、不是真实站点、不是论文效果证据；Playwright 后端尚未在真实冻结站点上运行，浏览器版本/网络规则/页面构建摘要尚未纳入正式冻结清单。Android 域的 Round 3–8 结论不变：学习式选择与协作角色分工的额外收益仍未被证实。两个平台各自都未完成 24 个配对检查点筛查，也没有新应用家族上的前瞻性在线对照。

下一道门槛是按平台分别冻结本地 Web 站点与许可台账，验证浏览器初态恢复、重放稳定性和预算 caps，再执行机制筛查；不得把本轮适配代码或离线 smoke 成功写成「跨平台有效性已验证」。
