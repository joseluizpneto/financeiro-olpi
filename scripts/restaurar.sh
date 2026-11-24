#!/usr/bin/env bash
# Restaura um backup gerado por backup.sh (SUBSTITUI os dados atuais)
# Uso: ./scripts/restaurar.sh backups/financas_2026-10-29_0300.sql.gz
set -euo pipefail

[ "$#" -ge 1 ] && [ -f "$1" ] || {
  echo "Informe o arquivo de backup. Ex.: $0 backups/financas_AAAA-MM-DD_HHMM.sql.gz"
  exit 1
}
ARQUIVO_ORIG=$(realpath "$1")

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

TMP_GZ=""
WEB_PARADO=0
limpar() {
  [ -n "$TMP_GZ" ] && rm -f "$TMP_GZ"
  if [ "$WEB_PARADO" -eq 1 ]; then
    docker compose start web >/dev/null 2>&1 || true
  fi
}
trap limpar EXIT

ARQUIVO_GZ="$ARQUIVO_ORIG"
if [[ "$ARQUIVO_ORIG" == *.age ]]; then
  command -v age >/dev/null 2>&1 || { echo "Instale 'age' para descriptografar o backup." >&2; exit 1; }
  CHAVE_PRIV=$(_env BACKUP_AGE_IDENTITY "${HOME}/.config/age/key.txt")
  [ -f "$CHAVE_PRIV" ] || { echo "Chave privada age não encontrada em $CHAVE_PRIV." >&2; exit 1; }
  TMP_GZ=$(mktemp /tmp/restaurar_financas_XXXXXX.sql.gz)
  age -d -i "$CHAVE_PRIV" -o "$TMP_GZ" "$ARQUIVO_ORIG"
  ARQUIVO_GZ="$TMP_GZ"
fi

gzip -t "$ARQUIVO_GZ"

printf "Isso vai substituir TODOS os dados atuais por %s. Digite SIM para continuar: " "$ARQUIVO_ORIG"
read -r resposta
[ "$resposta" = "SIM" ] || { echo "Cancelado."; exit 1; }

docker compose stop web
WEB_PARADO=1

gunzip -c "$ARQUIVO_GZ" | docker compose exec -T db psql \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 \
  --single-transaction \
  -q

docker compose start web
WEB_PARADO=0
echo "Restauração concluída com sucesso."
