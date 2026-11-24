#!/usr/bin/env bash
# Agente de atualização segura via Git para Docker Compose (executado no host)
# Uso:
#   ./scripts/atualizar.sh --sync    # Atualiza host_info.json com o commit/branch atuais
#   ./scripts/atualizar.sh --daemon  # Para cron a cada minuto: sincroniza e processa pedido.json se existir
#   ./scripts/atualizar.sh --agora   # Executa git pull --ff-only + docker compose up -d --build imediatamente
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"
UPDATE_DIR="${GIT_UPDATE_DIR:-${ROOT_DIR}/data/atualizacao}"
HOST_INFO_FILE="${UPDATE_DIR}/host_info.json"
PEDIDO_FILE="${UPDATE_DIR}/pedido.json"
STATUS_FILE="${UPDATE_DIR}/status.json"
LOCK_FILE="${UPDATE_DIR}/worker.lock"

mkdir -p "$UPDATE_DIR"
chmod 775 "$UPDATE_DIR" 2>/dev/null || true

_ajustar_permissoes() {
  chmod 775 "$UPDATE_DIR" 2>/dev/null || true
  for f in "$HOST_INFO_FILE" "$STATUS_FILE"; do
    if [ -f "$f" ]; then
      chmod 664 "$f" 2>/dev/null || true
    fi
  done
  if [ "$(id -u)" -eq 0 ]; then
    chown -R 1000:1000 "$UPDATE_DIR" 2>/dev/null || true
  fi
}

_sanitizar() {
  python3 -c '
import re, sys
txt = sys.stdin.read()
txt = re.sub(r"ghp_[A-Za-z0-9_]+", "***", txt)
txt = re.sub(r"github_pat_[A-Za-z0-9_]+", "***", txt)
txt = re.sub(r"(https?://)[^@\s/]+@", r"\1***@", txt)
sys.stdout.write(txt)
'
}

_escrever_host_info() {
  local commit branch data_commit autor mensagem remote_url sujo
  commit="$(git rev-parse HEAD 2>/dev/null || true)"
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [ "$branch" = "HEAD" ] && branch=""
  data_commit="$(git log -1 --format=%cI 2>/dev/null || true)"
  autor="$(git log -1 --format=%an 2>/dev/null || true)"
  mensagem="$(git log -1 --format=%s 2>/dev/null || true)"
  remote_url="$(git remote get-url origin 2>/dev/null || true)"
  if [ -n "$(git status --porcelain 2>/dev/null || true)" ]; then
    sujo="1"
  else
    sujo="0"
  fi

  python3 - "$HOST_INFO_FILE" "$commit" "$branch" "$data_commit" "$autor" "$mensagem" "$remote_url" "$sujo" <<'PY'
import datetime, json, os, re, sys, urllib.parse

dest, commit, branch, data_commit, autor, mensagem, remote_url, sujo = sys.argv[1:9]

def norm_repo(url: str) -> str:
    s = (url or "").strip()
    if s.startswith("git@github.com:"):
        s = s[len("git@github.com:"):]
    elif s.startswith("ssh://git@github.com/"):
        s = s[len("ssh://git@github.com/"):]
    elif s.startswith(("https://", "http://")):
        p = urllib.parse.urlparse(s)
        s = p.path.lstrip("/") if (p.hostname or "").lower() == "github.com" else ""
    if s.endswith(".git"):
        s = s[:-4]
    s = s.strip("/")
    return s if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", s) else ""

dados = {
    "commit": commit,
    "commit_curto": commit[:7],
    "branch": branch or "main",
    "data_commit": data_commit,
    "autor": autor,
    "mensagem": mensagem,
    "repositorio": norm_repo(remote_url),
    "alteracoes_locais": sujo == "1",
    "heartbeat_em": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
}
tmp = dest + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(dados, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
os.chmod(tmp, 0o664)
os.replace(tmp, dest)
PY
  _ajustar_permissoes
}

_escrever_status() {
  local estado="$1" etapa="$2" pedido_id="$3" branch="$4" solicitante="$5" commit_ant="$6" commit_novo="$7" erro="$8" log_file="$9" iniciado_em="${10}"
  python3 - "$STATUS_FILE" "$estado" "$etapa" "$pedido_id" "$branch" "$solicitante" "$commit_ant" "$commit_novo" "$erro" "$log_file" "$iniciado_em" <<'PY'
import datetime, json, os, re, sys

dest, estado, etapa, pedido_id, branch, solicitante, commit_ant, commit_novo, erro, log_file, iniciado_em = sys.argv[1:12]

def limpar(txt: str) -> str:
    txt = re.sub(r"ghp_[A-Za-z0-9_]+", "***", txt)
    txt = re.sub(r"github_pat_[A-Za-z0-9_]+", "***", txt)
    txt = re.sub(r"(https?://)[^@\s/]+@", r"\1***@", txt)
    return txt

log_txt = ""
if log_file and os.path.isfile(log_file):
    with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
        linhas = fh.read().splitlines()[-80:]
    log_txt = limpar("\n".join(linhas))

agora = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
dados = {
    "id": pedido_id,
    "estado": estado,
    "etapa": limpar(etapa),
    "branch": branch,
    "solicitado_por": solicitante,
    "commit_anterior": commit_ant,
    "commit_novo": commit_novo,
    "iniciado_em": iniciado_em or agora,
    "atualizado_em": agora,
    "finalizado_em": agora if estado in ("concluido", "erro") else "",
    "erro": limpar(erro),
    "log": log_txt,
}
tmp = dest + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(dados, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
os.chmod(tmp, 0o664)
os.replace(tmp, dest)
PY
  _ajustar_permissoes
}

_validar_branch() {
  local br="$1"
  if [ -z "$br" ] || [[ "$br" == *".."* ]] || [[ "$br" == */ ]] || [[ "$br" == *.lock ]]; then
    return 1
  fi
  [[ "$br" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$ ]]
}

_executar_atualizacao() {
  local pedido_id="$1" branch_req="$2" solicitante="$3"
  local iniciado_em commit_ant commit_novo branch_local log_tmp
  iniciado_em="$(date -Is)"
  commit_ant="$(git rev-parse HEAD 2>/dev/null || true)"
  commit_novo="$commit_ant"
  branch_local="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  log_tmp="$(mktemp /tmp/financas_update_XXXXXX.log)"

  _cleanup_update() {
    rm -f "$log_tmp"
  }
  trap _cleanup_update RETURN

  _falhar() {
    local msg="$1"
    _escrever_status "erro" "Falha na atualização" "$pedido_id" "$branch_req" "$solicitante" "$commit_ant" "$commit_novo" "$msg" "$log_tmp" "$iniciado_em"
    _escrever_host_info || true
    echo "ERRO: $msg" >&2
    return 1
  }

  if ! _validar_branch "$branch_req"; then
    _falhar "Nome de branch inválido no pedido."
    return 1
  fi

  if [ -n "$branch_local" ] && [ "$branch_local" != "$branch_req" ]; then
    _falhar "Branch solicitada ($branch_req) difere da branch ativa no servidor ($branch_local)."
    return 1
  fi

  if [ -n "$(git status --porcelain)" ]; then
    _falhar "Repositório no servidor possui alterações locais não commitadas."
    return 1
  fi

  _escrever_status "executando" "Baixando alterações do Git (git fetch / pull --ff-only)…" "$pedido_id" "$branch_req" "$solicitante" "$commit_ant" "$commit_novo" "" "$log_tmp" "$iniciado_em"

  if ! { git fetch origin -- "$branch_req" && git pull --ff-only origin -- "$branch_req"; } >>"$log_tmp" 2>&1; then
    _falhar "Não foi possível aplicar git pull --ff-only na branch $branch_req. Veja o log."
    return 1
  fi

  commit_novo="$(git rev-parse HEAD 2>/dev/null || true)"
  export APP_GIT_COMMIT="$commit_novo"
  export APP_GIT_BRANCH="$branch_req"
  export APP_GIT_DATE="$(git log -1 --format=%cI 2>/dev/null || true)"

  _escrever_status "executando" "Reconstruindo e reiniciando container (docker compose up -d --build)…" "$pedido_id" "$branch_req" "$solicitante" "$commit_ant" "$commit_novo" "" "$log_tmp" "$iniciado_em"

  if ! docker compose up -d --build >>"$log_tmp" 2>&1; then
    _falhar "Falha ao executar docker compose up -d --build. Veja o log."
    return 1
  fi

  _escrever_status "executando" "Aguardando serviço web ficar saudável…" "$pedido_id" "$branch_req" "$solicitante" "$commit_ant" "$commit_novo" "" "$log_tmp" "$iniciado_em"

  local cid="" tentativas=0 status_hc=""
  cid="$(docker compose ps -q web 2>/dev/null || true)"
  if [ -n "$cid" ]; then
    while [ "$tentativas" -lt 20 ]; do
      status_hc="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)"
      if [ "$status_hc" = "healthy" ] || [ "$status_hc" = "running" ]; then
        break
      fi
      sleep 3
      tentativas=$((tentativas + 1))
    done
  fi

  _escrever_host_info
  _escrever_status "concluido" "Atualização concluída com sucesso." "$pedido_id" "$branch_req" "$solicitante" "$commit_ant" "$commit_novo" "" "$log_tmp" "$iniciado_em"
  echo "Atualização concluída: ${commit_ant:0:7} -> ${commit_novo:0:7}"
}

_processar_pedido() {
  if [ ! -f "$PEDIDO_FILE" ]; then
    return 0
  fi

  local parsed pedido_id branch solicitante
  parsed="$(python3 - "$PEDIDO_FILE" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    d = json.load(fh)
print(d.get("id", ""))
print(d.get("branch", ""))
print(d.get("solicitado_por", ""))
PY
)" || {
    rm -f "$PEDIDO_FILE"
    _escrever_status "erro" "Pedido de atualização corrompido" "" "" "" "$(git rev-parse HEAD 2>/dev/null || true)" "" "Arquivo pedido.json inválido." "" "$(date -Is)"
    return 1
  }

  rm -f "$PEDIDO_FILE"
  pedido_id="$(printf '%s\n' "$parsed" | sed -n '1p')"
  branch="$(printf '%s\n' "$parsed" | sed -n '2p')"
  solicitante="$(printf '%s\n' "$parsed" | sed -n '3p')"

  _executar_atualizacao "$pedido_id" "$branch" "$solicitante"
}

MODO="${1:---sync}"
case "$MODO" in
  --sync)
    _escrever_host_info
    ;;
  --daemon)
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
      exit 0
    fi
    _escrever_host_info
    _processar_pedido
    ;;
  --agora)
    exec 9>"$LOCK_FILE"
    flock -n 9 || { echo "Já existe uma atualização em andamento." >&2; exit 1; }
    _escrever_host_info
    branch_atual="$(git rev-parse --abbrev-ref HEAD)"
    _executar_atualizacao "manual-$(date +%s)" "$branch_atual" "cli"
    ;;
  *)
    echo "Uso: $0 [--sync|--daemon|--agora]" >&2
    exit 1
    ;;
esac
