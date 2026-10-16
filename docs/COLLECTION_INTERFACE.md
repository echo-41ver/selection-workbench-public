# 第一部分与第二部分交接

网页“开始第一部分 · 数据获取”冻结 `execution_mode: collection`。此模式不启动第一二层 watch、第三层 API，也不读取分析数据库来判断采集完成。旧配置没有此字段时保留原有全流程行为。

命令行可在品类 JSON 中设置该字段，再运行 `scripts/selection.py start --category <文件>`。采集配置只要求品类标识、Amazon 官方榜单及数量、TEMU 搜索配置及路数，不要求三层规则存在。

每个任务均写入独立 `runs/<category_key>/`，第二部分按该目录只读接入：

| 文件 | 内容 / 主键 |
| --- | --- |
| `category.json` | 本次冻结配置，任务标识 `category_key` |
| `amazon/amazon_frontend_latest.json` | 官方榜单及详情快照，`records` 内以 `asin` 为主键，`rank` 为榜单排名 |
| `amazon/amazon_frontend_latest.jsonl` | 同批商品逐行数据，含来源与来源 URL |
| `temu/keyword_plan.json` | 关键词及 Amazon 商品标题证据；追加新词保留已有位置，避免换路重采 |
| `temu/workers/<worker>/<query>/products.jsonl` | 各路各搜索词前端证据，主键 `goods_id` |
| `temu/union/products.jsonl` | 去重后的 TEMU 输入；保留来源词和销量排序证据 |
| `temu/union/watch_state.json` | 汇总是否成功、输入签名、更新时间 |
| `commands/rotate/<请求编号>.json` | 每路换 session 的排队、执行、完成或失败回执 |
| `commands/rotation-results/<请求编号>.json` | 对应替换进程的独立结果，防止不同路并发写入互相覆盖 |
| `publish_state.json` | 两份来源 Excel 的发布状态及错误 |
| `excel/` | 人工查看用工作簿，不作为程序解析接口 |

所有快照使用临时文件加原子替换。第二部分应完整读取单份快照后处理；不能以文件存在、进程存活或商品数量满足限额代替“内容完整”。Amazon 榜单齐全与详情/图片完整是不同状态；失败记录和缺失字段必须继续传递，不得直接判作不同款。

Amazon 准备阶段的 `LISTING_COMPLETE` 仅表示前 N 名标题已获得。点击运行后详情与 TEMU 同时推进。`.listing-checkpoint.json` 按榜单页保留断点；`.checkpoint.json` 记录详情队列。新结果的 `detail_quality` 含标题、规格或卖点、图片解码和实际宽高证据，图片当前要求宽高均不小于 600 像素。只有全部必要证据通过才标记详情完成；旧版只有文字成功标记的断点会重新核验。

关键词 `coverage.without_specific_query` 明示没有特征搜索词覆盖的 Amazon 商品，不能把类目基础词采样当成每件商品的精准搜索。关键词预算有限时优先覆盖不同商品。

通用品类关键词会直接从实际 Amazon 标题提取颜色、材质、结构、带单位参数、件数和型号，不依赖模型必须预先填写 `keyword_features`。识别常见单复数和连字符；每条派生词保存原始标题和属性证据。有限预算优先覆盖尚无具体搜索词的商品，再平衡各商品追加词数；不将商品本身与标题参数强行拼成未经标题支持的品类。网页重启恢复未运行草稿时，用保留的标题本地刷新关键词，不重新调用模型。覆盖率仅表示已规划有证据的搜索入口，不表示已找到同款。

网页支持不同路独立并行换 session：先点击第 4 路，再点击第 12 路，监督器会在下一轮检查时启动第 12 路，不等待第 4 路完成（正常检查间隔约 2 秒）。同一路在排队或替换期间拒绝重复请求。每个请求有独立子进程、日志与结果；失败只更新对应路的回执，成功路独立恢复原断点。共享浏览器清单沿用短时文件锁保护，浏览器替换全过程不使用全局串行锁。

`status.json` 的“完成”仅指本次榜单与关键词限额，绝不表示平台全量。运行中、部分失败、验证码等待等状态不代表数据已完整交付。

本轮不修改第二部分内部算法、规则、向量或视觉 API。第二部分可继续独立迭代，通过上述输入文件接入。

## 双排序采集（2026-09-28）

每个关键词在同一路依次执行 `sales` 与 `recent`，各自限额为 `max_products_per_keyword`。原销量快照目录不变，最近上架使用同级 `<slug>--recent` 目录，因此原并集、watch 和分析输入仍可使用 `workers/*/*/products.jsonl`。两种排序都必须先经页面选中状态确认，点击失败沿用自动重新定位重试。开始滚动采集前遇到验证码，保留页面等待人工验证；当前采集器开始滚动后遇到验证码，保存商品断点并交由监督器自动更换 session，随后恢复同一关键词和排序。

进度中的 `scroll_collection_started` 只表示当前采集器是否实际发出过采集滚动；`collection_phase` 对应 `BEFORE_SCROLL` 或 `SCROLLING`。新采集器从未开始滚动状态启动，不继承旧断点的阶段；仅有历史商品数、轮次或旧自动恢复标记不能触发验证码换 session。新 session 的启动、主页搜索和排序阶段遇验证码仍需人工验证。独立试跑显式关闭 `--auto-session-recovery` 时继续采用人工处理。

后续新建和替换环境共用 Windows Firefox 153 指纹，先打开 `about:blank`，再按默认主页搜索和搜索图标提交流程进入采集。支持中文“销量／新品上架”排序。新检测到的混合本机代理在自动模式下优先 HTTP，显式指定的协议及现有任务出口继续保留；本机已安排下一次新任务采用 HTTP `127.0.0.1:7890`，正在运行的浏览器不迁移。

商品行 `search_sort.requested` 和进度 `sales_sort.requested` 区分两种排序；保留 `sales_sort` 字段名兼容旧消费者。监督状态新增 `current_sort`、`completed_passes`；`completed_queries` 只包含两轮均完成的关键词。请求换 session 后按当前排序独立断点恢复，销量轮已完成不会被最近上架轮覆盖或误用其数量。

合并后每个 goods_id 仍仅一行，`observation_sources` 保存各轮来源。TEMU Excel 增加两张独立样本表，各自按关键词和商品 ID 去重、保留原始展示销量；网页 `frontend_market` 提供同口径描述性参考。最近上架没有精确天数断言，`K/+ sold` 按近似数量读取，缺失展示不作零销量；该模块不改变第二部分的同款或商业评分算法。

本次验证覆盖排序控件切换、两轮执行顺序、最近上架中断后续采、跨轮去重及 Excel 分表；采用隔离模拟页面/采集器，尚未完成新增一次 TEMU 真实网站的两轮验收。
