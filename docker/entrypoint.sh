#!/bin/sh
# Container entrypoint. One image, several roles:
#   web      gunicorn (no access log: URLs can carry single-use tokens)
#   worker   Procrastinate background worker
#   migrate  apply migrations, then runtime grants (run with the OWNER role URL)
#   manage   any other management command, e.g. `manage seed_demo`
set -eu

case "${1:-web}" in
  web)
    python manage.py check_db_role
    # 2 processes x 4 threads: 8 requests at once in less memory than 3
    # single-threaded workers; slow downloads or the breached-password check
    # no longer tie up a whole process. --preload loads the code once.
    exec gunicorn core.wsgi:application \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-2}" \
      --threads "${WEB_THREADS:-4}" \
      --worker-class gthread \
      --preload \
      --max-requests 1000 \
      --max-requests-jitter 100 \
      --graceful-timeout 10 \
      --timeout "${WEB_TIMEOUT:-30}" \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
      --error-logfile -
    ;;
  worker)
    python manage.py check_db_role
    exec python manage.py procrastinate worker
    ;;
  migrate)
    # Cache table first, so the post-migrate runtime grants cover it too.
    # Skipped quickly when there is nothing to do (most updates).
    python manage.py createcachetable
    if python manage.py migrate --check >/dev/null 2>&1; then
      echo "No migrations to apply."
      exec python manage.py apply_db_grants
    fi
    exec python manage.py migrate --noinput
    ;;
  manage)
    shift
    exec python manage.py "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
