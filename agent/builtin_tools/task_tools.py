# -*- coding: utf-8 -*-
"""
任务管理工具

包含任务清单相关的工具。
"""

import json
from datetime import datetime
import os
from typing import Any, Dict, List, Optional

from ..core.tool_base import (
    BaseDeclarativeTool,
    BaseToolInvocation,
    ToolInvocation,
    ToolKind,
    ToolResult,
    ToolErrorType,
)
from ..todolist_tool import (
    view_tasks,
    create_tasks,
    update_tasks,
    delete_tasks,
    clear_all,
    get_summary,
)


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


# ============== TasklistView Tool ==============

class TasklistViewInvocation(BaseToolInvocation[Dict[str, Any]]):
    """查看任务清单调用实例"""
    
    def get_description(self) -> str:
        return "查看当前任务清单"
    
    def execute(self) -> ToolResult:
        try:
            data = view_tasks(self.repo_root)
            content = json.dumps(data, ensure_ascii=False, indent=2)
            return ToolResult.success(content)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class TasklistViewTool(BaseDeclarativeTool[Dict[str, Any]]):
    """查看任务清单工具"""
    
    def __init__(self):
        super().__init__(
            name="tasklist_view",
            display_name="TasklistView",
            description="查看当前任务清单，按分区展示任务与状态",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return TasklistViewInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== TasklistCreate Tool ==============

class TasklistCreateInvocation(BaseToolInvocation[Dict[str, Any]]):
    """创建任务调用实例"""
    
    def get_description(self) -> str:
        sections = self.params.get("sections", [])
        tasks = self.params.get("tasks", [])
        return f"创建任务 (分区: {len(sections)}, 任务: {len(tasks) if tasks else 0})"
    
    def execute(self) -> ToolResult:
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
                self.repo_root,
                sections=sections,
                tasks=tasks_arg,
                section_id=section_id,
                section_title=section_title,
            )
            content = json.dumps(result, ensure_ascii=False, indent=2)
            return ToolResult.success(content)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class TasklistCreateTool(BaseDeclarativeTool[Dict[str, Any]]):
    """创建任务工具"""
    
    def __init__(self):
        super().__init__(
            name="tasklist_create",
            display_name="TasklistCreate",
            description="创建任务或分区，可一次批量新增",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "description": "批量创建的分区及任务",
                    },
                    "tasks": {
                        "type": "array",
                        "description": "向单个分区追加的任务列表",
                    },
                    "section_id": {"type": "string"},
                    "section_title": {"type": "string"},
                },
                "required": [],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return TasklistCreateInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== TasklistUpdate Tool ==============

class TasklistUpdateInvocation(BaseToolInvocation[Dict[str, Any]]):
    """更新任务调用实例"""
    
    def get_description(self) -> str:
        task_ids = self.params.get("task_ids", [])
        status = self.params.get("status", "")
        return f"更新任务 ({len(task_ids)} 个, 状态: {status})"
    
    def execute(self) -> ToolResult:
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
                self.repo_root,
                task_ids=task_ids or [],
                content=content,
                status=status,
                section_id=section_id,
                section_title=section_title,
                metadata=metadata if isinstance(metadata, dict) else None,
            )
            content = json.dumps(result, ensure_ascii=False, indent=2)
            return ToolResult.success(content)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class TasklistUpdateTool(BaseDeclarativeTool[Dict[str, Any]]):
    """更新任务工具"""
    
    def __init__(self):
        super().__init__(
            name="tasklist_update",
            display_name="TasklistUpdate",
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
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return TasklistUpdateInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== TasklistDelete Tool ==============

class TasklistDeleteInvocation(BaseToolInvocation[Dict[str, Any]]):
    """删除任务调用实例"""
    
    def get_description(self) -> str:
        task_ids = self.params.get("task_ids", [])
        section_ids = self.params.get("section_ids", [])
        return f"删除任务 ({len(task_ids) if task_ids else 0} 个任务, {len(section_ids) if section_ids else 0} 个分区)"
    
    def execute(self) -> ToolResult:
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
                self.repo_root,
                task_ids=task_ids,
                section_ids=section_ids,
                confirm=confirm,
            )
            content = json.dumps(result, ensure_ascii=False, indent=2)
            return ToolResult.success(content)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class TasklistDeleteTool(BaseDeclarativeTool[Dict[str, Any]]):
    """删除任务工具"""
    
    def __init__(self):
        super().__init__(
            name="tasklist_delete",
            display_name="TasklistDelete",
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
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return TasklistDeleteInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== TasklistClear Tool ==============

class TasklistClearInvocation(BaseToolInvocation[Dict[str, Any]]):
    """清空任务清单调用实例"""
    
    def get_description(self) -> str:
        return "清空全部任务和分区"
    
    def execute(self) -> ToolResult:
        try:
            confirm = bool(self.params.get("confirm", False))
            result = clear_all(self.repo_root, confirm=confirm)
            content = json.dumps(result, ensure_ascii=False, indent=2)
            return ToolResult.success(content)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class TasklistClearTool(BaseDeclarativeTool[Dict[str, Any]]):
    """清空任务清单工具"""
    
    def __init__(self):
        super().__init__(
            name="tasklist_clear",
            display_name="TasklistClear",
            description="清空全部任务和分区（需确认）",
            kind=ToolKind.TASK,
            parameter_schema={
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "default": False,
                    },
                },
                "required": [],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return TasklistClearInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== SyncState Tool ==============

class SyncStateInvocation(BaseToolInvocation[Dict[str, Any]]):
    """同步状态调用实例"""
    
    def get_description(self) -> str:
        return "同步内存文件与任务清单状态"
    
    def execute(self) -> ToolResult:
        try:
            mem_dir = os.path.join(self.repo_root, "memory")
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
            return ToolResult.failure(f"同步状态失败 - {str(e)}", ToolErrorType.EXECUTION_FAILED)


class SyncStateTool(BaseDeclarativeTool[Dict[str, Any]]):
    """同步状态工具"""
    
    def __init__(self):
        super().__init__(
            name="sync_state",
            display_name="SyncState",
            description="同步内存文件与任务清单的最新状态",
            kind=ToolKind.MEMORY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "tasklist_progress": {"type": "string"},
                    "memory_summary": {"type": "string"},
                },
                "required": [],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return SyncStateInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )

