# 第四轮：统一预算与真实 GUI 验收

日期：2026-11-23。

## 验收目标与结果

1. **GUI 与通用角色调度共用账本：通过。** Scope 在同一 SQLite 中提供 trial/role/call 额度分层。父级总预留期间不会再次扣除子调用支出；正常关闭逐级结算，异常与未决调用保留父承诺。TeamExecutor 原回调仍兼容，真实角色通过 quota.ledger 使用受限子账本。
2. **真实 APK、设备、阿里 GUI 底座：通过工程验收。** 创建独立 BudgetMASAcceptance AVD（Android 34、google_apis、arm64-v8a，emulator-5580），未修改其他项目已有 AVD。实际调用 gui-plus-2026-02-26，没有复制密钥或公开私有 endpoint。
3. **独立结果与重放：通过工程验收。** 官方开源 Simple Calculator 5.12.0，任务 2+3=5；主执行后的 UIAutomator result 文本检查通过；清理应用数据后重放两次，文本检查也均通过。任务完成不等于发现缺陷。
4. **统一账单与失败记录：通过。** 成功批次 limits 为 8 model_calls 和 48 device_steps；实际 5 和 29；未用额度为 3 和 19；无未决顶层预留。

## 来源与环境

- [上游项目](https://github.com/SimpleMobileTools/Simple-Calculator) 与 [5.12.0 官方 release](https://github.com/SimpleMobileTools/Simple-Calculator/releases/tag/5.12.0)。上游标注 GPL-3.0，APK 使用官方 fdroid release 资产，仅下载到忽略目录，未重新分发。
- APK SHA256：092629deaeaaea824dbf0cd824989e80b498b1fa07ad5b4c3e744fc64ce5ab2f。
- 本地资产来源记录：`results/round4-assets/manifest.json`。这是本次下载内容散列，不是上游提供的签名验证。
- SDK 使用现有 `/opt/homebrew/share/android-commandlinetools`。avdmanager 需要显式 Java 17 的 JAVA_HOME；未安装新 SDK 镜像。emulator 36.6.11，分辨率 1080×2400，关闭独立测试设备的动画以减少 UI 状态滞后。
- 上游活动入口为 com.simplemobiletools.calculator/.activities.SplashActivity.Orange。

## 运行证据

成功运行：`results/round4-live-calculator-v3/report.json`，其 execution 子目录含截图、每轮模型原文、provider usage 和动作轨迹，verification 子目录含两次 crash log 与 verdict。模型调用5次，累计15,081 token，全部返回期望模型快照且 usage 完整。该 token 数包含执行模型推理，未估计 API 货币支出，也未将模拟器 CPU 时间换算为费用。逻辑设备操作计数不等于 ADB 进程数，UI 检查作为一个逻辑操作计费。

截图与日志可能含应用信息，继续忽略提交。摘要含 artifact SHA256，但还不是完整签名归档或掉电恢复事务。

两次失败尝试 v1、v2 全部保留：v1 在实际模型响应的 tool_call JSON 解析阶段失败，v2 在终止协议校验阶段失败。v2 的模型自称完成，但人工独立查看 UI 时仍为表达式 2+3，不能记作成功。随后完善工具 schema、保留原始响应、将缺省终止状态规范成 unknown（仅停止请求）并增加独立结果 oracle；未执行不受支持的模型命令。

失败批次父预留继续保留，不因重跑而将其当作零成本或自动释放；已知模型调用与 token 单独从调用记录统计。全部尝试的计数见 `results/round4-assets/acceptance-summary.json`，成功批次指标不能代表全部研发开销。未以多次重试挑选正式测试成绩，三次均为工程调试。

## 代码映射与测试

- scopes.py：分层预算与未决用量保护。
- runtime.py：通用角色执行使用 Scope 和带子账本的 Quota。
- gui_team.py：真实 GUI 执行和独立重放适配器；oracle 标签与已知缺陷映射只在 verifier。
- live.py：通用 Campaign 的真实设备运行入口；明确采用 FixedOrderPlanner 基线。
- android.py：规范 GUI stop 请求，界面稳定等待和 UIAutomator 结果查询。
- providers.py：GUI-Plus 工具 schema、快照与 usage 校验。

Python 3.12 下40项测试全部通过。新增层级额度无重复扣费、未知调用阻止结算、越额和虚报拒绝、真实适配器合同的模拟集成、未知 crash 不计收益、独立 oracle 驳回错误任务完成声明，以及 live entrypoint 模拟验收。此前规划暴力枚举对照、并发预算、训练隔离和 CLI 子进程测试保持通过。远程 CI 尚未运行。

## 剩余科研门槛

这轮完成的是真实执行与预算管理工程验收。当前 proposer/critic 是规则基线，GUI-Plus 是唯一调用的执行模型；未声称已完成多 LLM 协作收益评估。程序化 task oracle 仅适用于指定字段，不能作为通用 GUI 语义正确性 oracle。

未完成正式 THEMIS 纳入清单与 fixed APK 对照、真实多应用划分和训练轨迹、学习型协作策略的同预算效果与校准实验。真实时间上限、外部计费上限、远端服务恢复、屏幕方向变化及 native crash 仍不具备全面保证。Scope 未实现自动重启接管；关闭中断采取保守保留额度，需核对后处理。
