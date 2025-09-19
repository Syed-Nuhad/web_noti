# webnotify_project/celery.py
from __future__ import annotations
import os
from celery import Celery

# Set default Django settings module for 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webnotify_project.settings")

app = Celery("webnotify_project")

# Read config from Django settings, using CELERY_ prefix (namespace='CELERY')
app.config_from_object("django.conf:settings", namespace="CELERY")

# Autodiscover tasks from installed apps (looks for tasks.py)
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    print(f"Celery debug task called: {self.request!r}")
    return "debug"
