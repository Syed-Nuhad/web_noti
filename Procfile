web: gunicorn webnotify_project.wsgi
worker: celery -A webnotify_project worker --loglevel=info
beat: celery -A webnotify_project beat --loglevel=info