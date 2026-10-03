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
- 草稿方案：`create_draft` / `stage_transform` 在正式图之外维护多个互不干扰的待审映射方案，试算关系不进入 `upstream` / `impact` 等普通查询。
- 草稿删除：`stage_field_deletion` 只在草稿中软删字段；发布前有效图和普通血缘查询保持不变。
- 发布差异：`draft_diff(draft_id)` 对比草稿与当前有效图，报告新增依赖、被移除的依赖、失去来源的目标字段和即将出现的循环。
- 双稿合并：`begin_merge` 生成独立待发布草稿和可持久化会话；不同来源共同保留，字段删除/引用、同一映射或同一转换证据冲突会列出影响链，由用户逐项选择。
- 反复预览：`preview_merge` / `resolve_merge_conflict` 可重复调用；未决冲突不进入输出草稿，已做选择按冲突指纹保留。基线变化只要求重确认与相关边或字段有关的决定。
- 合并隔离：放弃合并只丢弃合并输出草稿，两份原草稿和审查依据不变；正式历史只在 `publish_merge` 后追加。
- 基线核对：每个草稿记录 `base_revision`；两个草稿基于同一旧版先后提交时，后一份发布会抛出 `StaleBaselineError`，需用户重新确认（`confirm_stale=True`）后才落盘，不会静默覆盖先发布的成果。
- 历史快照：每次发布与撤回都追加一条 `HistoryEntry`，内含发布前完整图快照；批量校验失败或保存中断时内存与磁盘整体回滚，历史图保持完整。
- 发布撤回：`withdraw_publish(history_id)` 软删除该次发布启用的边并恢复其停用的边，撤回本身也作为历史条目永久保留。
- 字段拆分迁移：`create_field_split` 只建立独立拆分声明；`record_split_decision` 采用人工明确的新字段解释，`record_manual_split_dependency` 标记仍需判断，系统不会按字段名自动映射。
- 分批采用：`plan_split_migration` 先展示范围内未决依赖、共享中间结果冲突和影响链；`create_split_migration_batch` 生成独立草稿，发布后同一正式图中可同时保留旧解释路径与新解释路径。
- 撤回复核：迁移批次保存采用的解释和发布时间；撤回某批后，依赖该批决定的后续已发布批次会被标记为 `requires_recheck`，来源前后对比保留在批次记录中。
- 旧字段退役：`create_split_retirement_draft` 只在所有实际依赖均已处置且没有剩余旧边时生成软删草稿；失败发布沿用原子回滚，不落部分正式图。

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
```

### 草稿方案与发布

```python
# 在正式图之外并行维护多个方案
service.create_draft("draft.merge_tier", "把 tier 并入 segment")
service.stage_transform(
    "draft.merge_tier",
    "transforms.segment.v2",
    "build_segment",
    "2.0.0",
    [("customers.v2.tier", "enriched.v1.segment", "coalesce(tier, 'unknown')")],
    code_ref="jobs/build_segment.py@9b3d1e",
)

# 发布前对比：新增依赖、失去来源的字段、新出现的循环
diff = service.draft_diff("draft.merge_tier")
print(diff.added_dependencies, diff.lost_sources, diff.new_cycles)

# 发布；若基线已被其他草稿推进，会抛出 StaleBaselineError
entry = service.publish_draft("draft.merge_tier", note="merge tier")
# 确认差异后可强制发布：service.publish_draft("draft.merge_tier", confirm_stale=True)

# 合并两份分别审查的草稿
service.stage_field_deletion("draft.remove_region", "orders.v1.region")
preview = service.begin_merge(
    "merge.20261002",
    "draft.release.20261002",
    "draft.merge_tier",
    "draft.remove_region",
)
for conflict_id in preview.unresolved_conflicts:
    preview = service.resolve_merge_conflict(
        "merge.20261002", conflict_id, "keep_field"  # 或 delete_field / left / right
    )
# preview.new_cycles、preview.lost_sources、preview.explanations 会连同两边原修改解释影响
entry = service.publish_merge("merge.20261002", confirm_stale=True)

# 发布后撤回，历史条目与图快照全部保留
service.withdraw_publish(entry.id)
service.history()
```

### 字段拆分与下游分批迁移

```python
service.create_field_split(
    "split.contact",
    "raw.v1.contact",
    ["raw.v1.email", "raw.v1.phone"],
)

# 明确某条旧依赖采用 email；shared intermediate 可指定新的并行结果。
service.record_split_decision(
    "split.contact",
    "t.profile",
    "mid.v1.profile",
    "raw.v1.email",
    new_target_field_id="mid.v1.profile_email",
    rationale="customer path explicitly uses email",
)

# 语义不明的依赖保持 manual，不会接到第一个新字段。
service.record_manual_split_dependency(
    "split.contact", "t.billing", "out.v1.billing", "phone or email must be reviewed"
)

print(service.split_report("split.contact"))
plan = service.plan_split_migration(
    "split.contact",
    ["mid.v1.profile", "mid.v1.profile_email", "out.v1.customer_email"],
)
if not plan["unresolved"] and not plan["conflicts"]:
    batch = service.create_split_migration_batch(
        "batch.customer.email",
        "split.contact",
        ["mid.v1.profile", "mid.v1.profile_email", "out.v1.customer_email"],
        additional_paths=[
            {
                "transform_id": "t.customer",
                "source_field_id": "mid.v1.profile_email",
                "target_field_id": "out.v1.customer_email",
                "expression": "email_profile",
            }
        ],
    )
    entry = service.publish_split_migration_batch("batch.customer.email")
    service.withdraw_publish(entry.id)

# 全部依赖迁移并核对 report 后再退役旧字段。
service.create_split_retirement_draft("split.contact")
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
python -m lineage.cli --store lineage.json create-draft d1 proposal-a
python -m lineage.cli --store lineage.json stage-transform d1 t.segment.v2 build_segment 2.0.0 --mapping customers.v2.tier enriched.v1.segment 'coalesce(tier)'
python -m lineage.cli --store lineage.json stage-field-deletion d1 orders.v1.region
python -m lineage.cli --store lineage.json draft-diff d1
python -m lineage.cli --store lineage.json publish-draft d1 --confirm-stale --note 'merge tier'
python -m lineage.cli --store lineage.json begin-merge m1 release d1 d2
python -m lineage.cli --store lineage.json preview-merge m1
python -m lineage.cli --store lineage.json resolve-merge m1 'merge:m1:deleted-field:orders.v1.region' keep_field
python -m lineage.cli --store lineage.json publish-merge m1 --confirm-stale --note 'release plan'
python -m lineage.cli --store lineage.json abandon-merge m1
python -m lineage.cli --store lineage.json withdraw-publish history:r1:publish:d1
python -m lineage.cli --store lineage.json create-field-split split.contact raw.v1.contact raw.v1.email raw.v1.phone
python -m lineage.cli --store lineage.json record-split-decision split.contact t.profile mid.v1.profile raw.v1.email --new-target-field-id mid.v1.profile_email --rationale reviewed
python -m lineage.cli --store lineage.json record-split-manual split.contact t.billing out.v1.billing 'needs review'
python -m lineage.cli --store lineage.json split-report split.contact
python -m lineage.cli --store lineage.json plan-split-migration split.contact mid.v1.profile mid.v1.profile_email out.v1.customer_email
python -m lineage.cli --store lineage.json create-split-batch batch.customer split.contact mid.v1.profile mid.v1.profile_email out.v1.customer_email
python -m lineage.cli --store lineage.json publish-split-batch batch.customer
python -m lineage.cli --store lineage.json create-split-retirement split.contact
python -m lineage.cli --store lineage.json history
```

安装为包后也可以使用 `lineage --store lineage.json ...`。

## 持久化格式

JSON 文件包含 `objects`、`fields`、`transforms`、`contributions` 四个图集合，以及 `drafts`（草稿方案，含草稿软删字段）、`field_splits`（字段拆分声明与人工解释）、`migration_batches`（分批迁移、来源对比和撤回审计）、`merge_sessions`（合并会话与逐项决定）、`history`（含完整图快照的发布/撤回记录）和 `revision`（基线版本号）。保存采用临时文件加原子替换，适合当前单机、低频提交的工具集成场景。其他处理工具可以直接复用 `LineageService` 提交同构关系；后续也可以把 `JsonLineageStore` 替换成数据库仓储，而保持服务接口不变。

## 测试

```bash
python -m unittest discover -s tests -v
```
