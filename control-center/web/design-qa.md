# Lite Actions Demo 设计验收

## 对照依据

- 视觉基准为第 3 张 Pools 时间轴设计稿。 [原稿](/Users/depeng/.codex/generated_images/01a1ec5e-1625-7de0-8982-4e3307067e56/exec-04616636-5482-48d3-a1e7-a8f0dff7730a.png)
- 浏览器截图使用 1487 × 1058 CSS 像素、DPR 1，原稿和浏览器截图均为 1487 × 1058 像素。两图均为匿名、浅色、Blue / A3 队列选中 8P 用例的初始态。对照图对两侧使用相同裁剪与缩放。 [浏览器截图](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/pools-light.jpg) · [全页对照](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/compare-final-full.jpg) · [时间条局部对照](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/compare-final-focus.jpg)
- 业务基准为 `website/control-center-prd` 分支定稿 `fb2b757423f7a82c1929d744ac50d27ae8fce287`。 [PRD](https://github.com/depeng1994/lite-actions/blob/fb2b757423f7a82c1929d744ac50d27ae8fce287/control-center/docs/PRD.md)

## 发现与修正

- **P2，首次比对已修正。** 初版队列表位置比设计稿偏上，节点地址、表头和图例偏小。提高设备组行高、调整标题区留白和信息字号，复拍后时间轴、下方队列与图例均处在对应视觉区间，IP 地址在桌面完整可读。 [首次全页对照](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/compare-before-full.jpg)
- **P2，首次比对已修正。** 运行租约中的作者、阶段和时长被长仓名或并排元数据截断。把阶段、已用时间、预计时长和样本数放到独立一行，点击租约也可查看完整字段。准确按分钟计算租约边界与预计时段。复拍确认 16P 租约跨两组，实线在现在标记结束，预计使用接续为虚线。 [最终时间条局部对照](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/compare-final-focus.jpg)
- **P3，视觉差异。** 原稿使用两行租约文字，Demo 用三行呈现可读的完整运行信息。浅色保留暖灰底色、青绿选中态和蓝黄资源配色。深色沿用相同信息层级和状态线型。 [深色截图](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/pools-dark.jpg)
- 字体使用系统中文无衬线，IP、时钟和版本号使用等宽。原稿为文字标识、表格和标准图标，Demo 使用可访问文本、原生布局与 Phosphor 线性图标。页面无外部图像资源依赖。检查了图例与排队文案、按分钟定位、对话框标题、空历史态和键盘焦点样式。

## 浏览器验证

- 两种主题均可切换。1487、1024 和 390 CSS 像素下检查 Pools。窄屏的时间轴和表格可在各自容器中横向滚动。
- 匿名读取完整模拟 IP 和端口。模型仓默认打开 vllm-ascend。个人仓、组织仓和 GitHub／GitCode 可区分。103 条历史以 50、50、3 加载，匿名可打开归档详情。
- 普通管理员可为 GitCode 排队用例插队，GitCode 无取消入口。GitHub Workflow #1072 展示平台结果、Blue 与 Yellow 停止回执和设备释放，其他运行租约继续显示。 [取消回执](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/cancel-receipt.jpg)
- Blue A3 设备组 0–7 暂停后，等到模拟配置回执才停止展示该组新排期，已有 8P 租约继续显示，8–15 维持启用。恢复、资产新增、排空和删除已用演示节点验证。 [暂停影响预览](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/pause-impact.jpg)
- Super 能创建普通管理员、再次读取相同演示密钥并删除账号，审计显示撤销结果。普通管理员无用户管理入口。 [用户管理截图](/Users/depeng/.codex/visualizations/2026/11/18/01a1ec5e-1625-7de0-8982-4e3307067e56/super-users.jpg)
- 浏览器检查无警告或错误。构建和模板静态托管测试通过。运行数据为当前标签页内的模拟状态，刷新恢复样例，主题保存在本地。

final result: passed
