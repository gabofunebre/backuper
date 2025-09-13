from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from orchestrator.app.database import SessionLocal
from orchestrator.app.models import App
from orchestrator.services.client import BackupClient

scheduler = BackgroundScheduler()


def schedule_app_backups() -> None:
    """Load apps from the database and register their backup jobs."""
    with SessionLocal() as db:
        apps = db.query(App).all()
        for app in apps:
            if not app.schedule:
                continue
            job_id = f"backup_{app.id}"
            trigger = CronTrigger.from_crontab(app.schedule)
            scheduler.add_job(
                run_backup,
                trigger,
                args=[app.id],
                id=job_id,
                replace_existing=True,
            )


def run_backup(app_id: int) -> None:
    """Execute backup for the given app id."""
    with SessionLocal() as db:
        app = db.query(App).get(app_id)
        if not app:
            return
    client = BackupClient(app.url, app.token)
    if client.check_capabilities():
        client.export_backup(app.name)


def start() -> None:
    """Start the background scheduler if not already running."""
    if not scheduler.running:
        schedule_app_backups()
        scheduler.start()
