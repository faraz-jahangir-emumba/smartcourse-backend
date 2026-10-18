# The API as a container, so `docker compose up` is the whole setup.
#
# Before this, running the project meant installing uv, syncing, migrating and
# starting uvicorn by hand - four steps a reviewer has to get right before
# seeing anything. The Week 1 deliverable asks for "local setup ready".

FROM python:3.12-slim

# uv, copied from its own published image rather than installed with a script.
# Pinned: `latest` would change the build between today and next month.
COPY --from=ghcr.io/astral-sh/uv:0.9.6 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, in their own layer.
#
# Docker caches each step and reuses it while its inputs are unchanged. Copying
# only the lockfile here means editing application code does not reinstall
# every dependency - the install layer is still valid, so a rebuild takes
# seconds instead of a minute.
COPY pyproject.toml uv.lock ./

# --no-install-project because the source is not here yet; this installs only
# what the lockfile names. --frozen fails if the lockfile and pyproject have
# drifted, so a dependency added without re-locking is caught at build time
# rather than working on one machine and not another.
RUN uv sync --frozen --no-install-project

# Now the code, which changes on nearly every build.
COPY . .

# Install the project itself, now that it exists.
RUN uv sync --frozen

# Dev dependencies are included deliberately: this image runs the test suite in
# CI and locally as well as serving the API. A slimmer production image would
# add --no-dev, and is a Module 3 concern along with the rest of deployment.

EXPOSE 8000

# --host 0.0.0.0, not the default 127.0.0.1. Inside a container, localhost
# means the container itself, so a server bound to it is unreachable from your
# machine no matter what ports are published.
CMD ["uv", "run", "uvicorn", "smartcourse.main:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
