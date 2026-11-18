# 资料收集端设计

收集端只负责从授权网关取得原信、解析 MIME、按材料内部披露代码归属，并把每个判断
可追溯到原信与具体 MIME 部分。网关不替收集端作判断，收集端也不执行邮件内容。

## 1. 事实模型（只追加，不就地改写）

状态目录（默认 `./var/collector`，可用 `--state-dir` 指定）保存两类东西：

- `blobs/sha256/<前两位>/<完整哈希>`：原始字节，包括整封原信和每个叶子 MIME 部分。
  同 Message-ID 的不同字节各占一份；同字节只存一份。
- `ledger.sqlite3`：只追加的事实表，任何重跑都是幂等的，不产生重复事实。

表：

- `raw_messages(sha256 PK, message_id, bytes, observed_first_at)`
- `observations(id, raw_sha256, folder, generation, uid, observed_at, UNIQUE(folder, generation, uid))`
  —— 同一封原信移动前后各留一条观察；文件夹重建后的新代际是新的覆盖，不复用旧序号。
- `mime_parts(id, raw_sha256, depth, index, content_type, filename, disposition, cid, blob_sha256)`
  —— 嵌套 `message/rfc822` 递归进入，叶子部分保留独立字节与路径。
- `disclosures(id, part_row, raw_sha256, depth, index, valid, parse_error,
  enterprise, period, part, edition, blob_sha256)` —— 字段不全/解析失败 `valid=0`，
  仍登记为待核实，而不是丢弃。
- `corrections(id, raw_sha256, depth, signer, replacement_of, target_part, body_excerpt)`
  —— 只采信正文里结构化的 `replacement-of` / `target-part` 两行，自然语言不构成事实。
- `attestations` 为 disclosures 的判定投影（见下），撤回判定为 corrections 的投影，
  两者都在读取时从事实表计算，不另存可变状态。
- `manual_decisions(id, enterprise, period, part, scope_part NULL=全册, kind,
  rationale, created_at, superseded_by NULL)` —— 人工确认/排除有自己的适用范围；
  之后来信冲突时不自动冲掉，只把冲突升级为待核实，要求人工处理。
- `checkpoints(folder PK, generation, through, updated_at)` —— 中断后续收的依据。
- `contacts(enterprise, address, updated_at)` —— `/contacts` 的本地快照。

## 2. 归属与签发身份

披露文件必须内部同时含 `enterprise / period / part / edition` 四个键且各只有一个值，
以其内部代码为归属依据；主题、文件名、Message-ID 都不参与版本判断。

附件的“签发者”是直接承载该 MIME 部分的那层邮件的 From：外层是转发，则外层转发者
不是签发者，要进嵌套层核对。该 From 必须出现在附件声明企业的通讯录里，否则
`attestation_pending`（转发者不在通讯录、其他企业同名文件、来源不明转信同此处理）。

只提到原件却没带来原件的邮件，没有披露部分可登记；若同时没有可采信的结构化更正，
不改变任何现状（移进 Archive 本身更不是撤回）。

## 3. 更正与撤回

更正信必须同时给出 `replacement-of`（目标 edition）与 `target-part`（目标册别），
且：

1. 签发者在某企业通讯录中；
2. 台账中该企业、该册别、该 edition 存在有效披露（期间唯一），期间据此确定；
3. 范围只落在 target-part，其他册别不受影响。

满足后该 edition 进入 `withdrawn`（确认撤回）。条件不满足（范围不清、edition 对不上、
签发者不在通讯录）登记更正事实但不撤回，挂 `correction_unresolved` 待核实。
更正信自身携带的附件按普通披露附件独立走归属流程。

## 4. 状态口径（企业 × 期间 × 册别）

- `available`：有有效披露且签发者匹配，且没有被有效撤回。
- `withdrawn`：有效披露被范围相符、身份相符的更正撤回 —— 与“尚未收齐”严格区分。
- `missing`（缺册）：该企业该期间已出现过的册别中，当前没有任何有效可用版本。
- `pending`（待核实）：存在候选但证据不足，原因码包括
  `disclosure_invalid`（字段不全/无法解析）、`attestation_pending`（签发身份不符）、
  `manual_conflict`（与人工确认冲突）、`correction_unresolved`（更正无法落地）。

人工确认是带范围的覆盖层：同一范围内后到材料与确认冲突时不自动替换，转
`manual_conflict`；撤回判定同样不越过人工确认，冲突转人工。

## 5. 收件循环与重建

每个文件夹独立保存 `(generation, through)` 检查点：

- 先 GET `/folders`；本地无该文件夹或代际变化时 `after=0` 重新枚举整代，
  旧代际序号不再代表覆盖；枚举期间拿到 409 就丢掉本页结果、按新代际重来，
  重建窗口里新增的信因此不会漏。
- 代际一致时从 `through` 继续；每页按返回的 `through` 落检查点，先把本页
  原信全部下载、核验 sha256、入账，再推进检查点，保证中断后继续不丢不重。
- `--once` 跑完整一圈退出；不带参数按间隔轮询。

## 6. 安全口径

邮件与附件一律是不可信数据：展示/导出只做转义文本与字节复制，不渲染 HTML、
不打开或执行附件、不访问邮件中的任何链接（`.invalid` 也不访问）；写入状态目录
之外不产生文件；所有网络目标只有网关基址。
