release: python scripts/ops/release.py
web: gunicorn tandem.wsgi --workers ${WEB_CONCURRENCY:-3} --timeout 30 --log-file -
worker: celery -A tandem worker --beat --loglevel=info --concurrency=${AGENT_WORKER_CONCURRENCY:-4} --prefetch-multiplier=1
photon: npm run photon
