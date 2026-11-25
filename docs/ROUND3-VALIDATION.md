# 第三轮实施与验收记录

2026-11-23。本轮新增 GUISession、多轮设备执行、受限动作语言、应用 reset、崩溃签名与重复重放、保形校准、CLI 子进程集成测试、预算审计和 CI 配置。

## 模块与数学设计

`calibration.residual_radius` 使用 split conformal 的有限样本次序统计量：对 n 个残差排序，取 ceil((n+1)(1-alpha)) 位；若该位大于 n，返回无穷大而非任意缩窄区间。收益采用双侧绝对残差，资源上界采用正向残差。`calibrate_by_application` 对同应用残差取最大，按应用独立单位校准；要求组交换性，分布漂移下不保证覆盖，多个资源没有自动联合覆盖保证。

训练、调参、校准和最终测试必须分开。calibration 是 validation 的预留子分区：在原始应用级划分后，再将验证应用划成 tuning 与 calibration；训练函数使用 tuning 记录作为 validation，校准函数使用 split=calibration，不能把整个 validation 先调参后再重复校准。当前未自动创建这个子分区，调用者须提供固定名单。

校准模型给出独立的 reward_lower，不把残差半径伪装成贝叶斯方差。规划器遇到无信息的无穷界限时弃权。校准仍未在真实测试轨迹上验证。数学基础线性模型与有限候选规划详见 ALGORITHM.md。

## 可执行验收

在本机 Python 3.12 上 32 项 unittest 通过（系统 Python 3.9.6 上也执行通过，但项目声明支持 3.11+），其中包含子进程执行 `fit-joint`、checkpoint reload、`split`、`team-fixture`。GUI 集成使用模拟设备和模拟模型，验证一次探索和两次重放共 10 个逻辑设备操作、一次模型调用，验证额度未用部分正确释放。失败模型场景保留验证预留，真实网关超时保留模型调用预留。

受限动作测试覆盖异常坐标、额外 shell 字段、不支持字符、文本 shell 引用、按键、滚动、reset 失败后禁止启动。provider mock 测试覆盖 usage、快照 mismatch、共享多维账本和超时。不调用付费模型。

修复了 SQLite 初始化中 executescript 隐式事务提交问题；TeamExecutor 从已验证日志恢复已知缺陷去重集合；日志写入增加 fsync；角色额度拒绝非有限数值。journal 和账本仍不是跨数据库原子提交，异常恢复仍需核对；不能据此宣称分布式 exactly-once。

本机 ADB 存在，`adb devices -l` 返回空设备列表。没有真实设备、APK 运行或 GUI-Plus 联网证据。GUI runner 尚未与通用 TeamExecutor 共用一个角色级 quota scope，因此 GUI session 目前是独立真实执行入口，不能把两套账本的资源加总声称已经完成通用多智能体端到端预算闭环。

## 正式实验前剩余门槛

- 目标 APK 与 fixed APK、许可证、应用谱系、环境恢复可重复性清单。
- GUI executor 与通用协作调度的统一 quota scope；模型动作 critic 的真实调用接口。
- 真设备测试输入和观测稳定性、crash buffer 异步延迟、非 Java/native 崩溃和多进程应用的归属验证。
- 真实训练/验证/测试轨迹、组级校准与完整固定/动态预算和协作对照。
- 校准 artifact 的持久化及训练特征缩放、模型选择；当前 checkpoint 仅保存联合线性基模型。
