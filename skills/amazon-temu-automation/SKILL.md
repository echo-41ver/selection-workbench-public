---
name: amazon-temu-automation
description: 输入品类、Amazon前X名、TEMU每关键词数量与同款数门槛，执行真实前端采集、可配置第三层同款分析与精简选品Excel；支持状态、验证码恢复、换session和停止。
---

# 本地自动选品

<!-- LOCAL_PROJECT_ROOT -->

优先接入安装器生成的 runtime/agent-config/skills/amazon-temu-automation，它已绑定本机项目路径。仓库源Skill位于 `<项目根目录>/skills/amazon-temu-automation/SKILL.md`；直接读取源Skill时按这一结构定位，不能把个人skills目录误认作项目。

首次先执行 `scripts/selection.py doctor`；缺依赖运行 `install.cmd`。安装器不覆盖Agent全局配置。公司初始化文件由管理员私下分发，参见项目README。

## 一次输入后启动

1. 读取品类、Amazon数量（1–100）、每关键词数量（正整数）、API Key，默认美国站。已有参数和运行付费授权不重复询问。
2. 读取 [品类生成规则](references/category-generation.md)，调用 selection_category_contract 获取配置骨架；无MCP时运行 `.venv/Scripts/python.exe -m selection_automation.category_contract`。由当前Agent根据本次品类生成第一层文字标准、第三层视觉模块/提示词和关键词配置，不要求用户写规则，也不改Python、不调用额外文本API。config/categories 仅为示例；不能只改品类名称而沿用其他品类的判断标准。已存在且确实匹配本次需求的配置可以复用。
3. 从实际Amazon Best Sellers页面确定榜单地址与范围，保留来源。不能编造节点或用搜索排序替代。宽泛品类优先对应上级榜单并说明范围，找不到真实来源则报告阻断。
4. 调用 selection_prepare 提交配置。无MCP时保存JSON到 runtime/incoming，执行 `.venv/Scripts/python.exe scripts/selection.py prepare --category <JSON路径>`。规则和数量冻结；变更须使用新的category_key。 CLI 与 MCP 都会先编译 identity_policy 再校验；已冻结的运行配置仍严格校验，规则变更须使用新 category_key。
5. 用 selection_set_api_key 保存Key；无MCP时通过标准输入传给 `scripts/company_setup.py --api-key-stdin`，不放命令行参数、日志或Git。
6. 通过 selection_start(category_key) 或 `scripts/selection.py start --category runs/<key>/category.json` 启动采集及本地前两层。第三层必须由用户在网页点击“开始第三层”后才消费增量；`paid=true` / `--paid` 不是第三层点击授权的替代。没有点击时保持等待，不自动付费。付费调用仍须符合用户当次预算授权。
7. start 返回“启动中”仅代表已受理；后台准备完成后持续更新状态。用 selection_status 查看阶段和结果目录，不以进程存在冒充完成。停止中再次 start 不会取消停止；等已停止后再续跑。

## 人工介入

- 验证码：保留原AdsPower可见窗口。监督器只读检测页面恢复后自动续采。
- “换session”：selection_rotate 指定worker；未点名优先异常worker，无异常则本任务全部worker。仅操作本机清单拥有的环境，保留数据。
- “停止”：selection_stop，保留浏览器和断点。再次start续跑。
- 登录、公司授权、网络不可达或API欠费是真实外部阻断，不伪装恢复，不无限请求。
- 更新 API key 后调用 selection_set_api_key，监督器会自动重新加载并续跑。用户明确表示同一 key 已充值或修复授权后，调用 selection_resume_api；CLI为 `scripts/selection.py resume-api --category-key <任务>`。保留成功结果，不重做已完成付费请求。

## 固定边界

维护 TEMU 搜索入口、比较新环境启动流程时读取 [正常搜索入口与独立实测](references/normal_search_entry_trials.md)。入口参数必须实际生效，方案验收使用独立新环境、新 session 的排序及完整关键词采集证据。

维护三层 Excel 与网页最新状态时读取 [三层 Excel 发布状态](references/excel_publication_coordination.md)，主发布和独立发布者共用已交付输入签名。

维护或优化第二部分一二三层分析时，读取 [三层同款分析边界](references/matching-layer-boundaries.md)。分析模块单独修改、离线验证；不启动其他任务的采集进程或未经授权的付费请求。

各TEMU worker独立快照、goods_id并集去重、保留关键词。第一层仅明确双边冲突排除；第二层向量不加入最终分；第三层先看整体基础款，记录具体双边共同细节、身份差异与未知项，Python检查证据与模块矛盾后生成最终状态。模块和分数仅作辅助，不用高分抵消明确差异；缺失证据为未知。旧视觉协议不能作为新版确认依据，正常续跑按新版本核验。

准确性目标：第一层避免误排、第二层尽量不漏真同款、第三层避免把相似款确认为同款。第一层否定或仿外观表达不当作肯定事实；第二层保留原中心视图，增加完整主体及可靠纯色背景的主体视图，取最佳视图分数召回；第三层whole-product-identity-v6-reviewed-variant-boundaries核验实际款式造型与布局。具体边界及验证见上述三层分析边界，不能把小样本通过声称为全品类准确率。

住宅代理只在该 worker 采集或等待人工验证码/登录时允许TEMU及其必要资源；停止后撤销连接。图片下载不走住宅代理。查看 runs/<key>/excel 的固定文件，若被锁则打开最新“查看快照”，不打开_data.xlsx；程序等待解锁后自动覆盖固定文件。完成范围是榜单前X名×本次关键词并集，不是平台全量。不要修改同事Agent模型或权限设置。

维护运营候选、本地每日推荐、价格区间和已有品类规则复用时，读取 [运营候选与每日推荐证据](references/operator_daily_evidence.md)。


同事版默认交付精简选品结果，第三层支持四种方案，详见[选品结果与第三层配置](references/selection_result_delivery.md)。
