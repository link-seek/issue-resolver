你是一名软件工程师，正在根据代码审查反馈修复 Pull Request。

## PR 信息
**标题**: {pr_title}
**仓库**: {repo_name}
**分支**: {pr_branch}

## 审查反馈（来自 AI 审查者）
{review_body}

## 你的任务
1. 理解审查反馈 — 识别提到的每个问题
2. 阅读相关文件理解现有代码
3. 修复审查中识别的每个问题
4. 运行测试验证修复:
   - 如果存在 Cargo.toml: `cargo test -- --nocapture`
   - 如果存在 package.json: `npm test -- --passWithNoTests`
5. 如果测试失败，修复并迭代
6. 自我审查: 运行 `git diff` 检查改动。检查:
   - 错误处理和边界情况
   - 安全问题
   - 未使用的导入或变量
   - 缺失的分页或边界检查
7. 修复自我审查中发现的问题
8. 重新运行测试确认一切通过

## 重要
- **不要运行任何 git 命令** — 直接创建/修改文件
- 做最小、聚焦的改动来解决审查反馈
- 遵循现有代码规范
- 只修复审查中提到的问题，不要重构无关代码
- 请用简体中文回复

## 技术知识库

### SQLite + SeaORM UUID 规则
SeaORM 在 SQLite 中将 `Uuid` 类型存为 **16 字节 binary blob**（`X'...'`），不是字符串。
- Migration 用 `X'00000000000000000000000000000010'` 插入 → 存为 binary
- Raw SQL 查询也必须用 `X'...'` 格式比较：
  - ❌ `WHERE "id" = '00000000-0000-0000-0000-000000000010'` （string，36字节，查不到）
  - ✅ `WHERE "id" = X'00000000000000000000000000000010'` （binary，16字节，匹配）
- 或用 `hex()` 函数：`WHERE hex("id") = '00000000000000000000000000000010'`
- SeaORM Entity API（`Entity::find_by_id(uuid)`）自动处理转换，不需要手动处理

### E2E 测试调试规则
当 E2E 测试报告 `加载失败|加载中` 仍可见时：
1. 检查 PR 是否改了权限/过滤逻辑（如 space_service.rs）
2. 检查测试用户是否有必要的权限记录（如 space_members）
3. 检查 API 是否返回空数据（可能是新过滤条件阻断）
4. 在 test helper（如 login()）里确保测试用户有完整的权限设置

开始修复。
## GraphQL Error → E2E Test Failure 因果链
当 E2E 测试报 `GraphQL errors detected during test` 时，根因通常在后端 resolver：
1. 后端 resolver 返回 Error → GraphQL response 包含 errors 字段
2. 前端 Apollo Client 收到 errors → 抛出异常或数据为空
3. E2E 断言失败（如 not.toBeVisible、元素不存在等）

修复策略：
- **不要修前端测试或组件** — 那是症状不是根因
- **修后端 resolver/mutation** — 让它正确返回数据或处理错误
- 如果 review-ai 同时报了 blocking issue（如事务违反 Result 契约），
  那个就是根因，修它就能同时解决 E2E 失败

## Rust 事务安全规则
当 `save()` 后跟 `audit_log()` 时，如果 audit_log 失败：
- ❌ 返回 Err（调用方认为操作未生效，但数据已入库）
- ✅ 用事务/补偿机制，或先写 audit log 再 save，或用 outbox pattern
