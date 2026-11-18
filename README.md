# ESG 往来邮件的原件收集与更正版归属

这是围绕城市低碳公开资料的本地研发项目。`source` 提供授权合成邮箱网关与来源契约；
`collector` 是资料收集端，持续收取原信与可识别文件，按材料内部披露代码整理各企业
报告期的可用、缺册、待核实与已确认撤回，每项判断都可回到原信与具体 MIME 部分。
来源不会替收集端作身份、覆盖或交付判断。

## 运行来源

使用 Python 3.11 或 3.12，先执行 python -m pip install -r requirements.txt。

启动命令：python -m source.server --port 8765。浏览器或 HTTP 客户端访问 http://127.0.0.1:8765。接口和资料定义见 docs/source-contract.md。

## 运行收集端

收集端只用 Python 标准库，默认状态目录 `var/collector/`（已在 .gitignore 中排除）。

```
# 收一圈即退出（适合定时任务）
python -m collector --state-dir var/collector collect --once

# 持续轮询（默认 60 秒一圈，可 --interval 调整）
python -m collector --state-dir var/collector collect

# 查看企业 × 报告期 × 册别的当前状态（加 --json 导出）
python -m collector --state-dir var/collector status
python -m collector --state-dir var/collector pending

# 登记带适用范围的人工确认/排除；之后来信冲突不会自动冲掉它
python -m collector --state-dir var/collector decide \
  --enterprise C01 --period 2025 --part energy --kind confirm --rationale '依据...'

# 按 sha256（或前缀）把原信/MIME 部分的原始字节复制出来回查，不解析执行
python -m collector --state-dir var/collector export <sha256> ./回查目录

# 列出某册别全部判断可回到的原信、MIME 层与观察位置
python -m collector --state-dir var/collector evidence \
  --enterprise C01 --period 2025 --part energy
```

设计依据与状态口径见 docs/collector-design.md。要点：主题、文件名、Message-ID
不参与版本判断；转发者不是原签发者；局部撤回不连坐其他册别；“尚未收齐”与
“已确认撤回”严格区分；邮件内容只做文本展示与字节复制，不渲染、不执行、不访问链接。

## 检查来源与收集端

执行 python -m unittest discover -s tests -v。`test_source.py` 核对来源行为；
`test_mime_parse.py`、`test_materials.py`、`test_engine.py` 为收集端单元测试；
`test_collector_integration.py` 对真实运行的合成网关验证文件夹代际重建、409
重新枚举、中断续收与两阶段归属语义。

## 容器

执行 docker build -t esg-mail-acquisition .，然后 docker run --rm -p 8765:8765 esg-mail-acquisition。测试可用 docker run --rm esg-mail-acquisition python -m unittest discover -s tests -v。

source 提供来源服务，data 保存合成原始资料，collector 实现资料收集端，tests 核对来源行为与收集端判断；docs 记录字段、来源契约与收集端设计。不要将运行日志、凭据或采集数据提交到版本库。
