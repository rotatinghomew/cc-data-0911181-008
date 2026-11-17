# 关联申报中服务

补助审核台账检查附件、展示原始到场依据并按金额生成当前复核队列，保留历史抽审来源。

## 运行环境

使用 Python 3.11，仅依赖标准库，无外部服务、账号或网络数据。
示例数据使用虚构标识和本地合成记录，不涉及真实学员。

```bash
python3 -m app check
python3 -m app summary
python3 -m app serve --host 127.0.0.1 --port 8080
```

服务提供只读 JSON 查询：`/api/checks`, `/api/queue`, `/api/leads`, `/api/plan`,
`/api/leads/evaluation`。
`/health` 显示存活及表计数，`/api/records?table=表名` 可查看已知数据表。
详细字段语义、线索规则、选择偏差处理见 `docs/数据契约.md`。

## 关联线索与复核清单

`/api/leads` 用班次、到场记录、教师、代办人和场地关系把需要一起查看的申报分组，
给出风险、理由和保留意见。线索只作人工复核依据，不作违规认定，不处罚，不读取外部个人资料。

`/api/plan` 在每日名额内给出复核清单：同一关联组有当日名额上限，被限流的
高风险班次列在 `deferred` 中；已复核班次不进入清单，回避的班次仍待复核。

`/api/leads/evaluation` 只比较新旧排序价值：未复核班次按未知处理，默认只在
历史已复核子集内比较，避免把过去按金额挑选样本的习惯固化。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests smoke.py
python3 smoke.py
```

测试从实际数据装载进入领域计算、报告与真实 HTTP 服务，并用临时副本验证错误输入。
冒烟程序动态分配本地端口，启动服务后读取全部业务查询并原样输出 JSON，随后正常关闭。
查询不写回数据，因此可以直接重复运行；验证不会访问项目外文件。

## 容器

```bash
docker build -t grant-review:local .
docker run --rm --network none grant-review:local
docker run --rm --network none grant-review:local python3 smoke.py
```

镜像只封装本应用，测试使用回环网络，不依赖宿主端口。
无第三方包依赖，`requirements.lock` 记录此状态。

## 业务办理

review_dispatch.assign 逐笔派工并避开学校及教师、代办人、场地的利益关联；
review_dispatch.dispatch 按线索顺序批量派工，跳过有利益关联或名额不足的候选并返回原因；
review_dispatch.recuse 登记回避、释放当日名额并保持班次待复核。
document_requests.request 仅向本校索取该笔记录；document_requests.request_group
对多校关联组按学校分别索件，每校只收到本校签到明细。review_feedback.resolve
只登记单笔结论，不连带结案同组其他班次。

业务操作以 JSON 对象 POST 到 `/api/workflow/操作名`，下列操作保留现有函数入参和行为。

- `review_dispatch.assign`
- `document_requests.request`
- `review_feedback.resolve`

新增操作：

- `review_dispatch.dispatch`
- `review_dispatch.recuse`
- `document_requests.request_group`

业务贯通用例见 `tests/test_workflows.py` 与 `tests/test_leads.py`，其中为可直接运行的本地调用。默认状态存于内存；需要重启后保留时，可运行 `python3 -m app serve --state /tmp/learning-service.sqlite3`。状态库只保留本机操作，不向外部发送数据。
