# 字段级数据血缘追踪系统

这是一个不依赖真实数据仓库的 Python 最小实现，用本地 JSON 文件保存：

- 数据对象版本：`DataObject`，包含对象名、版本和分支。
- 字段：`Field`，字段 ID 全局唯一，字段名只在单个对象版本内唯一。
- 转换版本：`TransformVersion`，记录转换名称、版本和代码证据，例如 `etl/join.py@sha`。
- 字段贡献关系：`Contribution`，一条有向边表示某个转换让一个来源字段贡献给一个目标字段。

## 设计要点

- 多来源合成：一个目标字段可以同时保留多条入边，不会被覆盖。
- 精准撤回：删除转换版本只停用该转换贡献的边；其他转换的边保持有效。
- 原子提交：一次 `submit_transform` 或替换映射是一个事务，所有映射先校验、再随单次原子保存整体生效；提交失败时内存与磁盘都回到提交前版本，不会残留前半条关系。
- 整组替换：`replace_transform_mappings` 在同一图版本内用一组新边替换某转换的全部边，旧边软删除、相同两端的边复用并更新，新旧贡献不会短暂同时有效。
- 可恢复审计：删除是 `active=false` 的软删除，历史记录仍保存在 JSON 中。
- 失效隔离：引用了已不存在字段的历史边会保留在文件中，但不参与 `upstream`/`impact` 等有效查询。
- 来源查询：`upstream(field_id)` 返回字段的直接来源和传递来源。
- 影响分析：`impact(field_id)` 返回直接下游和传递下游。
- 转换证据：每条查询到的边都带 `transform_id`、表达式和转换版本上的 `code_ref`。
- 分支汇合：对象包含 `branch`，可用稳定的字段 ID 显式汇合多个分支。
- 同名版本核对：`field_identity_report(field_id)` 列出同名字段所属对象、版本和分支，避免只按字段名匹配。
- 循环安全：BFS 遇到回到起点的环边不会无限扩展；结果的 `cycles` 会报告可达环。

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
service.delete_transform("transforms.segment.v1")

# 整组替换该转换的贡献关系（单事务，失败完全不生效）
service.replace_transform_mappings(
    "transforms.segment.v1",
    [
        ("orders.v1.region", "enriched.v1.segment", "lower(region)"),
    ],
)
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
python -m lineage.cli --store lineage.json replace-mappings t.segment.v1 --mapping orders.v1.region enriched.v1.segment 'lower(region)' --mapping customers.v2.tier enriched.v1.segment "coalesce(tier,'?')"
python -m lineage.cli --store lineage.json list contributions
```

安装为包后也可以使用 `lineage --store lineage.json ...`。

## 持久化格式

JSON 文件包含四个顶层集合：`objects`、`fields`、`transforms`、`contributions`。保存采用临时文件（写入后 `fsync`）加 `os.replace` 原子替换，适合当前单机、低频提交的工具集成场景。任何批量写操作在内存快照上执行并只保存一次：保存中途崩溃时已落盘的仍是上一个完整版本，内存也会回滚。其他处理工具可以直接复用 `LineageService` 提交同构关系；后续也可以把 `JsonLineageStore` 替换成数据库仓储，而保持服务接口不变。

## 测试

```bash
python -m unittest discover -s tests -v
```
