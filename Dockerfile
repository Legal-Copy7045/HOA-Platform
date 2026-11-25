# syntax=docker/dockerfile:1.7
# One image runs web, worker and the migrate step (CLAUDE.md: Docker, one image).
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Optional extra CA for builds behind a TLS-inspecting proxy:
#   docker build --secret id=extra_ca,src=/path/ca.crt .
# It is used only for this step and never stored in the image.
COPY requirements/base.txt requirements/base.txt
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --require-hashes -r requirements/base.txt

COPY --chown=app:app . .

# collectstatic needs settings to import; these values exist only for this step.
RUN DJANGO_SETTINGS_MODULE=core.settings.local \
    DJANGO_SECRET_KEY=build-only-not-a-secret \
    DATABASE_URL=sqlite:////tmp/build.sqlite3 \
    python manage.py collectstatic --noinput \
 && rm -f /tmp/build.sqlite3

USER app
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
