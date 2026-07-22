# SQL Server 候选校验能力边界

日期：2026-07-21

## 保存前的确定性保证

生成主流程不会在候选阶段写入 SQLite 当前有效制品，也不会部署持久化 SQL Server 存储过程。每个候选按以下顺序处理：

1. QuerySpec 严格结构校验；
2. 从 `sys.schemas`、`sys.objects`、`sys.columns`、`sys.types` 和扩展属性读取实际引用对象；
3. SQL 安全检查；
4. SQL Server 静态编译和结果元数据检查；
5. 名称、参数、来源、写入范围、输出和 Oracle 规则契约检查；
6. 回滚事务内的 SP/Oracle 业务比较；
7. 整批通过后在一个 SQLite 事务中替换当前会话制品。

SchemaEvidence 只包含 QuerySpec 实际引用对象的结构，不读取业务行。对象和字段采用 schema-qualified 精确绑定；近似名称只作为错误提示，不会自动替换。指纹由排序后的对象及字段结构计算，不包含捕获时间。

独立 Oracle 使用 `sys.sp_describe_first_result_set` 绑定对象、字段、参数并取得第一结果集元数据，不执行查询。

SP 候选使用会话级本地临时过程：创建后开启 `SHOWPLAN_XML` 对代表性 `EXEC` 做计划编译，再通过 `sys.sp_describe_first_result_set` 获取结果元数据。过程主体不会被实际执行；成功、异常路径都会尝试关闭 SHOWPLAN、删除临时过程并关闭连接。

## 已知边界

SQL Server 存在 deferred name resolution、条件分支和动态行为边界。项目禁止动态 SQL，并先用 QuerySpec/SchemaEvidence 约束允许对象；但 SHOWPLAN 是否覆盖特定 SQL Server 版本中的全部条件分支，只能由隔离测试库中的集成探针确认。

因此：

- 未运行集成探针时，不把 SHOWPLAN 描述为对任意 T-SQL 的完整静态证明；
- 当前强保证限于项目允许的静态 SQL 子集、QuerySpec 精确绑定、SQL Server describe/SHOWPLAN 与回滚业务校验的组合；
- 真实目标数据库返回 207 或 208 时，只刷新一次 SchemaEvidence，之后重新执行全部闸门；
- Schema 刷新后仍未解析、业务结果无法归因或分支能力不明确时，不保存候选，返回失败或 `needs_review`；
- 部署前重新捕获 Schema 指纹，并核对保存的 bundle 哈希。任一变化都要求重新验证。

## 隔离集成探针

`test_sqlserver_compile_integration.py` 默认跳过。只有同时满足以下条件才允许运行：

- `RUN_SQLSERVER_COMPILE_INTEGRATION=1`；
- 配置中的 `environment=test`；
- 数据库名非空且明确为隔离测试库；
- 用户明确授权该外部数据库操作。

探针不得指向真实业务数据库。探针负责验证 207/208、参数和结果元数据、写过程不执行、条件分支边界以及临时对象清理。未获得授权时，离线测试只验证接口、闸门顺序、清理控制流和默认跳过行为。
