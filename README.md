# 字段级数据血缘追踪系统

这是一个不依赖真实数据仓库的 Python 最小实现，用本地 JSON 文件保存：

- 数据对象版本：`DataObject`，包含对象名、版本和分支。
- 字段：`Field`，字段 ID 全局唯一，字段名只在单个对象版本内唯一。
- 转换版本：`TransformVersion`，记录转换名称、版本和代码证据，例如 `etl/join.py@sha`。
- 字段贡献关系：`Contribution`，一条有向边表示某个转换让一个来源字段贡献给一个目标字段。

## 设计要点

- 多来源合成：一个目标字段可以同时保留多条入边，不会被覆盖。
- 精准撤回：删除转换版本只停用该转换贡献的边；其他转换的边保持有效。
- 整批原子提交：`submit_transform` 先校验全部映射再一次性落盘；任一映射非法或保存中断时，内存与磁盘都不保留该批任何部分。
- 原子替换：`replace_transform` 在一次保存内停用旧贡献并启用新贡献，新旧边不会短暂混在同一版本。
- 可恢复审计：删除是 `active=false` 的软删除，历史记录仍保存在 JSON 中。
- 悬空引用隔离：引用了已不存在字段或转换的边不参与有效影响查询，但记录保留。
- 来源查询：`upstream(field_id)` 返回字段的直接来源和传递来源。
- 影响分析：`impact(field_id)` 返回直接下游和传递下游。
- 转换证据：每条查询到的边都带 `transform_id`、表达式和转换版本上的 `code_ref`。
- 分支汇合：对象包含 `branch`，可用稳定的字段 ID 显式汇合多个分支。
- 同名版本核对：`field_identity_report(field_id)` 列出同名字段所属对象、版本和分支，避免只按字段名匹配。
- 循环安全：BFS 遇到回到起点的环边不会无限扩展；结果的 `cycles` 会报告可达环。

## 待审草稿与发布历史

多个映射方案可以作为**草稿**同时存在、彼此隔离，正式图之外的试算关系不会进入 `upstream`/`impact` 等普通查询：

- `create_draft`：以当前有效图修订号为基线，提交一个或多个转换替换方案，覆盖字段替换、多来源合并和反馈边；整批先校验后保存，任何映射非法或保存中断都不留下草稿、不改动有效图。
- `draft_diff`：对当前有效图试算差异，返回 `added_edges`/`removed_edges`、`new_dependencies`（新增的传递依赖）、`lost_sources`（失去来源的目标字段及剩余来源，`orphaned` 标记完全失去来源的结果）和 `cycles`（发布后存在的循环，并标注是否为新增循环）。
- `publish_draft`：只有基线仍是当前修订号时才发布为新的有效图修订；同一份旧版基线被他人抢先发布时抛出 `BaselineChangedError`，后一份草稿不会覆盖前一份成果。
- `rebase_draft`：用户查看刷新后的差异并重新确认后，把草稿锚定到新基线；刷新差异会显式暴露与已发布方案的冲突（例如新方案会移除别人新增的边）。
- `withdraw_revision`：撤回一次发布，把图恢复到该修订发布之前的完整快照；撤回本身也是一个新修订，发布、撤回的前后完整图都永久保留在历史时间线上，可用 `revision_graph` 审计。

## Python API

```python
from lineage import JsonLineageStore, LineageService

service = LineageService(JsonLineageStore("lineage.json"))

service.create_object("orders.v1", "orders", "v1", branch="main")
service.create_object("customers.v2", "customers", "v2", branch="main")
service.create_object("enriched.v1", "enriched_orders", "v1", branch="main")

service.create_field("orders.v1.region", "orders.v1", "region")
service.create_field("customers.v2.tier", "customers.v2", "tier")
service.create_field("enriched.v1.segment", "enriched.v1", "segment")

service.submit_transform(
    "transforms.segment.v1",
    "build_segment",
    "1.0.0",
    [
        ("orders.v1.region", "enriched.v1.segment", "region_upper(region)"),
        ("customers.v2.tier", "enriched.v1.segment", "coalesce(tier, 'unknown')"),
    ],
    code_ref="jobs/build_segment.py@4f1a2c",
)

service.upstream("enriched.v1.segment")
service.impact("customers.v2.tier")
service.field_identity_report("customers.v2.tier")
service.replace_transform(
    "transforms.segment.v1",
    [("orders.v1.region", "enriched.v1.segment", "lower(region)")],
)
service.delete_transform("transforms.segment.v1")

# 待审草稿：试算、差异、乐观发布与撤回
draft = service.create_draft(
    "drafts.segment.v2",
    [
        {
            "transform_id": "transforms.segment.v1",
            "mappings": [
                ("orders.v1.region", "enriched.v1.segment", "lower(region)"),
                ("customers.v2.tier", "enriched.v1.segment", "coalesce(tier, 'unknown')"),
            ],
        },
        {  # 也可以在草稿中引入一个全新的转换（需要 name/version）
            "transform_id": "transforms.feedback.v1",
            "name": "segment_feedback",
            "version": "1.0.0",
            "mappings": [("enriched.v1.segment", "orders.v1.region", "feedback(segment)")],
        },
    ],
)
service.draft_diff("drafts.segment.v2")
try:
    service.publish_draft("drafts.segment.v2")
except BaselineChangedError as conflict:
    # 基线已被另一份草稿推进：重新看差异、确认后再发布
    service.draft_diff("drafts.segment.v2")
    service.rebase_draft("drafts.segment.v2")
    service.publish_draft("drafts.segment.v2")
service.withdraw_revision(1)  # 撤回发布，历史图仍可通过 revision_graph 查看
```

建议由提交方生成稳定、可读且全局唯一的 ID，例如：

- 对象：`database.schema.table.version`，如 `dw.dim_customer.v20260929`。
- 字段：在对象 ID 后追加字段名，如 `dw.dim_customer.v20260929.tier`。
- 转换：`团队.任务.版本`，如 `marketing.segment_builder.v3`。

这样不同分支、同名不同版本字段不会被误认为同一个节点。

## CLI

```bash
python -m lineage.cli --store lineage.json create-object orders.v1 orders v1 --branch main
python -m lineage.cli --store lineage.json create-field orders.v1.region orders.v1 region
python -m lineage.cli --store lineage.json create-transform t.segment.v1 build_segment 1.0.0 --code-ref jobs/build_segment.py@4f1a2c
python -m lineage.cli --store lineage.json add-contribution orders.v1.region enriched.v1.segment t.segment.v1 --expression 'region_upper(region)'
python -m lineage.cli --store lineage.json upstream enriched.v1.segment
python -m lineage.cli --store lineage.json impact orders.v1.region
python -m lineage.cli --store lineage.json delete-transform t.segment.v1
python -m lineage.cli --store lineage.json replace-transform t.segment.v1 --mapping orders.v1.region enriched.v1.segment 'lower(region)'
python -m lineage.cli --store lineage.json list contributions

# 草稿与发布历史
python -m lineage.cli --store lineage.json draft-create d1 t.segment.v1 --mapping orders.v1.region enriched.v1.segment 'lower(region)'
python -m lineage.cli --store lineage.json draft-diff d1
python -m lineage.cli --store lineage.json draft-publish d1
python -m lineage.cli --store lineage.json draft-rebase d1   # 基线变化后重新确认
python -m lineage.cli --store lineage.json withdraw-revision 1
python -m lineage.cli --store lineage.json revision-graph 1
python -m lineage.cli --store lineage.json list revisions
```

安装为包后也可以使用 `lineage --store lineage.json ...`。

## 持久化格式

JSON 文件包含 `objects`、`fields`、`transforms`、`contributions` 四个有效图集合，以及 `drafts`（待审方案）、`revisions` 和 `current_revision`（发布时间线，每个修订保存发布前后的完整图快照）。保存采用临时文件加原子替换，适合当前单机、低频提交的工具集成场景。草稿保存、草稿发布和撤回都遵循「先校验/先快照、单次落盘、失败回滚内存与磁盘」的原则；批量错误或落盘失败不会留下半截关系，撤回也不会抹掉任何历史修订。其他处理工具可以直接复用 `LineageService` 提交同构关系；后续也可以把 `JsonLineageStore` 替换成数据库仓储，而保持服务接口不变。

## 测试

```bash
python -m unittest discover -s tests -v
```
