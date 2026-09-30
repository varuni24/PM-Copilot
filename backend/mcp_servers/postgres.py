from datetime import date

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from database.models import Decision, Person, Project, Risk, SessionLocal, Task
from database.seed import task_data

mcp = MCPServer("PM Copilot Database", instructions="Use these tools to read and create project-management records.")


class TaskRecord(BaseModel):
    id: int
    title: str
    description: str | None
    status: str
    owner: str | None
    owner_id: int | None
    due_date: str | None
    project_id: int


class TasksResult(BaseModel):
    tasks: list[TaskRecord]


class OverdueTasksResult(TasksResult):
    as_of_date: str


class DecisionRecord(BaseModel):
    id: int
    title: str
    description: str | None
    decided_on: str | None


class RiskRecord(BaseModel):
    id: int
    title: str
    description: str | None
    status: str


class ProjectRecord(BaseModel):
    id: int
    name: str
    description: str | None
    task_ids: list[int]
    decisions: list[DecisionRecord]
    risks: list[RiskRecord]


class ProjectSummary(BaseModel):
    id: int
    name: str
    description: str | None


class ProjectsResult(BaseModel):
    projects: list[ProjectSummary]


class PersonRecord(BaseModel):
    id: int
    name: str
    email: str | None


class PeopleResult(BaseModel):
    people: list[PersonRecord]


class CreatedTaskResult(BaseModel):
    task: TaskRecord


@mcp.tool()
def get_projects() -> ProjectsResult:
    """List available projects with numeric IDs, names, and descriptions. Call this before create_task to resolve a project name or select the only available project; never guess a project ID."""
    with SessionLocal() as session:
        projects = session.scalars(select(Project).order_by(Project.name)).all()
        return ProjectsResult(projects=[ProjectSummary(id=project.id, name=project.name, description=project.description) for project in projects])


@mcp.tool()
def get_tasks(owner_name: str | None = None, project_id: int | None = None, status: str | None = None) -> TasksResult:
    """List tasks. Filter by a person's name (for example Sarah), project ID, or status when requested. Returns task IDs, titles, owners, due dates, and project IDs."""
    with SessionLocal() as session:
        query = select(Task).options(joinedload(Task.owner))
        if owner_name:
            query = query.join(Task.owner).where(func.lower(Person.name) == owner_name.strip().lower())
        if project_id is not None:
            query = query.where(Task.project_id == project_id)
        if status:
            query = query.where(Task.status == status)
        tasks = session.scalars(query.order_by(Task.id)).all()
        return TasksResult(tasks=[TaskRecord.model_validate(task_data(task)) for task in tasks])


@mcp.tool()
def get_overdue_tasks(project_id: int | None = None) -> OverdueTasksResult:
    """List tasks due before today's date that are not done. Optionally filter by project ID. Returns task IDs, titles, owners, due dates, and project IDs."""
    with SessionLocal() as session:
        query = select(Task).options(joinedload(Task.owner)).where(
            Task.due_date < date.today(), Task.status != "done"
        )
        if project_id is not None:
            query = query.where(Task.project_id == project_id)
        tasks = session.scalars(query.order_by(Task.due_date, Task.id)).all()
        return OverdueTasksResult(as_of_date=date.today().isoformat(), tasks=[TaskRecord.model_validate(task_data(task)) for task in tasks])


@mcp.tool()
def get_project(project_id: int) -> ProjectRecord:
    """Get one project by numeric project ID, including its description, task IDs, decisions, and risks. Use this when asked about a specific project."""
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if not project:
            raise ToolError(f"Project {project_id} was not found.")
        return ProjectRecord(
            id=project.id,
            name=project.name,
            description=project.description,
            task_ids=[task.id for task in project.tasks],
            decisions=[DecisionRecord(id=decision.id, title=decision.title, description=decision.description, decided_on=decision.decided_on.isoformat() if decision.decided_on else None) for decision in project.decisions],
            risks=[RiskRecord(id=risk.id, title=risk.title, description=risk.description, status=risk.status) for risk in project.risks],
        )


@mcp.tool()
def get_people() -> PeopleResult:
    """List people who can own tasks. Returns each person's ID, name, and email. Use this to identify an owner before creating a task."""
    with SessionLocal() as session:
        people = session.scalars(select(Person).order_by(Person.name)).all()
        return PeopleResult(people=[PersonRecord(id=person.id, name=person.name, email=person.email) for person in people])


@mcp.tool()
def create_task(title: str, project_id: int, owner_name: str | None = None, description: str | None = None, due_date: str | None = None) -> CreatedTaskResult:
    """Create a task in a known project. Requires a title and numeric project ID. Optional owner_name must match a person returned by get_people. Optional due_date must be an ISO date (YYYY-MM-DD); resolve relative dates before calling. Returns the created task."""
    title = title.strip()
    if not title:
        raise ToolError("Task title cannot be empty.")
    try:
        parsed_due_date = date.fromisoformat(due_date) if due_date else None
    except ValueError:
        raise ToolError("due_date must be an ISO date in YYYY-MM-DD format.")

    with SessionLocal.begin() as session:
        project = session.get(Project, project_id)
        if not project:
            raise ToolError(f"Project {project_id} was not found. No task was created.")
        owner = None
        if owner_name:
            owner = session.scalar(select(Person).where(func.lower(Person.name) == owner_name.strip().lower()))
            if not owner:
                raise ToolError(f"Person '{owner_name}' was not found. No task was created.")
        task = Task(title=title, description=description, status="todo", owner=owner, due_date=parsed_due_date, project=project)
        session.add(task)
        session.flush()
        return CreatedTaskResult(task=TaskRecord.model_validate(task_data(task)))


if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=8001)
