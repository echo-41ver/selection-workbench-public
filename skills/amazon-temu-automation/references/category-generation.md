# 新品类规则生成

采集前提未齐时，可以先编译 identity_policy，再用规则编辑器预览或修改标准；未知的榜单 URL 和用户数量保持 null，不用假值绕校验。draft_view/edit_rules 仅检查规则结构、语义一致性及安全字段；prepare/start 和冻结读取器仍执行完整前提校验，必须先核实真实来源并取得数量。纯阈值/图片提示修改且第一层文字不变不需要模型调用；实际自然语言改写仍须遵守服务和预算授权。

先调用 `selection_category_contract`，或运行 `.venv/Scripts/python.exe -m selection_automation.category_contract` 获取与当前程序匹配的配置骨架和字段说明。当前Agent完成规则生成，不额外调用文本API，不要求使用者手写规则。`config/categories` 只是示例，不是支持品类清单。

可替换部分是第一层 `stage1.attributes`、第三层 `stage3.modules/module_descriptions/visual_instructions` 及评分条件；同时替换本品类的 Amazon 榜单、TEMU 基础词/特征词和排除词。新品类使用 `stage1.text_gate=declarative`，不要选择浴巾等专用品类 gate，不继承专用 scoring_policy。程序自动把这些配置接入第一二层、视觉请求和Excel，用户不需要编辑脚本。

1. category_key为英文小写、数字和下划线；中文名称不含路径字符。市场US。
2. 根据真实Best Sellers页面确定标题、链接和节点，写入amazon.url_evidence。X为1–100；榜单不足时报告，不用搜索结果补齐。
3. TEMU基础词为明确英文品类词；keyword_features覆盖品类结构/功能/外观的代表性短语。程序只用Amazon商品实际文字命中的短语组合关键词并保留来源ASIN，不沿用浴巾词典。
4. stage1.text_gate=declarative。attributes含name、hard_conflict、values、ambiguous_phrases。values把同义短语归类；仅双边各命中唯一且不同的属性值才硬排除。组合套装、否定、适配/替换描述加入模糊短语。品牌、价格和缺失信息不作冲突。
5. 第三层只用主图可验证身份特征，通常3–7模块。modules权重合计100；module_descriptions说明比较内容，module_labels用中文。
6. 优先用identity_required_modules明确标记可见关键身份特征，不把辅助权重或不可见电池仓、后接口当作一律必须看见。未提供时兼容confirmation_minimums的模块键。关键未知仍阻断。可用module_variant_fields将模块绑定具体允许变体；只有同款设计已对应且有具体双边变体事实时才兼容V，不能用其他模块补分。variant_fields只填写具体允许的变体名称，不填整个模块键；默认本体图案、结构、主体类型及套装构成变化不是普通颜色变体；浴巾可明确允许decorative_pattern，但主体织法、构造与阴刻/装饰带位置布局必须对应，浴帘整幅印花仍不可变。分数阈值保留辅助配置，不能决定最终同款。
7. 整体身份核验由通用协议自动加入：先分别识别主体和套装，按实际可见内容记录有辨识力的双边共同细节，不凑固定条数、逐项差异及遮挡未知。模块保留双边具体证据；不让模型评分。主图不能可靠证明的材质、精确尺寸、功率、电压或内部结构，不在视觉层推断。
8. 默认deepseek-flash、https://api.deepseek.com、batch_size=12、max_concurrency=4。禁止把公司Key发给任意生成的端点。
9. 使用本品类的“明确一致、明确不同、信息不足”三个文字样例自查规则；双边信息不足时不得硬排除。调用prepare检查并冻结任务。配置错误由Agent修正后重新prepare，不转交用户手写；不得在校验失败时启动。规则和数量发生变化时使用新任务key，保留旧断点。不生成可执行Python/PowerShell作为规则。

电钻要避免把适配配件认成整机；浴帘印花不同是身份冲突；其他品类重新定义身份边界，不机械套用这些例子。
