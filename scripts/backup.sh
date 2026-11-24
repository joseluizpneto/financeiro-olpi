#!/usr/bin/env bash
# Gera backup compactado (e opcionalmente criptografado com age) em ./backups
# Uso: ./scripts/backup.sh  (cron: 0 3 * * * cd /caminho/financas && ./scripts/backup.sh >> backups/backup.log 2>&1)
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."

_env() {
  local chave="$1" padrao="${2:-}" linha=""
  if [ -f .env ]; then
    linha=$(grep -E "^${chave}=" .env | tail -n 1 || true)
    if [ -n "$linha" ]; then
      linha="${linha#*=}"
      linha="${linha%$'\r'}"
      linha="${linha#\"}"; linha="${linha%\"}"
      linha="${linha#\'}"; linha="${linha%\'}"
      printf '%s' "$linha"
      return
    fi
  fi
  printf '%s' "$padrao"
}

POSTGRES_USER=$(_env POSTGRES_USER financas)
POSTGRES_DB=$(_env POSTGRES_DB financas)
AGE_RECIPIENT=$(_env BACKUP_AGE_RECIPIENT "")

mkdir -p backups
chmod 700 backups

CARIMBO=$(date +%Y-%m-%d_%H%M)
ARQUIVO="backups/financas_${CARIMBO}.sql.gz"
TMP="${ARQUIVO}.tmp"
trap 'rm -f "$TMP"' EXIT

docker compose exec -T db pg_dump \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  --clean --if-exists --format=plain | gzip -c > "$TMP"

gzip -t "$TMP"
[ -s "$TMP" ] || { echo "Erro: backup gerado está vazio."; exit 1; }
mv "$TMP" "$ARQUIVO"

if [ -n "$AGE_RECIPIENT" ]; then
  if ! command -v age >/dev/null 2>&1; then
    echo "Erro: BACKUP_AGE_RECIPIENT definido, mas o utilitário 'age' não está instalado." >&2
    exit 1
  fi
  age -r "$AGE_RECIPIENT" -o "${ARQUIVO}.age" "$ARQUIVO"
  rm -f "$ARQUIVO"
  ARQUIVO="${ARQUIVO}.age"
fi

echo "Backup salvo e verificado em $ARQUIVO"

# Mantém os 30 backups válidos mais recentes
ls -1t backups/financas_*.sql.gz* 2>/dev/null | tail -n +31 | xargs -r rm --
