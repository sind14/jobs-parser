#!/bin/sh
# Runs on every start of the web container: bring the schema up to date and make sure
# the admin user exists, then start the server (or any command passed to the container).
set -e

python manage.py migrate --noinput
python manage.py ensure_admin

exec "$@"
