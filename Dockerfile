# syntax=docker/dockerfile:1.7
# One image runs web, worker and the migrate step (CLAUDE.md: Docker, one image).
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The base image is rebuilt only every few weeks; take the Debian security
# updates published since, so the image scan (CI image-scan) finds nothing
# fixable. No new packages are installed.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Optional extra CA for builds behind a TLS-inspecting proxy:
#   docker build --secret id=extra_ca,src=/path/ca.crt .
# It is used only for this step and never stored in the image.
COPY requirements/base.txt requirements/base.txt
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --require-hashes -r requirements/base.txt \
 && python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3*/ensurepip/_bundled
# pip is needed only to build the image. It is removed with its vendored
# libraries (urllib3, msgpack, setuptools' pkg_resources, listed in its SBOM),
# which the image scan flags and the portal never runs; nothing in the
# container installs packages (the filesystem is read-only anyway).

# Copied as root: the runtime user can read the code but never change it.
COPY . .

# collectstatic needs settings to import; these values exist only for this step.
RUN DJANGO_SETTINGS_MODULE=core.settings.local \
    DJANGO_SECRET_KEY=build-only-not-a-secret \
    DATABASE_URL=sqlite:////tmp/build.sqlite3 \
    ADMIN_ENABLED=false \
    python manage.py collectstatic --noinput \
 && rm -f /tmp/build.sqlite3 \
 && python -m compileall -q -j 0 /app
# Bytecode is compiled above, once: the container's filesystem is read-only,
# so otherwise every process start would recompile the whole app.

# Safe default: anything run from this image uses production settings unless
# told otherwise (compose sets local settings explicitly for development).
ENV DJANGO_SETTINGS_MODULE=core.settings.production

USER app
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
