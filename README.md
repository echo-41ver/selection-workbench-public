# Amazon → TEMU 本地选品工作台

Windows 本地自动化项目：采集 Amazon Best Sellers 商品与 TEMU 搜索商品，持续去重、保存断点、导出 Excel，并提供三层同款分析入口和网页监控。

## 功能

- Amazon 真实榜单排名校验，最多前 100 名；详情、图片、规格补充及失败重试。
- 从 Amazon 商品标题提炼 TEMU 关键词，保留大类目词；多路独立环境、销量最高/最近上架两种采样、去重与故障监督。
- 网页配置、监控、历史记录、Excel 入口；验证码保留可见浏览器等待人工处理。
- 文字规则、图片向量候选、外接视觉 API；第三层付费分析由网页单独启动。
- SQLite 增量数据、来源隔离、幂等导入、持续 Excel；缺失销量保留未知，不当作零。
- 一键准备 Windows 依赖并启动本地网页；可选 Skill/stdio MCP 接入 Agent。

## 部署

下载或克隆后在 Windows 执行 deploy.cmd。详细说明见 [部署指南](docs/DEPLOYMENT_GUIDE.md)。也可以让本机 Agent 按该指南安装。程序包含后端和完整流程入口；公开包不包含公司凭据。

管理员私下提供公司配置。当前使用者确认后才能导入并按 Windows 用户加密保存。没有可用 AdsPower API 权限、代理和公司配置，不能进行真实采集。不要上传公司配置、运行数据、日志和含公司配置的内部 ZIP。

## 工程与验证

Python 3.12、Node.js 20+、PowerShell 7、Playwright、SQLite、ResNet50、OpenAI 兼容视觉 API。依赖版本保存在 requirements.lock.txt 与 package-lock.json。

离线测试：python -m pytest -q；Node 测试：node --test tests/*.test.mjs。安装准备检查：install.ps1 -PreflightOnly。

已完成本机四路小规模真实采集（4 件 Amazon、960 条采样记录、649 件去重 TEMU 商品）及离线真实快照的增量接口、Excel 字段和来源隔离验收。参见 [稳定基线](docs/DELIVERY_BASELINE.md) 和 [数据接口](docs/COLLECTION_INTERFACE.md)。四路验收不代表任意并发数或数小时稳定运行；三层入口存在不等于全品类准确率已验收。

本展示包仅有过滤后的源码，不包含原开发仓库 Git 历史、运行数据和实际公司配置。可自行初始化新 Git 仓库后上传。项目不自动发布到 GitHub。

## 版本同步

scripts/build_release.ps1 从已提交源码构建“内部运行版”和“GitHub展示版”，默认均无公司凭据。dist/最新部署包.json 记录版本与 SHA256，旧包保留。需要带配置的内部包时，由管理员双击 dist 内的中文入口并亲自确认。构建不会自动装入本机公司配置。

部署到同事电脑的旧版本不会自动升级；需要使用最新包重新部署。安装失败时项目环境恢复，原数据和断点保留。

Windows安装目录请使用较短路径，例如D:\选品工作台（绝对路径不超过100字符）。预检会在读取公司配置或改变环境之前提示路径过长；不需要修改系统设置。
