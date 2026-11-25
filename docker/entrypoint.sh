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
    exec gunicorn core.wsgi:application \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-3}" \
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
    python manage.py createcachetable
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
