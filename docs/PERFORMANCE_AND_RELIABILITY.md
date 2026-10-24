# 性能与稳定性优化记录

本轮以原仓库 `788341a82362237e421905fd83db9c122745d034` 为基线，重点修复重复读取、前端刷新竞态、连接故障恢复和发送结果不确定时的重复执行风险。保持原有 macOS Accessibility 与 Windows 平台分工，不增加运行时第三方依赖。

## 改动与实际影响

| 范围 | 原有问题 | 本轮处理 |
| --- | --- | --- |
| 任务列表 | 活动日志缓存仅 8 项，顺序扫描 9 项以上时不断淘汰下一项，重复读完整日志 | 改为 128 项 LRU、每文件锁和增量 offset；限制缓存 turn 数与单条记录大小；识别替换、截短与不完整末行 |
| 长任务历史 | 只截取前 200 项会隐藏后面的最终回复；被省略内容更新可能没有可见 revision | 在 200 项预算内优先保留用户消息、最终回复和最近活动，计算源 revision，并显示省略项数 |
| 历史分页 | 仅依赖末尾内容签名，加载更早历史后可能不触发重绘 | 签名覆盖 turn 数、首 turn、分页 cursor 与源 revision |
| 后台运行刷新 | 900 ms 轮询可叠加，旧响应可能覆盖刚发送、停止或切换后的状态 | 每任务只允许一个读取请求；用版本与状态快照丢弃过期响应 |
| 浏览器恢复 | 请求 body 挂起可卡住刷新；BFCache 返回后定时器可能不再运行 | GET deadline 覆盖 headers 与 body；幂等恢复 pageshow/online/visibilitychange 轮询 |
| 浏览器缓存 | 反复裁剪并 stringify 完整历史，工作量随历史增长过快 | 每个 turn 估算一次，最后序列化完整快照一次；内容相同时不因 fetchedAt 变化重写存储 |
| app-server | 读取连接崩溃后无法自行恢复，旧 reader 可能影响新连接 | 对明确列出的只读 RPC 按需重建连接，失败后 1–30 秒退避；按 generation 隔离 pending/reader；写请求不自动重放 |
| Mac 发送 | 已执行但 HTTP 回包丢失时，重试可能重复发送或创建任务 | 新增独立 deliveryReceipts 能力与 SQLite 持久回执；副作用前提交 claim；相同 ID 只查询回执 |
| Desktop 状态 | 多客户端同时轮询会重复运行 Accessibility 探测 | 共享单次探测，显示结果缓存 0.75 秒；操作前检查仍读取新状态；超时返回结构化错误 |
| 热点代理 | 每个请求关闭浏览器连接，频繁启动 route 探测 | 允许 HTTP/1.1 连接复用，空闲 15 秒/最多 100 请求；按时间检查网络，拓扑变化时关闭已建立连接 |

这些改动主要改善反复刷新成本和故障后的恢复能力。单客户端每 3 秒的 Accessibility 探测周期没有改为事件推送；0.75 秒缓存主要合并多个页面或设备同时发起的请求。

## 可重复的缓存基准

运行：

```sh
python3 scripts/benchmark-history-cache.py
```

对比基线：

```sh
git show 788341a82362237e421905fd83db9c122745d034:codex_app_server.py > /tmp/pocket-baseline-app-server.py
python3 scripts/benchmark-history-cache.py --module /tmp/pocket-baseline-app-server.py
```

脚本创建 50 个临时合成 JSONL 文件，每个 525,120 字节。分别顺序扫描 8、9、50 个任务三次，记录实际文件读取字节数、打开次数和本地耗时。它不读取真实账户，不启动模型，不触发 Desktop 操作。

| 场景 | 基线每轮读取 | 优化后每轮读取 |
| --- | ---: | ---: |
| 8 个不变任务，第二/三轮 | 0 B | 0 B |
| 9 个不变任务，第二/三轮 | 4,726,080 B | 0 B |
| 50 个不变任务，第二/三轮 | 26,256,000 B | 0 B |
| 50 个任务，首次冷扫描 | 26,256,000 B | 26,256,000 B |

本次 Linux / Python 3.12 的一次样本中，50 个任务的热扫描从约 96–98 ms 降至约 0.46–0.64 ms；首次扫描从约 95 ms 变为约 123 ms。冷扫描的有界逐行处理有额外开销，主要收益来自后续不变文件的增量读取。文件系统、日志内容及机器负载会影响时间；此处字节数更能直接验证缓存问题是否解决。

这些数字不代表手机端到端延迟。真实体验仍取决于 Tailscale 路径、手机网络、Accessibility 探测、Codex Desktop 和模型响应。代理测试验证了 3 个连续请求复用一个客户端 TCP 连接，没有对真实 TLS 握手或手机网络做测速。

## 回执协议与兼容性

Mac 能力表增加 `deliveryReceipts: true`；`nativeTextSend` 的 Windows 含义保持不变。新前端仅在能力可用时，为新建任务和已有任务的发送携带 `requestId`。

- claim 在发送或创建前持久提交，同一设备的相同 ID 不会再次执行。
- ID 不能与不同指令/附件复用；创建请求还核对 Project、模型和推理配置。
- `receiptOnly: true` 不分配新 claim，不发送消息，不创建任务。
- 若新建已经取得 thread ID，会保存该 ID，并允许前端在该任务下核对原请求。
- 文本结果可根据新的、精确匹配的历史消息确认；附件结果和失去 thread ID 的创建操作在证据不足时保持待确认。
- 重启后不会把未知结果自动当作“没发送”。回执只保存内容哈希与必要元数据，不保存指令或附件正文。
- 浏览器在请求待确认期间，临时把原 payload 与 ID 保存在 sessionStorage，便于页面刷新或编辑草稿后继续核对；收到终结回执后删除。编辑后的新草稿不会覆盖这份待确认上下文。
- 老客户端不携带 ID 时仍按原有协议处理。Mac Continue 暂未加入这一套回执；Windows 原有回执路径保持独立。

这是防重复派发与结果核对机制。Desktop 没有提供覆盖所有 UI 动作的端到端事务，不能据此承诺任意断电/崩溃场景的 exactly-once 执行。

### 手工处理一直待确认的请求

记录若因崩溃、附件回执缺失或未知创建结果无法自动核对，会阻止同任务的新发送；未知创建会阻止同设备再次新建。先在电脑确认原操作是否已经执行，再使用逐条恢复工具。不要删除整个回执数据库。

1. 在 Mac 上核对 Desktop 的任务历史，等待相关操作停止变化。需要停止 Bridge 时，确认其他后台任务已处理妥当；停止 Bridge 会影响其持有的后台进程。
2. 停止 Bridge 服务；使用安装器的默认 LaunchAgent 时可运行：

   ```sh
   launchctl bootout "gui/$(id -u)/com.local.mobile-codex-bridge"
   ```

3. 查看待确认记录的 owner、request ID、阶段与已知任务 ID：

   ```sh
   python3 scripts/manage-delivery-receipts.py list
   ```

4. 逐条决定结果，替换下例中的占位值：

   ```sh
   python3 scripts/manage-delivery-receipts.py resolve \
     --owner DEVICE_ID --request-id REQUEST_UUID --outcome confirmed \
     --confirm-stopped-and-reviewed
   ```

   `confirmed` 表示你已确认原消息送达。若创建请求未保存任务 ID，必须同时提供实际的 `--thread-id THREAD_ID`。如果核对后决定放弃这次尝试，使用 `--outcome refused`；它只解除这条记录对后续新 ID 的阻塞，不撤回已执行的操作，也不会发送任何消息。

5. 重新启动 Bridge，再在页面核对原回执。后续新消息使用新 ID；旧 ID 永久保留为终结记录，不会再次执行。

   ```sh
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.local.mobile-codex-bridge.plist"
   ```

默认数据库是 `~/Library/Application Support/MobileCodexBridge/delivery.sqlite`。自定义 device registry 时，使用它旁边的数据库，通过脚本全局参数 `--journal /path/to/delivery.sqlite` 指定。自定义 Bridge 端口时，resolve 还需提供 `--bridge-port PORT`。脚本在该端口仍有服务时拒绝修改记录；确认参数要求操作者已停止实际 Bridge 并核对 Desktop。

如果第一次请求根本没到 Bridge，页面可能保留待确认编号，而 `list` 没有对应记录。核对 Desktop、设备 ID 与页面提示的请求 ID 后，仍先停止 Bridge，再使用下面的命令将该未见 ID 永久标为放弃。它不执行消息；迟到的原请求也只能取得拒绝回执。已有记录不能通过此命令覆盖，必须使用 `resolve`。

```sh
python3 scripts/manage-delivery-receipts.py abandon-unseen \
  --owner DEVICE_ID --request-id REQUEST_UUID --scope create \
  --confirm-stopped-and-reviewed
```

发送到已有任务时，将 `--scope create` 换成原任务 ID。设备 ID 可在停止服务前通过 `python3 scripts/manage-bridge-devices.py list` 核对。恢复服务后，页面使用原内容核对终结回执；新草稿仍需你再次点击发送。

## 健康状态

```sh
curl http://127.0.0.1:4317/health/live
curl http://127.0.0.1:4317/health/ready
```

`/health` 与 `/health/live` 仅表示 HTTP 进程存活。`/health/ready` 在读取连接已初始化并存活时返回 200，否则返回 503，并给出 `appServer.state` 与 `retryAfterSeconds`。健康探测本身不启动 Codex、不执行 RPC、不激活 Desktop；后续安全读取按需触发恢复。因此 readiness 不表示 Accessibility 权限、特定任务可发送、网络或模型均已就绪。

## 验证范围

2026-10-24 在 Linux / Python 3.12.14 / Node 24.22.1 的最终验证结果：Python 共 475 项，464 项通过、11 项因平台条件跳过；浏览器逻辑共 75 项全部通过。相较基线新增 73 项 Python 和 22 项前端回归。语法与 diff 空白检查通过。Node 24 本机使用 `--test-isolation=none` 输出逐项计数，CI 继续使用 Node 22 的常规测试命令。

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_web_*.cjs
node --check web/app.js
node --check web/pairing/pair.js
git diff --check
```

新增回归覆盖长 turn 尾部、JSONL 截断、跨文件并发、catalog 生命周期、旧 generation、重复提交、响应丢失、重启、手工回执解除、请求 body 挂起、BFCache、历史实际重绘、持久连接和网络变化关闭连接。HTTP 测试使用回环地址与假服务；不能用沙箱禁止本机端口造成的失败判断产品代码有缺陷。

新增 Linux/macOS、Python 3.11/3.13、Node 22 的 CI，并扩充现有 Windows 检查。工作流配置不等于已在所有系统实测通过：真实 Mac Accessibility、Windows 原生脚本、手机浏览器与网络切换仍需要对应设备验证。

## 后续值得继续做的优化

1. 对齐前端、Bridge、Helper 和代理的操作 deadline，补齐 stdio 写入阻塞的总超时预算。
2. 研究 SSE 或事件推送及自适应轮询；这需要一起改代理的流式响应路径，本轮仍使用轮询与完整响应缓冲。
3. 为很长的历史引入虚拟列表或局部 DOM 更新，进一步减少手机渲染工作量。
4. 缩小附件上传期间的全局锁范围，并统一 UTF-8 字节上限与字符上限。
5. 对真实设备记录首屏、发送确认、历史更新的 p50/p95，以及空闲 CPU、内存和重连次数，再决定下一轮优先级。
