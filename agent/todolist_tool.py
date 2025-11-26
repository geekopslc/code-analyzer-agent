import json
import os
import uuid
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("agent.todolist")

# 统一存储在 memory/todolist.json
TODOLIST_FILENAME = os.path.join("memory", "todolist.json")
TODOLIST_LOG_FILENAME = os.path.join("memory", "todolist.log")


class TaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class Section:
    title: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Task:
    content: str
    section_id: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: TaskStatus = TaskStatus.PENDING
    metadata: Dict[str, Any] = field(default_factory=dict)


def _ensure_memory_dir(repo_root: str) -> None:
    mem_dir = os.path.join(repo_root, "memory")
    try:
        os.makedirs(mem_dir, exist_ok=True)
    except Exception:
        pass


def _path(repo_root: str) -> str:
    _ensure_memory_dir(repo_root)
    return os.path.join(repo_root, TODOLIST_FILENAME)


def _log_path(repo_root: str) -> str:
    _ensure_memory_dir(repo_root)
    return os.path.join(repo_root, TODOLIST_LOG_FILENAME)


def _log_change(repo_root: str, action: str, details: Optional[str] = None) -> None:
    try:
        path = _log_path(repo_root)
        with open(path, "a", encoding="utf-8") as f:
            line = action
            if details:
                line += f" {details}"
            f.write(line + "\n")
    except Exception:
        pass


def _load_state(repo_root: str) -> Tuple[List[Section], List[Task]]:
    path = _path(repo_root)
    if not os.path.exists(path):
        return [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"加载 todolist 失败: {e}")
        return [], []

    # 新结构：{"sections": [...], "tasks": [...]}
    if isinstance(raw, dict) and "sections" in raw and "tasks" in raw:
        sections = [Section(**s) for s in raw.get("sections", [])]
        tasks_list = []
        for t in raw.get("tasks", []):
            status = t.get("status", TaskStatus.PENDING)
            if isinstance(status, str):
                try:
                    status = TaskStatus(status)
                except ValueError:
                    status = TaskStatus.PENDING
            tasks_list.append(Task(
                id=t.get("id", str(uuid.uuid4())),
                content=t.get("content", ""),
                status=status if isinstance(status, TaskStatus) else TaskStatus(status),
                section_id=t.get("section_id", ""),
                metadata=t.get("metadata", {}) or {},
            ))
        return sections, tasks_list

    # 老结构：列表形式
    if isinstance(raw, list):
        default_section = Section(title="General")
        sections = [default_section]
        tasks_list = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            content = item.get("title") or item.get("description") or item.get("content") or ""
            status = item.get("status", "pending")
            mapped = TaskStatus.PENDING
            if status in ("done", "completed"):
                mapped = TaskStatus.COMPLETED
            elif status in ("cancelled", "blocked"):
                mapped = TaskStatus.CANCELLED
            tasks_list.append(Task(
                id=item.get("id", str(uuid.uuid4())),
                content=content,
                status=mapped,
                section_id=default_section.id,
                metadata=item.get("metadata", {}),
            ))
        return sections, tasks_list

    logger.warning("todolist.json 内容格式不受支持，已忽略")
    return [], []


def _save_state(repo_root: str, sections: List[Section], tasks: List[Task]) -> None:
    payload = {
        "sections": [asdict(s) for s in sections],
        "tasks": [
            {
                "id": t.id,
                "content": t.content,
                "status": t.status.value if isinstance(t.status, TaskStatus) else str(t.status),
                "section_id": t.section_id,
                "metadata": t.metadata or {},
            }
            for t in tasks
        ],
    }
    path = _path(repo_root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _format_response(sections: List[Section], tasks: List[Task]) -> Dict[str, Any]:
    section_map = {s.id: s for s in sections}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for task in tasks:
        grouped.setdefault(task.section_id, []).append({
            "id": task.id,
            "content": task.content,
            "status": task.status.value if isinstance(task.status, TaskStatus) else str(task.status),
            "metadata": task.metadata or {},
        })

    formatted_sections = []
    for section in sections:
        section_tasks = grouped.get(section.id, [])
        if not section_tasks:
            continue
        formatted_sections.append({
            "id": section.id,
            "title": section.title,
            "tasks": section_tasks,
        })

    return {
        "sections": formatted_sections,
        "total_tasks": len(tasks),
        "total_sections": len(sections),
    }


def view_tasks(repo_root: str) -> Dict[str, Any]:
    logger.info("查看任务列表")
    sections, tasks = _load_state(repo_root)
    logger.info(f"加载了 {len(sections)} 个分区和 {len(tasks)} 个任务")
    return _format_response(sections, tasks)


def create_tasks(
    repo_root: str,
    sections: Optional[List[Dict[str, Any]]] = None,
    tasks: Optional[List[Any]] = None,
    section_id: Optional[str] = None,
    section_title: Optional[str] = None,
) -> Dict[str, Any]:
    sections_data, tasks_data = _load_state(repo_root)
    section_map = {s.id: s for s in sections_data}

    def _ensure_section(title: Optional[str], ident: Optional[str]) -> Section:
        if ident and ident in section_map:
            return section_map[ident]
        if title:
            for s in sections_data:
                if s.title == title:
                    return s
        new_section = Section(title=title or "General")
        sections_data.append(new_section)
        section_map[new_section.id] = new_section
        return new_section

    created_tasks = []  # 用于记录创建的任务
    
    if sections:
        logger.info(f"创建任务分区，分区数: {len(sections)}")
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            title = sec.get("title") or "Untitled"
            section = _ensure_section(title, sec.get("id"))
            logger.info(f"处理分区: {title}")
            for item in sec.get("tasks", []):
                if isinstance(item, dict):
                    content = item.get("content") or item.get("title") or ""
                    metadata = item.get("metadata") or {}
                else:
                    content = str(item)
                    metadata = {}
                if not content:
                    continue
                new_task = Task(content=content, section_id=section.id, metadata=metadata)
                tasks_data.append(new_task)
                created_tasks.append(new_task)
                logger.info(f"创建任务: {content} (ID: {new_task.id})")
    elif tasks:
        logger.info(f"创建任务，任务数: {len(tasks)}")
        target_section = None
        if section_id:
            target_section = section_map.get(section_id)
        if not target_section:
            target_section = _ensure_section(section_title, section_id)
        for item in tasks:
            if isinstance(item, dict):
                content = item.get("content") or item.get("title") or ""
                metadata = item.get("metadata") or {}
            else:
                content = str(item)
                metadata = {}
            if not content:
                continue
            new_task = Task(content=content, section_id=target_section.id, metadata=metadata)
            tasks_data.append(new_task)
            created_tasks.append(new_task)
            logger.info(f"创建任务: {content} (ID: {new_task.id})")
    else:
        # 🔧 如果模型没传任何任务，自动创建一个空任务，防止死循环
        logger.warning("tasklist_create 收到空参数，自动创建一个默认任务防止死循环")
        target_section = _ensure_section(section_title, section_id)
        placeholder_task = Task(
            content="Default placeholder task", 
            section_id=target_section.id, 
            metadata={"auto_created": True}
        )
        tasks_data.append(placeholder_task)
        created_tasks.append(placeholder_task)
        logger.info(f"创建占位任务: Default placeholder task (ID: {placeholder_task.id})")

    _save_state(repo_root, sections_data, tasks_data)
    
    # 记录创建的任务信息
    task_details = [f"{t.content}({t.id})" for t in created_tasks]
    _log_change(repo_root, "CREATE_TASKS", f"tasks={len(tasks or [])}, created={len(created_tasks)}, details={';'.join(task_details[:5])}")
    
    logger.info(f"任务创建完成，总共创建了 {len(created_tasks)} 个任务")
    return _format_response(sections_data, tasks_data)


def update_tasks(
    repo_root: str,
    task_ids: List[str],
    content: Optional[str] = None,
    status: Optional[str] = None,
    section_id: Optional[str] = None,
    section_title: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    logger.info(f"更新任务，任务ID数: {len(task_ids)}")
    sections, tasks = _load_state(repo_root)
    section_map = {s.id: s for s in sections}
    task_map = {t.id: t for t in tasks}

    if not task_ids:
        raise ValueError("task_ids 不能为空")

    target_section = None
    if section_id:
        target_section = section_map.get(section_id)
    if not target_section and section_title:
        for s in sections:
            if s.title == section_title:
                target_section = s
                break
        if not target_section:
            target_section = Section(title=section_title)
            sections.append(target_section)
            section_map[target_section.id] = target_section

    updated_tasks = []  # 用于记录更新的任务
    for tid in task_ids:
        task = task_map.get(tid)
        if not task:
            raise KeyError(f"Task {tid} 不存在")
        old_status = task.status
        old_content = task.content
        old_section_id = task.section_id
        
        if content is not None:
            task.content = content
        if status is not None:
            task.status = TaskStatus(status)
        if metadata:
            task.metadata.update(metadata)
        if target_section:
            task.section_id = target_section.id
            
        updated_tasks.append(task)
        logger.info(f"更新任务 {tid}: 状态从 {old_status} 变更为 {task.status}, 内容从 '{old_content[:20]}...' 变更为 '{task.content[:20]}...', 分区从 {old_section_id} 变更为 {task.section_id}")

    _save_state(repo_root, sections, tasks)
    
    # 记录更新的任务信息
    update_details = []
    for task in updated_tasks:
        detail = f"{task.id}(status:{task.status.value})"
        update_details.append(detail)
    _log_change(repo_root, "UPDATE_TASKS", f"task_ids={task_ids}, updates={';'.join(update_details[:5])}")
    
    logger.info(f"任务更新完成，总共更新了 {len(updated_tasks)} 个任务")
    return _format_response(sections, tasks)


def delete_tasks(
    repo_root: str,
    task_ids: Optional[List[str]] = None,
    section_ids: Optional[List[str]] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    logger.info(f"删除任务，任务ID数: {len(task_ids or [])}, 分区ID数: {len(section_ids or [])}")
    sections, tasks = _load_state(repo_root)
    task_ids = task_ids or []
    section_ids = section_ids or []

    if section_ids and not confirm:
        raise ValueError("删除分区需要 confirm=true")

    remaining_sections = [s for s in sections if s.id not in section_ids]
    remaining_section_ids = {s.id for s in remaining_sections}
    remaining_tasks = [
        t for t in tasks
        if t.id not in (task_ids or []) and t.section_id in remaining_section_ids
    ]

    deleted_task_count = len(tasks) - len(remaining_tasks)
    deleted_section_count = len(sections) - len(remaining_sections)
    
    _save_state(repo_root, remaining_sections, remaining_tasks)
    _log_change(
        repo_root,
        "DELETE_TASKS",
        f"task_ids={task_ids}, section_ids={section_ids}, confirm={confirm}, deleted_tasks={deleted_task_count}, deleted_sections={deleted_section_count}",
    )
    
    logger.info(f"任务删除完成，删除了 {deleted_task_count} 个任务和 {deleted_section_count} 个分区")
    return _format_response(remaining_sections, remaining_tasks)


def clear_all(repo_root: str, confirm: bool) -> Dict[str, Any]:
    logger.info(f"清空所有任务，确认状态: {confirm}")
    if not confirm:
        raise ValueError("清空任务需要 confirm=true")
    sections: List[Section] = []
    tasks: List[Task] = []
    _save_state(repo_root, sections, tasks)
    _log_change(repo_root, "CLEAR_ALL")
    logger.info("所有任务已清空")
    return _format_response(sections, tasks)


# 兼容旧接口 --------------------------------------------------------------

def list_items(repo_root: str) -> List[Dict[str, Any]]:
    """兼容旧接口：返回扁平任务列表"""
    logger.info("列出所有任务项（兼容接口）")
    _, tasks = _load_state(repo_root)
    result = [
        {
            "id": t.id,
            "title": t.content,
            "status": t.status.value if isinstance(t.status, TaskStatus) else str(t.status),
            "metadata": t.metadata,
            "section_id": t.section_id,
        }
        for t in tasks
    ]
    logger.info(f"返回 {len(result)} 个任务项")
    return result


def add_item(repo_root: str, title: str, **_: Any) -> Dict[str, Any]:
    logger.info(f"添加任务项: {title}")
    response = create_tasks(repo_root, tasks=[title])
    # 返回刚创建的最后一个任务
    for section in response.get("sections", []):
        for task in section.get("tasks", []):
            if task["content"] == title:
                logger.info(f"成功添加任务项: {title} (ID: {task['id']})")
                return task
    logger.warning(f"未能找到刚创建的任务项: {title}")
    return {}


def update_item(repo_root: str, item_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"更新任务项 {item_id}")
    status = patch.get("status")
    if isinstance(status, str):
        status_lower = status.lower()
        if status_lower in ("done", "completed"):
            status = TaskStatus.COMPLETED.value
        elif status_lower in ("blocked", "cancelled"):
            status = TaskStatus.CANCELLED.value
        else:
            status = TaskStatus.PENDING.value
    content = patch.get("title") or patch.get("content")
    metadata = patch.get("metadata")
    section_id = patch.get("section_id")
    data = update_tasks(
        repo_root,
        task_ids=[item_id],
        content=content,
        status=status,
        section_id=section_id,
        metadata=metadata,
    )
    for section in data.get("sections", []):
        for task in section.get("tasks", []):
            if task["id"] == item_id:
                logger.info(f"成功更新任务项 {item_id}")
                return task
    logger.warning(f"未能找到要更新的任务项: {item_id}")
    return {}


def mark_done(repo_root: str, item_id: str, **_: Any) -> Dict[str, Any]:
    logger.info(f"标记任务为完成: {item_id}")
    result = update_item(repo_root, item_id, {"status": TaskStatus.COMPLETED.value})
    logger.info(f"任务 {item_id} 已标记为完成")
    return result


def pop_next(repo_root: str) -> Optional[Dict[str, Any]]:
    logger.info("获取下一个待处理任务")
    sections, tasks = _load_state(repo_root)
    pending = [t for t in tasks if t.status == TaskStatus.PENDING]
    if not pending:
        logger.info("没有待处理的任务")
        return None
    task = pending[0]
    update_tasks(repo_root, [task.id], status=TaskStatus.PENDING.value)
    result = {
        "id": task.id,
        "title": task.content,
        "status": task.status.value,
        "section_id": task.section_id,
    }
    logger.info(f"返回下一个待处理任务: {task.content} (ID: {task.id})")
    return result


def clear(repo_root: str) -> List[Dict[str, Any]]:
    logger.info("清空任务（兼容接口）")
    clear_all(repo_root, confirm=True)
    logger.info("任务已清空")
    return []


def get_summary(repo_root: str) -> Dict[str, Any]:
    logger.info("获取任务摘要")
    sections, tasks = _load_state(repo_root)
    status_counts = {
        TaskStatus.PENDING.value: 0,
        TaskStatus.COMPLETED.value: 0,
        TaskStatus.CANCELLED.value: 0,
    }
    for task in tasks:
        status_counts[task.status.value] = status_counts.get(task.status.value, 0) + 1
    
    pending_items = [t.id for t in tasks if t.status == TaskStatus.PENDING]
    result = {
        "total": len(tasks),
        "status_counts": status_counts,
        "pending_items": pending_items,
    }
    logger.info(f"任务摘要: 总计 {len(tasks)} 个任务, 待处理: {len(pending_items)}")
    return result