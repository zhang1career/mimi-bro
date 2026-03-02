"""
依赖分析模块。

分析子任务间的依赖关系：
1. 代码依赖分析：扫描 scope 目录的 import/require 语句
2. 任务描述分析：解析 requirement 字段的关键词
3. 综合判断：生成依赖图供人工确认
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DependencyEdge:
    """依赖边"""

    from_task: str
    to_task: str
    reason: str  # code_import, requirement_keyword, file_overlap
    details: str = ""


@dataclass
class DependencyGraph:
    """依赖图"""

    nodes: list[str] = field(default_factory=list)
    edges: list[DependencyEdge] = field(default_factory=list)

    def add_node(self, node: str) -> None:
        if node not in self.nodes:
            self.nodes.append(node)

    def add_edge(self, edge: DependencyEdge) -> None:
        for e in self.edges:
            if e.from_task == edge.from_task and e.to_task == edge.to_task:
                return
        self.edges.append(edge)

    def get_dependents(self, task_id: str) -> list[str]:
        """获取依赖指定任务的所有任务"""
        return [e.to_task for e in self.edges if e.from_task == task_id]

    def get_dependencies(self, task_id: str) -> list[str]:
        """获取指定任务依赖的所有任务"""
        return [e.from_task for e in self.edges if e.to_task == task_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": [
                {
                    "from": e.from_task,
                    "to": e.to_task,
                    "reason": e.reason,
                    "details": e.details,
                }
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DependencyGraph:
        graph = cls()
        for node in data.get("nodes", []):
            graph.add_node(node)
        for edge_data in data.get("edges", []):
            graph.add_edge(DependencyEdge(
                from_task=edge_data["from"],
                to_task=edge_data["to"],
                reason=edge_data.get("reason", "unknown"),
                details=edge_data.get("details", ""),
            ))
        return graph

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> DependencyGraph:
        data = json.loads(path.read_text())
        return cls.from_dict(data)


@dataclass
class SubtaskInfo:
    """子任务信息"""

    id: str
    skill: str
    requirement: str
    scope: str
    files: list[str] = field(default_factory=list)


class DependencyAnalyzer:
    """依赖分析器"""

    PYTHON_IMPORT_PATTERN = re.compile(
        r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
        re.MULTILINE,
    )
    JS_IMPORT_PATTERN = re.compile(
        r"""(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
        re.MULTILINE,
    )
    GO_IMPORT_PATTERN = re.compile(
        r"""import\s+(?:\(\s*([\s\S]*?)\s*\)|"([^"]+)")""",
        re.MULTILINE,
    )

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    def analyze_breakdown(
            self,
            breakdown_path: Path,
            use_explicit_deps: bool = True,
            analyze_code: bool = False,
    ) -> DependencyGraph:
        """
        分析 breakdown.json 生成依赖图。

        Args:
            breakdown_path: breakdown.json 路径
            use_explicit_deps: 是否使用显式 deps 字段（默认 True）
            analyze_code: 是否分析代码依赖（默认 False，仅当无显式 deps 时有用）
        """
        data = json.loads(breakdown_path.read_text())
        if not isinstance(data, list):
            raise ValueError("breakdown.json must be a list")

        subtasks = []
        explicit_deps: dict[str, list[str]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            subtask = SubtaskInfo(
                id=item.get("id", ""),
                skill=item.get("skill", ""),
                requirement=item.get("requirement", ""),
                scope=item.get("scope", "src"),
            )
            if subtask.id:
                subtask.files = self._collect_scope_files(subtask.scope)
                subtasks.append(subtask)
                explicit_deps[subtask.id] = list(item.get("deps") or [])

        graph = DependencyGraph()
        for subtask in subtasks:
            graph.add_node(subtask.id)

        if use_explicit_deps:
            _add_explicit_dependencies(explicit_deps, graph)

        if analyze_code:
            self._analyze_code_dependencies(subtasks, graph)
            _analyze_requirement_dependencies(subtasks, graph)

        return graph

    def _collect_scope_files(self, scope: str) -> list[str]:
        """收集 scope 目录下的所有代码文件"""
        scope_path = self.workspace / scope
        if not scope_path.exists():
            return []

        extensions = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java"}
        files = []

        if scope_path.is_file():
            files.append(str(scope_path.relative_to(self.workspace)))
        else:
            for f in scope_path.rglob("*"):
                if f.is_file() and f.suffix in extensions:
                    files.append(str(f.relative_to(self.workspace)))

        return files

    def _analyze_code_dependencies(
            self,
            subtasks: list[SubtaskInfo],
            graph: DependencyGraph,
    ) -> None:
        """分析代码依赖关系"""
        file_to_subtask: dict[str, str] = {}
        for subtask in subtasks:
            for f in subtask.files:
                file_to_subtask[f] = subtask.id

        for subtask in subtasks:
            imports = set()
            for f in subtask.files:
                file_path = self.workspace / f
                if file_path.exists():
                    imports.update(self._extract_imports(file_path))

            for imp in imports:
                dep_file = self._resolve_import_to_file(imp)
                if dep_file and dep_file in file_to_subtask:
                    dep_subtask = file_to_subtask[dep_file]
                    if dep_subtask != subtask.id:
                        graph.add_edge(DependencyEdge(
                            from_task=dep_subtask,
                            to_task=subtask.id,
                            reason="code_import",
                            details=f"{subtask.id} imports from {dep_file}",
                        ))

    def _extract_imports(self, file_path: Path) -> set[str]:
        """提取文件中的 import 语句"""
        imports = set()
        try:
            content = file_path.read_text(errors="ignore")
        except Exception:
            return imports

        suffix = file_path.suffix

        if suffix == ".py":
            imports.update(self._extract_python_imports(content))
        elif suffix in {".js", ".ts", ".jsx", ".tsx"}:
            imports.update(self._extract_js_imports(content))
        elif suffix == ".go":
            imports.update(self._extract_go_imports(content))

        return imports

    def _extract_python_imports(self, content: str) -> set[str]:
        """提取 Python import"""
        imports = set()
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.add(node.module.split(".")[0])
        except SyntaxError:
            for match in self.PYTHON_IMPORT_PATTERN.finditer(content):
                module = match.group(1) or match.group(2)
                if module:
                    imports.add(module.split(".")[0])
        return imports

    def _extract_js_imports(self, content: str) -> set[str]:
        """提取 JS/TS import"""
        imports = set()
        for match in self.JS_IMPORT_PATTERN.finditer(content):
            path = match.group(1) or match.group(2)
            if path and not path.startswith("."):
                continue
            if path:
                imports.add(path)
        return imports

    def _extract_go_imports(self, content: str) -> set[str]:
        """提取 Go import"""
        imports = set()
        for match in self.GO_IMPORT_PATTERN.finditer(content):
            if match.group(1):
                for line in match.group(1).splitlines():
                    line = line.strip().strip('"')
                    if line:
                        imports.add(line)
            elif match.group(2):
                imports.add(match.group(2))
        return imports

    def _resolve_import_to_file(self, import_path: str) -> str | None:
        """将 import 路径解析为文件路径"""
        if import_path.startswith("."):
            return None

        candidates = [
            import_path.replace(".", "/") + ".py",
            import_path.replace(".", "/") + "/__init__.py",
            import_path + ".ts",
            import_path + ".js",
            import_path + "/index.ts",
            import_path + "/index.js",
        ]

        for candidate in candidates:
            if (self.workspace / candidate).exists():
                return candidate

        return None


def analyze_dependencies(
        workspace: Path,
        breakdown_path: Path,
        output_path: Path | None = None,
) -> DependencyGraph:
    """
    分析 breakdown.json 中子任务的依赖关系。

    Args:
        workspace: 工作空间根目录
        breakdown_path: breakdown.json 路径
        output_path: 输出依赖图的路径（可选）

    Returns:
        DependencyGraph 对象
    """
    analyzer = DependencyAnalyzer(workspace)
    graph = analyzer.analyze_breakdown(breakdown_path)

    if output_path:
        graph.save(output_path)

    return graph


def _add_explicit_dependencies(
        explicit_deps: dict[str, list[str]],
        graph: DependencyGraph,
) -> None:
    """添加显式依赖到依赖图"""
    for subtask_id, deps in explicit_deps.items():
        for dep_id in deps:
            if dep_id in graph.nodes:
                graph.add_edge(DependencyEdge(
                    from_task=dep_id,
                    to_task=subtask_id,
                    reason="explicit_deps",
                    details=f"{subtask_id} explicitly depends on {dep_id}",
                ))


def _extract_keywords(text: str) -> list[str]:
    """提取文本中的关键词"""
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", text.lower())
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "must", "shall",
        "can", "need", "dare", "ought", "used", "to", "of", "in",
        "for", "on", "with", "at", "by", "from", "as", "into",
        "through", "during", "before", "after", "above", "below",
        "between", "under", "again", "further", "then", "once",
        "and", "or", "but", "if", "because", "until", "while",
        "this", "that", "these", "those", "it", "its",
    }
    return [w for w in words if w not in stopwords and len(w) > 2]


def _analyze_requirement_dependencies(
        subtasks: list[SubtaskInfo],
        graph: DependencyGraph,
) -> None:
    """分析任务描述中的依赖关系"""
    keywords_map: dict[str, list[str]] = {}
    for subtask in subtasks:
        keywords = _extract_keywords(subtask.requirement)
        keywords_map[subtask.id] = keywords

    dependency_keywords = {
        "depends on", "requires", "after", "based on",
        "using", "extends", "inherits", "imports from",
        "依赖", "需要", "基于", "使用",
    }

    for subtask in subtasks:
        req_lower = subtask.requirement.lower()
        for other in subtasks:
            if other.id == subtask.id:
                continue
            other_id_lower = other.id.lower()
            for kw in dependency_keywords:
                pattern = f"{kw}.*{re.escape(other_id_lower)}"
                if re.search(pattern, req_lower, re.IGNORECASE):
                    graph.add_edge(DependencyEdge(
                        from_task=other.id,
                        to_task=subtask.id,
                        reason="requirement_keyword",
                        details=f"'{subtask.requirement}' mentions dependency on {other.id}",
                    ))
                    break

    for subtask in subtasks:
        subtask_keywords = set(keywords_map[subtask.id])
        for other in subtasks:
            if other.id == subtask.id:
                continue
            other_keywords = set(keywords_map[other.id])
            overlap = subtask_keywords & other_keywords
            if len(overlap) >= 2:
                pass
