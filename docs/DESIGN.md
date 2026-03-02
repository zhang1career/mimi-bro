# 设计约定

## Knowledge API 接口使用规范

### 接口分类

| 接口 | 查询方式 | 适用场景 |
|------|----------|----------|
| `/knowledge/some_like?summary=...` | 语义相似度搜索 | 业务命令（用户交互） |
| `/knowledge?title=...` | 标题前缀匹配 | 后台命令（程序调用） |

### 约定

1. **业务命令**（定义在 `src/broker/cli/main.py`）：
   - 可使用 `/knowledge/some_like` 接口
   - 适用于需要语义搜索的场景

2. **后台命令**（定义在其他文件，如 `src/broker/cli/skill.py`）：
   - 不使用 `/knowledge/some_like` 接口
   - 使用 `/knowledge?title=...` 按标题精确查询
   - 原因：后台命令已知 skill_id（对应 title），无需语义搜索

### 示例

```python
# 业务命令 (main.py) - 语义搜索
items = _fetch_some_like("用户认证")  # 按语义相似度搜索

# 后台命令 (skill.py, sync.py) - 精确查询
items = _fetch_by_title("backend-dev")  # 按 title 前缀匹配
```

### 相关函数

- `_fetch_some_like(summary)`: 语义搜索，用于业务命令
- `_fetch_by_title(title)`: 标题查询，用于后台命令
