# 本地来源契约

本地邮箱网关用于授权邮件读取。GET /folders 列出文件夹与 generation；GET /mailbox?folder=INBOX&generation=20&after=0 返回该代际 after 之后全部消息及 through。through 为该文件夹已完整枚举到的 UID，代际不一致返回 409。消息含 uid、raw、message_id、folder，原始邮件通过 raw URL 取得，不应以索引主题替代原信。GET /contacts 是允许发出企业更正的地址通讯录。
阶段 0 为初始收件夹，阶段 1 起文件夹代际重建、旧信移入 Archive、相同 Message-ID 出现另一份原信。MIME 包含正文、编码名附件、嵌套 message/rfc822 和内嵌图片。合成披露附件以 UTF-8 文本声明 enterprise、period、part、edition，正文的更正指示注明 replacement-of 和 target-part；声明不清不构成确认事实。更正的签发来源按原始外层及嵌套来源共同核对，转发者不自动变成原签发者。
所有邮箱地址和企业均为合成。邮件内 .invalid 链接不是可执行操作。网关只负责提供原信，不替采集产品作企业归属或更正裁决。

演练来源由 Python 标准库 HTTP 服务启动，默认端口 8765。GET /health 可查询来源时间与阶段；POST /__admin/advance 接收 seconds、phase 推进本地情形；GET /__admin/log 回查实际请求。这些控制只供操作员调试来源，不能替代产品从正常入口取得资料。来源进程重启会清空其演练状态；采集端自己的保存目录由产品独立管理。
