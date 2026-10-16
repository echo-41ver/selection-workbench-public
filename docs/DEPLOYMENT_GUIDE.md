# 同事的一键部署说明

公司内部使用“内部运行版-含公司完整配置.zip”。解压到固定的短目录，例如 `D:\选品工作台`，双击 `deploy.cmd`。也可以让本机 Agent 执行：“按本说明运行 deploy.cmd，等待部署检查通过并打开工作台，不启动采集或付费分析。”

目标电脑使用 Windows 10/11 x64，预留至少 12 GB 可用空间，并开启该机可用的公司 VPN／代理。安装绝对路径不超过 100 字符，支持中文和空格。不要在 ZIP 内直接运行。缺少 VC++ 运行库时，Windows 可能显示管理员安装确认。

完整包随带固定版本 Python 3.12.10 安装器、便携 Node.js 24.19.0、便携 PowerShell 7.6.5、VC++ 安装器、Python 依赖、Playwright 模块和 Chromium、AdsPower 8.7.23 安装器、第二层 ResNet50 模型和 Codex CLI。先核对随包文件的 SHA256，再执行安装器；不要求目标电脑预装 Python、Python Launcher、Node、Git 或 WinGet。AdsPower 的账号授权、Firefox 153 内核准备和实际站点访问需要联网。

完整内部包会自动导入随包的公司配置，在目标电脑按当前 Windows 用户重新加密。AdsPower 使用公司 API Key 启动后台服务；代理和默认分析 API 使用公司配置，无需同事逐一登录这些服务。程序不复制原电脑的账号登录缓存。使用本机 Agent 时，由同事使用该机自己的 Agent 账号；部署期间不会强制弹出 Codex 登录。普通内部基础包和公开包不含公司凭据，不能代替完整公司配置包。

网络继续自动识别当前电脑的 HTTP／SOCKS5 出口和 TUN 路由。完整包不带旧电脑的固定代理端口。保留目标电脑已经安装和运行的 VPN、系统代理和订阅，不改变系统代理、TUN 或防火墙设置。检测到多个可用出口时，需要在工作台选择一次；没有可用出口时不会报告运行条件齐全。

安装会实际执行以下无付费分析检查：

- 使用 CPU 加载随包图像模型并完成一次推理。
- 写入、保存并重新打开含中文、数值和图片的 Excel。
- 启动随包 Chromium 并读取页面内容。
- 使用公司密钥连接 AdsPower，准备 Firefox 内核，创建独立测试环境，激活其窗口并验证 WebDriver、BiDi 和页面内容读取，随后清理这个测试环境。
- 运行工作台 MCP 的真实本地握手，检查公司默认分析密钥已经配置。不会自动发起计费分析请求。

只有基础依赖、当前安装的以上运行检查、公司默认分析配置和工作台 MCP 全部就绪，完整内部部署才报告 `parity_ready=true` 并成功退出。`ready=true` 只代表基础条件，不代表完整部署完成。安装结果保存在 `runtime/install-status.json`，实际运行检查保存在 `runtime/deployment-smoke.json`。API 密钥已配置与该服务余额、实时可用状态分别记录。

部署完成会打开工作台。以后双击 `start-web.cmd`。网页生成并检查品类规则后，再启动采集；第三层分析仍需单独点击启动。网站验证码按现有流程处理。

安装失败会恢复本次替换前的项目虚拟环境、Node 依赖、便携工具和已导入的公司密钥，保留采集数据、断点和 Excel。旧环境与失败环境保存在 `.deployment-backup/`。错误阶段、脚本行号、异常类型及命令退出码写入脱敏的 `runtime/install-failure.json`，修复原因后可再次运行 `deploy.cmd`。系统级 Python 或 VC++ 安装不会因项目回滚而卸载。

只读准备检查：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 -PreflightOnly`。外部另行提供的配置仍显示导入确认；管理员明确授权的命令部署可使用 `-AcceptCompanyConfig`。

公司完整包只放在私密仓库或发给获授权同事。公开仓库只同步公开发行源码和公开包，不包含公司配置、运行数据或账号缓存。
