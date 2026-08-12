FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/autoapply

RUN groupadd --system --gid 10001 autoapply \
    && useradd --system --uid 10001 --gid autoapply --home-dir /nonexistent autoapply

COPY worker/requirements.txt /tmp/worker-requirements.txt
RUN python -m pip install --requirement /tmp/worker-requirements.txt \
    && rm /tmp/worker-requirements.txt

# Copy only modules used by the persistent queue process.  In particular, the image
# excludes the legacy desktop app, local data, browser profiles, tests, and web UI.
COPY --chown=autoapply:autoapply app/__init__.py ./app/
COPY --chown=autoapply:autoapply \
    app/saas/__init__.py \
    app/saas/auth.py \
    app/saas/browser.py \
    app/saas/config.py \
    app/saas/crypto.py \
    app/saas/errors.py \
    app/saas/store.py \
    app/saas/providers.py \
    ./app/saas/
COPY --chown=autoapply:autoapply \
    app/saas/discovery/__init__.py \
    app/saas/discovery/common.py \
    app/saas/discovery/importers.py \
    app/saas/discovery/linkedin.py \
    app/saas/discovery/network.py \
    app/saas/discovery/public_ats.py \
    app/saas/discovery/providers.py \
    app/saas/discovery/referrals.py \
    app/saas/discovery/rss.py \
    app/saas/discovery/telegram.py \
    ./app/saas/discovery/
COPY --chown=autoapply:autoapply \
    worker/__init__.py \
    worker/main.py \
    worker/handlers.py \
    worker/browser_runtime.py \
    worker/discovery_runtime.py \
    ./worker/
COPY --chown=autoapply:autoapply \
    worker/providers/__init__.py \
    worker/providers/ashby.py \
    worker/providers/base.py \
    worker/providers/cutshort.py \
    worker/providers/google_forms.py \
    worker/providers/greenhouse.py \
    worker/providers/instahyre.py \
    worker/providers/lever.py \
    worker/providers/wellfound.py \
    worker/providers/yc.py \
    ./worker/providers/

USER autoapply

STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "worker.main"]
