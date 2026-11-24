#!/bin/sh
set -e

echo ">> Aplicando migrações do banco..."
python manage.py migrate --noinput

echo ">> Conferindo cadastros iniciais..."
python manage.py seed_inicial

if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  if ! python manage.py shell -c "from django.contrib.auth import get_user_model as g; import os, sys; sys.exit(0 if g().objects.filter(username=os.environ['DJANGO_SUPERUSER_USERNAME']).exists() else 1)"; then
    echo ">> Criando usuário administrador $DJANGO_SUPERUSER_USERNAME..."
    python manage.py createsuperuser --noinput --email "${DJANGO_SUPERUSER_EMAIL:-admin@localhost}"
  fi
fi

echo ">> Gerando lançamentos recorrentes..."
python manage.py gerar_recorrencias || true

exec "$@"
