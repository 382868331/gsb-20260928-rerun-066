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
- 发布差异：`draft_diff(draft_id)` 对比草稿与当前有效图，报告新增依赖、被移除的依赖、失去来源的目标字段和即将出现的循环。
- 基线核对：每个草稿记录 `base_revision`；两个草稿基于同一旧版先后提交时，后一份发布会抛出 `StaleBaselineError`，需用户重新确认（`confirm_stale=True`）后才落盘，不会静默覆盖先发布的成果。
- 历史快照：每次发布与撤回都追加一条 `HistoryEntry`，内含发布前完整图快照；批量校验失败或保存中断时内存与磁盘整体回滚，历史图保持完整。
- 发布撤回：`withdraw_publish(history_id)` 软删除该次发布启用的边并恢复其停用的边，撤回本身也作为历史条目永久保留。
- 字段软删除：`delete_field` / `remove_field_from_draft` 只把字段标记为 `active=false`，证据边与历史保留；发布含字段删除的草稿时一并落盘，撤回时恢复。
- 两稿合并：`start_merge` 打开一个可持久化的合并会话，把两份已分别审查的草稿合成一份待发布方案；不同来源的修改共同保留。
- 冲突裁决：一方删除字段而另一方仍引用（`field_removed`）、一方删除映射而另一方保留（`mapping_dropped`）、双方替换同一映射且表达式不同（`mapping_replaced`）都会生成带影响链的冲突，必须由用户在 `left` / `right` / `drop` / `keep_baseline` 中显式选择，绝不按提交先后覆盖。
- 反复预览：`resolve_merge_conflict` 可逐个裁决，`merge_preview` 可任意次重算；尚未决定的部分不进入模拟图，已决定内容重开存储后也不丢失。预览会连同两边原修改一起解释新增依赖（带来源 `origin`）、新增循环、失去来源的目标与彻底无来源结果。
- 基线再确认：合并进行中基线推进时，`merge_preview` 标记 `baseline_moved`，依赖基线的已决冲突被置为 `needs_reconfirm`；与基线无关的决定继续保留，完成合并需重新确认受影响项并以 `confirm_stale=True` 落盘。
- 合并隔离：`complete_merge` 只产出新的待发布草稿（记录 `sources` 两边来源），`abandon_merge` 放弃会话；两份原草稿、审查依据和正式历史在保存重开后均保持完整。

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

# 发布后撤回，历史条目与图快照全部保留
service.withdraw_publish(entry.id)
service.history()
```

### 合并两份已审查草稿

见下文“两稿合并”说明：`start_merge` 打开可保存重开的合并会话，`merge_conflicts` / `merge_preview` 查看带影响链的冲突与循环、无来源结果，`resolve_merge_conflict` 逐个裁决，`complete_merge` 在全部冲突解决后产出新的待发布草稿，`abandon_merge` 放弃且不影响原草稿与历史。基线推进后只有依赖基线的决定需要重新确认（`needs_reconfirm`），其他选择保留。

```python
# 两份草稿可以分别删除字段、替换同一映射，或补充不同来源
service.create_draft("draft.a", "方案 A")
service.stage_transform(
    "draft.a", "transforms.segment.v2", "build_segment", "2.0.0",
    [("customers.v2.tier", "enriched.v1.segment", "coalesce(tier)")],
)
service.create_draft("draft.b", "方案 B")
service.remove_field_from_draft("draft.b", "orders.v1.region")

# 打开持久化合并会话；冲突带影响链（上下游路径、引用位置）
session = service.start_merge("merge.1", "draft.a", "draft.b", name="A+B")
for conflict in service.merge_conflicts("merge.1"):
    print(conflict.id, conflict.kind, conflict.options, conflict.impact_chain)

# 可逐个解决并反复预览；未决部分不会出现在预览的有效图中
service.resolve_merge_conflict("merge.1", "field:orders.v1.region", "left")
preview = service.merge_preview("merge.1")
print(preview.baseline_moved)
print(preview.added_dependencies)   # 每条带来源 origin: draft.a / draft.b / both
print(preview.new_cycles)
print(preview.source_less_results)
print(preview.unresolved_conflicts)

# 全部冲突解决后生成新的待发布草稿；基线变化时先重认 needs_reconfirm 冲突
merged = service.complete_merge(
    "merge.1", "draft.merged", name="A+B", confirm_stale=True
)
service.publish_draft("draft.merged")

# 放弃合并不影响原草稿、审查依据与正式历史
service.abandon_merge("merge.1")
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
python -m lineage.cli --store lineage.json draft-diff d1
python -m lineage.cli --store lineage.json publish-draft d1 --confirm-stale --note 'merge tier'
python -m lineage.cli --store lineage.json withdraw-publish history:r1:publish:d1
python -m lineage.cli --store lineage.json history
python -m lineage.cli --store lineage.json remove-field-draft d1 orders.v1.region
python -m lineage.cli --store lineage.json merge-start m1 d1 d2 --name 'd1+d2'
python -m lineage.cli --store lineage.json merge-conflicts m1
python -m lineage.cli --store lineage.json merge-resolve m1 field:orders.v1.region left
python -m lineage.cli --store lineage.json merge-preview m1
python -m lineage.cli --store lineage.json merge-complete m1 draft.merged --confirm-stale
python -m lineage.cli --store lineage.json merge-abandon m1
```

安装为包后也可以使用 `lineage --store lineage.json ...`。

## 持久化格式

JSON 文件包含 `objects`、`fields`（含软删除 `active` 标记）、`transforms`、`contributions` 四个图集合，以及 `drafts`（草稿方案、字段删除与来源记录）、`merges`（合并会话、冲突与已做决定）、`history`（含完整图快照的发布/撤回记录）和 `revision`（基线版本号）。保存采用临时文件加原子替换，适合当前单机、低频提交的工具集成场景。其他处理工具可以直接复用 `LineageService` 提交同构关系；后续也可以把 `JsonLineageStore` 替换成数据库仓储，而保持服务接口不变。

## 测试

```bash
python -m unittest discover -s tests -v
```
