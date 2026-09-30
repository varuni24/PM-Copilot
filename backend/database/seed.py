from datetime import date, timedelta

from sqlalchemy import select

from database.models import Decision, Person, Project, Risk, SessionLocal, Task, initialize_database


def seed_database() -> None:
    initialize_database()
    with SessionLocal.begin() as session:
        if session.scalar(select(Project.id).where(Project.name == "PM Copilot Launch")):
            return

        project = Project(name="PM Copilot Launch", description="Launch the first version of the PM copilot.")
        sarah = Person(name="Sarah", email="sarah@example.com")
        alex = Person(name="Alex", email="alex@example.com")
        maya = Person(name="Maya", email="maya@example.com")
        session.add_all([project, sarah, alex, maya])
        session.flush()

        session.add_all([
            Task(title="Finish API documentation", description="Document the chat API.", status="in_progress", owner=sarah, project=project, due_date=date.today() - timedelta(days=2)),
            Task(title="Review launch checklist", description="Confirm launch readiness.", status="todo", owner=alex, project=project, due_date=date.today() - timedelta(days=1)),
            Task(title="Polish chat interface", description="Final UI review.", status="done", owner=maya, project=project, due_date=date.today() - timedelta(days=3)),
            Task(title="Prepare demo", description="Show the copilot to stakeholders.", status="todo", owner=sarah, project=project, due_date=date.today() + timedelta(days=3)),
            Decision(title="Use Ollama locally", description="Keep model calls local to avoid external API dependencies.", decided_on=date.today(), project=project),
            Risk(title="Demo schedule may slip", description="Documentation needs to be finished before the demo.", status="open", project=project),
        ])


def task_data(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "owner": task.owner.name if task.owner else None,
        "owner_id": task.owner_id,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "project_id": task.project_id,
    }
