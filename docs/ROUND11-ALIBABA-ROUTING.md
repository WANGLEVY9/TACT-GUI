# 第十一轮：百炼异构模型与额度对照

## 模型选择与报价

按华北 2（北京）目录价，每百万输入/输出 token 计价。小请求使用首档。实验价格估计采用 API 返回用量，未扣免费额度、缓存或合同优惠。模型能力和价格以官方页面为来源，模型是否可用另由本地账户的实际调用确认。

| 角色 | 模型 | 输入 / 输出（元） | 核对状态 |
| --- | --- | ---: | --- |
| 便宜的视觉执行和复核 | `qwen3.7-flash-2026-07-15` | 0.20 / 0.80（≤32K） | 实调，返回精确快照 ID，使用共同点击协议。[官方模型页](https://help.aliyun.com/zh/model-studio/qwen3-7-flash) |
| GUI 专用对照与视觉诊断 | `gui-plus-2026-02-26` | 1.50 / 4.50 | 实调，返回精确快照 ID。[官方模型页](https://help.aliyun.com/zh/model-studio/gui-plus) |
| 便宜的语义复核 | `qwen-flash-2025-07-28` | 0.15 / 1.50（≤128K） | 实调，纯文本输入。[官方模型页](https://help.aliyun.com/zh/model-studio/qwen-flash) |
| 较强的语义复核候选 | `qwen-plus-2025-12-01` | 0.80 / 2.00（≤128K，非思考） | 实调，纯文本输入。[官方模型页](https://help.aliyun.com/zh/model-studio/qwen-plus) |
| 中档通用视觉候选 | `qwen3.8-flash` | 0.80 / 2.70 | 官方目录候选。文档提供服务 ID，当前实验未将其用作锁定快照的对照。[官方模型页](https://help.aliyun.com/zh/model-studio/qwen3-8-flash) |
| 高档视觉与复杂规划候选 | `qwen3.8-max-0902` | 12.00 / 36.00 | 官方目录候选。可用于文本推理或多模态规划，未纳入本轮对照。[官方模型页](https://help.aliyun.com/zh/model-studio/qwen3-8-max) |

通用视觉模型与 GUI-Plus 共同遵循 `[0,999]` 归一化坐标和受限动作协议。提供给文本角色的观察是有限长度的可见标签、任务和操作历史，截图照常留作归档。隐藏断言、原生奖励和重放结果均留在评估侧。由于视觉与文本复核的输入不同，两者之间的比较不能用来识别单纯的模型效应。[百炼视觉理解与定位](https://help.aliyun.com/zh/model-studio/vision/)

## 注册对照

- 固定直接执行比较 GUI-Plus 与 Qwen3.7-Flash，以及同一 GUI-Plus 的 512/128 输出上限。
- 固定 GUI-Plus 比较直接执行与独立复核。每个用例的调用槽保持一致，增加复核会占一个不执行点击的槽。
- 固定 Flash 执行者比较 Flash 与 GUI-Plus 视觉复核；固定 GUI-Plus 视觉复核比较两个执行者。
- 固定 Flash 执行者和相同可见标签，比较纯文本 Qwen-Flash 与 Qwen-Plus。输出上限、观察合同和独立核验保持一致。

Android 预先登记 4 个已知应用内的开发检查点，模型调用上限 88。MiniWoB++ 原生开发池 3 个任务，调用上限 56。随后锁定 `gui_direct`、`flash_direct`、`flash_review`，在此前未参与开发成绩的 3 个原生模板上运行，调用上限 21。该留出来自同一个 benchmark，并非业务站点家族留出。

网关固定请求模型，验证返回模型 ID，并记录推理用量、输入模态、生成上限、结束原因和请求时间。输出因长度截断时，执行器不会应用该响应。账本预留调用和逻辑操作，未决请求继续占用预算。金额按整次请求的输入档计价，是标准目录估算。比较报告以各对照的同初态核验为准。非支配策略只用本次所有臂均可比的检测池计算。

## 结果

表中金额为同一行对应平台任务池的整组标准目录估算，未代表实际账单。

| 配置 | Android 确认 / 估算元 | Web 开发确认 / 估算元 | Web 模板留出确认 / 估算元 |
| --- | ---: | ---: | ---: |
| GUI-Plus 直接 512 | 2/4，0.047118 | 1/3，0.008250 | 1/3，0.009395 |
| Qwen3.7-Flash 直接 512 | 2/4，0.006110 | 3/3，0.000881 | 3/3，0.000755 |
| GUI-Plus 直接 128 | 1/4，0.052253 | 1/3，0.007026 | — |
| GUI-Plus 复核 + GUI-Plus 执行 | 3/4，0.052073 | 2/3，0.005838 | — |
| Flash 视觉复核 + Flash 执行 | 2/4，0.006491 | 3/3，0.000710 | 3/3，0.000742 |
| GUI-Plus 视觉复核 + Flash 执行 | 2/4，0.021697 | 2/3，0.001794 | — |
| 文本 Qwen-Flash 复核 + Flash 执行 | 2/4，0.004728 | 2/3，0.000832 | — |
| 文本 Qwen-Plus 复核 + Flash 执行 | 2/4，0.005225 | 3/3，0.001391 | — |

Android 中两个直接执行模型各通过两个任务，但成功用例不同。固定 Flash 执行者时，用 GUI-Plus 替代 Flash 视觉复核使「从设置主页进入互联网」由未达标变为达标，也使「计时器」从达标变为未达标。两种选择都有相同的执行槽和视觉观察。这提供了一个可追踪的异质性样本，尚不能从各一个重复训练可靠的升级判别器。

仅将 GUI-Plus 直接执行的上限由 512 降至 128，Android 出现 7 次长度截断、少确认 1 个目标，整组估算从 0.047118 元增加到 0.052253 元。128 组产生 11 次请求，512 组产生 10 次。当前执行契约会继续使用剩余调用槽，因而输出上限变小不能保证成功率或轨迹总费用同步下降。两个额度都请求关闭显式思考，费用使用服务器返回的 completion token 总数。

Android 32、Web 开发 24、Web 留出 9 条分支全部取得独立可判定结果。三个执行账本分别结算 86、56、20 次真实调用，无未决预留。汇总核对 162 张关联截图。三组估算合计约 0.23331 元，计入四次可用性探针后约 0.23880 元。模型比较只覆盖这些小样本和固定调用槽。业务站点和新 Android 应用的前瞻性验证仍待采集。

## 复现

先按 README 准备专用 Android 模拟器和冻结清单。设备映射必须覆盖 calculator、settings、clock、files。所有输入和输出目录显式登记，重复试验需新输出目录。

```sh
PYTHONPATH=src python -m budgetmas.mechanism configs/android-model-role-quota.json \
  results/round5-android/manifest-v2.json --device-map path/to/device-map.json \
  --adb-timeout 40 --out results/round11-fresh-android

PYTHONPATH=src python -m budgetmas.miniwob_routing configs/miniwob-model-role-quota.json \
  configs/android-model-role-quota.json results/benchmark-assets/miniwob-v1-1 \
  results/round11-fresh-web

PYTHONPATH=src python -m budgetmas.miniwob_routing configs/miniwob-routing-holdout.json \
  configs/android-model-role-quota.json results/benchmark-assets/miniwob-v1-1 \
  results/round11-fresh-web-holdout

PYTHONPATH=src python -m budgetmas.routing_summary results/round11-fresh-web
```

Web 使用原版 MiniWoB++ v1.1 的固定提交、Playwright 1.49.1、Chromium 131.0.6778.33、新浏览器上下文和原生 raw reward。安装步骤在 [Round 10](ROUND10-FRONTIER-AND-WEB.md)。汇总程序核验模型收据与截图哈希，再生成对应运行目录内的 `routing-summary.json`。
