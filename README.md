# 字段级数据血缘追踪系统

这是一个无外部依赖的 Python 核心库，用 JSON 保存数据对象、转换版本和字段级引用关系，不需要连接真实数据仓库。其他处理工具可以实例化同一模型，提交带转换证据的来源关系。

## 数据模型

- DataObject：数据对象，包含稳定 id、业务名 name、版本 version、分支 branch 和字段清单。
- Transformation：转换版本，包含版本、分支和 code_ref 等证据定位信息。
- Relation：一条有向边，从来源字段指向目标字段；同一个目标可以保留多个来源。
- FieldRef(object_id, field_name)：血缘图中的字段节点。对象 ID 不同即不同节点，避免同名不同版本字段混淆。

## 快速使用

    from lineage import (
        DataObject, LineageStore, Relation, Transformation,
        analyze_impact, trace_lineage,
    )

    store = LineageStore("lineage.json")
    store.add_object(DataObject(
        id="orders_v1", name="orders", version="v1", fields=["amount", "country"]
    ))
    store.add_object(DataObject(
        id="report_v1", name="report", version="v1", fields=["customer_amount"]
    ))
    store.add_transformation(Transformation(
        id="build_report", name="build_report", version="3", code_ref="jobs/report.py:10"
    ))

    amount = store.field("orders_v1", "amount")
    country = store.field("orders_v1", "country")
    result = store.field("report_v1", "customer_amount")

    store.add_relations([
        Relation(amount, result, "build_report", expression="orders.amount"),
        Relation(country, result, "build_report", expression="orders.country"),
    ])

    impact = analyze_impact(store, amount)
    upstream = trace_lineage(store, result)
    store.save()

impact.direct 是一跳下游，impact.transitive 是两跳及以上下游。每个 Dependency 都带 evidence，其中依次列出具体 Relation，可定位到 transformation_id、表达式和转换记录中的 code_ref。

## 关键语义

- 多来源合成：同一目标字段的多条 Relation 全部保留，不会被覆盖。
- 精确撤回：delete_transformation(id) 只删除该转换贡献的边，并标记转换删除时间；其他转换的关系保留。
- 影响分析：analyze_impact 计算下游，trace_lineage 计算上游；结果区分直接与传递依赖。
- 循环安全：遍历使用 BFS 和已访问节点集合，环不会导致无限循环，每个字段只返回一次。
- 分支汇合：branch_convergence(store, target) 按来源对象分支聚合直接入边并保留证据。
- 同名版本核对：find_field(name, field_name, version, branch) 和 compare_version_fields(...) 返回具体对象 ID 上的字段节点。

## 测试

运行：python -m unittest discover -s tests -v
