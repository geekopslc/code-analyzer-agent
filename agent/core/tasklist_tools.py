"""
任务清单工具

基于现有 todolist_tool.py 重构为声明式工具模式。
"""

import json
import logging
from typing import Any, Dict, List, Optional

from .tool_base import (
    BaseDeclarativeTool,
    BaseToolInvocation,
    ToolInvocation,
    ToolKind,
    ToolResult,
)

# 导入现有的任务清单功能
from ..todolist_tool import (
    view_tasks,
    create_tasks,
    update_tasks,
    delete_tasks,
    clear_all,
    get_summary,
)

logger = logging.getLogger("agent.core.tasklist_tools")


def _maybe_json(value: Any) -> Any:
    """尝试解析 JSON 字符串"""
    if isinstance(value, str):
        text = value.strip()
        if text and text[0] in ("{", "["):
            try:
                return json.loads(text)
            except Exception:
                return value
    return value


# ============================================================================
# TaskListView 工具
# ============================================================================

class TaskListViewParams(Dict[str, Any]):
    pass


class TaskListViewInvocation(BaseToolInvocation[TaskListViewParams]):
    
    def get_description(self) -> str:
        return "查看当前任务清单"
    
    def execute(self, repo_root: str) -> ToolResult:
        try:
            data = view_tasks(repo_root)
            return ToolResult.success(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            return ToolResult.failure(str(e), "VIEW_ERROR")


class TaskListViewTool(BaseDeclarativeTool[TaskListViewParams]):
    
    NAME = "tasklist_view"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="查看任务",
            description="查看当前任务清单，按分区展示任务与状态",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
    
    def create_invocation(self, params: TaskListViewParams) -> ToolInvocation[TaskListViewParams]:
        return TaskListViewInvocation(params, self.name, self.display_name)


# ============================================================================
# TaskListCreate 工具
# ============================================================================

class TaskListCreateParams(Dict[str, Any]):
    pass


class TaskListCreateInvocation(BaseToolInvocation[TaskListCreateParams]):
    
    def get_description(self) -> str:
        sections = self.params.get("sections", [])
        tasks = self.params.get("tasks", [])
        return f"创建任务: {len(sections)} 个分区, {len(tasks) if tasks else 0} 个任务"
    
    def execute(self, repo_root: str) -> ToolResult:
        try:
            sections = _maybe_json(self.params.get("sections"))
            if isinstance(sections, dict):
                sections = [sections]
            elif not isinstance(sections, list):
                sections = None
            
            tasks_arg = _maybe_json(self.params.get("tasks"))
            if isinstance(tasks_arg, str):
                if tasks_arg.strip() in ["创建分区", "创建任务", "创建"]:
                    tasks_arg = [{"content": "Default task placeholder", "metadata": {"auto_created": True}}]
                else:
                    tasks_arg = [{"content": tasks_arg}]
            elif isinstance(tasks_arg, dict):
                tasks_arg = [tasks_arg]
            elif not isinstance(tasks_arg, list):
                tasks_arg = None
            
            section_id = self.params.get("section_id")
            section_title = self.params.get("section_title")
            
            result = create_tasks(
                repo_root,
                sections=sections,
                tasks=tasks_arg,
                section_id=section_id,
                section_title=section_title,
            )
            return ToolResult.success(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            return ToolResult.failure(str(e), "CREATE_ERROR")


class TaskListCreateTool(BaseDeclarativeTool[TaskListCreateParams]):
    
    NAME = "tasklist_create"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="创建任务",
            description="创建任务或分区，可一次批量新增",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "description": "批量创建的分区及任务",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "tasks": {"type": "array"},
                            },
                            "required": ["title"],
                        },
                    },
                    "tasks": {
                        "type": "array",
                        "description": "向单个分区追加的任务列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "metadata": {"type": "object"},
                            },
                            "required": ["content"],
                        },
                    },
                    "section_id": {"type": "string"},
                    "section_title": {"type": "string"},
                },
                "required": [],
            },
        )
    
    def create_invocation(self, params: TaskListCreateParams) -> ToolInvocation[TaskListCreateParams]:
        return TaskListCreateInvocation(params, self.name, self.display_name)


# ============================================================================
# TaskListUpdate 工具
# ============================================================================

class TaskListUpdateParams(Dict[str, Any]):
    pass


class TaskListUpdateInvocation(BaseToolInvocation[TaskListUpdateParams]):
    
    def get_description(self) -> str:
        task_ids = self.params.get("task_ids", [])
        status = self.params.get("status", "")
        return f"更新 {len(task_ids) if isinstance(task_ids, list) else 1} 个任务{' -> ' + status if status else ''}"
    
    def execute(self, repo_root: str) -> ToolResult:
        try:
            task_ids = _maybe_json(self.params.get("task_ids"))
            if isinstance(task_ids, str):
                task_ids = [task_ids]
            
            status = self.params.get("status")
            content = self.params.get("content")
            metadata = _maybe_json(self.params.get("metadata"))
            section_id = self.params.get("section_id")
            section_title = self.params.get("section_title")
            
            result = update_tasks(
                repo_root,
                task_ids=task_ids or [],
                content=content,
                status=status,
                section_id=section_id,
                section_title=section_title,
                metadata=metadata if isinstance(metadata, dict) else None,
            )
            return ToolResult.success(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            return ToolResult.failure(str(e), "UPDATE_ERROR")


class TaskListUpdateTool(BaseDeclarativeTool[TaskListUpdateParams]):
    
    NAME = "tasklist_update"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="更新任务",
            description="更新一个或多个任务的内容、状态或所属分区",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要更新的任务ID列表",
                    },
                    "content": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "completed", "cancelled"],
                    },
                    "section_id": {"type": "string"},
                    "section_title": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["task_ids"],
            },
        )
    
    def create_invocation(self, params: TaskListUpdateParams) -> ToolInvocation[TaskListUpdateParams]:
        return TaskListUpdateInvocation(params, self.name, self.display_name)


# ============================================================================
# TaskListDelete 工具
# ============================================================================

class TaskListDeleteParams(Dict[str, Any]):
    pass


class TaskListDeleteInvocation(BaseToolInvocation[TaskListDeleteParams]):
    
    def get_description(self) -> str:
        task_ids = self.params.get("task_ids", [])
        section_ids = self.params.get("section_ids", [])
        return f"删除 {len(task_ids) if task_ids else 0} 个任务, {len(section_ids) if section_ids else 0} 个分区"
    
    def should_confirm(self) -> bool:
        """删除操作需要确认"""
        return True
    
    def execute(self, repo_root: str) -> ToolResult:
        try:
            task_ids = _maybe_json(self.params.get("task_ids"))
            if isinstance(task_ids, str):
                task_ids = [task_ids]
            elif not isinstance(task_ids, list):
                task_ids = None
            
            section_ids = _maybe_json(self.params.get("section_ids"))
            if isinstance(section_ids, str):
                section_ids = [section_ids]
            elif not isinstance(section_ids, list):
                section_ids = None
            
            confirm = bool(self.params.get("confirm", False))
            
            result = delete_tasks(
                repo_root,
                task_ids=task_ids,
                section_ids=section_ids,
                confirm=confirm,
            )
            return ToolResult.success(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            return ToolResult.failure(str(e), "DELETE_ERROR")


class TaskListDeleteTool(BaseDeclarativeTool[TaskListDeleteParams]):
    
    NAME = "tasklist_delete"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="删除任务",
            description="删除任务或分区（删除分区需确认）",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "section_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "confirm": {
                        "type": "boolean",
                        "default": False,
                    },
                },
                "required": [],
            },
        )
    
    def create_invocation(self, params: TaskListDeleteParams) -> ToolInvocation[TaskListDeleteParams]:
        return TaskListDeleteInvocation(params, self.name, self.display_name)


# ============================================================================
# TaskListClear 工具
# ============================================================================

class TaskListClearParams(Dict[str, Any]):
    pass


class TaskListClearInvocation(BaseToolInvocation[TaskListClearParams]):
    
    def get_description(self) -> str:
        return "清空全部任务和分区"
    
    def should_confirm(self) -> bool:
        """清空操作需要确认"""
        return True
    
    def execute(self, repo_root: str) -> ToolResult:
        try:
            confirm = bool(self.params.get("confirm", False))
            result = clear_all(repo_root, confirm=confirm)
            return ToolResult.success(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            return ToolResult.failure(str(e), "CLEAR_ERROR")


class TaskListClearTool(BaseDeclarativeTool[TaskListClearParams]):
    
    NAME = "tasklist_clear"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="清空任务",
            description="清空全部任务和分区（需确认）",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "必须为 true 才会执行清空操作",
                        "default": False,
                    },
                },
                "required": [],
            },
        )
    
    def create_invocation(self, params: TaskListClearParams) -> ToolInvocation[TaskListClearParams]:
        return TaskListClearInvocation(params, self.name, self.display_name)


# ============================================================================
# SyncState 工具
# ============================================================================

class SyncStateParams(Dict[str, Any]):
    pass


class SyncStateInvocation(BaseToolInvocation[SyncStateParams]):
    
    def get_description(self) -> str:
        return "同步内存文件与任务清单的最新状态"
    
    def execute(self, repo_root: str) -> ToolResult:
        import os
        from datetime import datetime
        
        try:
            mem_dir = os.path.join(repo_root, "memory")
            os.makedirs(mem_dir, exist_ok=True)
            
            # 写入 JSON 快照
            js_path = os.path.join(mem_dir, "sync_state.json")
            progress_text = self.params.get("tasklist_progress")
            if progress_text is None:
                progress_text = self.params.get("todolist_progress", "")
            
            snapshot = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "tasklist_progress": progress_text,
                "memory_summary": self.params.get("memory_summary", ""),
            }
            
            with open(js_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            
            # 附加写入 memory.md
            md_path = os.path.join(mem_dir, "memory.md")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lines = []
            lines.append("---")
            lines.append(f"时间: {ts}")
            lines.append("---")
            lines.append("")
            lines.append("## 状态同步")
            lines.append("")
            if snapshot.get("tasklist_progress"):
                lines.append("### TaskList 进度")
                lines.append(snapshot["tasklist_progress"])
                lines.append("")
            if snapshot.get("memory_summary"):
                lines.append("### Memory 摘要")
                lines.append(snapshot["memory_summary"])
                lines.append("")
            
            with open(md_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines))
            
            return ToolResult.success("已同步状态到 memory/sync_state.json 与 memory/memory.md")
        except Exception as e:
            return ToolResult.failure(str(e), "SYNC_ERROR")


class SyncStateTool(BaseDeclarativeTool[SyncStateParams]):
    
    NAME = "sync_state"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="同步状态",
            description="同步内存文件与任务清单的最新状态",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {
                    "tasklist_progress": {"type": "string"},
                    "memory_summary": {"type": "string"},
                },
                "required": [],
            },
        )
    
    def create_invocation(self, params: SyncStateParams) -> ToolInvocation[SyncStateParams]:
        return SyncStateInvocation(params, self.name, self.display_name)


# ============================================================================
# 工具工厂函数
# ============================================================================

def create_tasklist_tools() -> List[BaseDeclarativeTool]:
    """创建所有任务清单工具实例"""
    return [
        TaskListViewTool(),
        TaskListCreateTool(),
        TaskListUpdateTool(),
        TaskListDeleteTool(),
        TaskListClearTool(),
        SyncStateTool(),
    ]

