#!/bin/sh
set -eu

python manage.py migrate --noinput
python manage.py cleanup_expired_data --restore-check
python manage.py collectstatic --noinput
exec "$@"
