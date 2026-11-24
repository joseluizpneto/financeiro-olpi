from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

from .models import Integracao

RE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
RE_SHA = re.compile(r"^[0-9a-f]{7,40}$")

_RE_GHP = re.compile(r"ghp_[A-Za-z0-9_]+")
_RE_GH_PAT = re.compile(r"github_pat_[A-Za-z0-9_]+")
_RE_URL_CREDS = re.compile(r"(https?://)[^@\s/]+@")

_JANELA_AGENTE = timedelta(minutes=5)
_TIMEOUT_PENDENTE = timedelta(minutes=15)
_TIMEOUT_EXECUTANDO = timedelta(minutes=20)


def normalizar_repositorio(valor: str) -> str:
    """Aceita owner/repo, URL HTTPS ou SSH do GitHub e retorna owner/repo válido."""
    s = (valor or "").strip()
    if not s:
        return ""
    if s.startswith("git@github.com:"):
        s = s[len("git@github.com:") :]
    elif s.startswith("ssh://git@github.com/"):
        s = s[len("ssh://git@github.com/") :]
    elif s.startswith(("https://", "http://")):
        parsed = urllib.parse.urlparse(s)
        if (parsed.hostname or "").lower() != "github.com":
            return ""
        s = parsed.path.lstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    s = s.strip("/")
    if not RE_REPO.match(s) or ".." in s:
        return ""
    return s


def validar_branch(valor: str) -> str:
    """Valida e retorna o nome da branch ou string vazia se inválida."""
    s = (valor or "").strip()
    if not s or ".." in s or s.endswith("/") or s.endswith(".lock") or "//" in s:
        return ""
    if not RE_BRANCH.match(s):
        return ""
    return s


def sanitizar_segredos(texto: str, *segredos: str) -> str:
    """Remove tokens do GitHub e credenciais em URLs de mensagens e logs."""
    res = str(texto or "")
    for seg in segredos:
        if seg:
            res = res.replace(seg, "***")
    res = _RE_GHP.sub("***", res)
    res = _RE_GH_PAT.sub("***", res)
    res = _RE_URL_CREDS.sub(r"\1***@", res)
    return res


def _mascarar_token(valor: str) -> str:
    if not valor:
        return ""
    if len(valor) <= 8:
        return "••••"
    return f"{valor[:4]}••••{valor[-4:]}"


def diretorio_atualizacao() -> Path:
    return Path(getattr(settings, "GIT_UPDATE_DIR", Path(settings.BASE_DIR) / "data" / "atualizacao"))


def _ler_json(caminho: Path) -> dict[str, Any] | None:
    if not caminho.is_file():
        return None
    try:
        with caminho.open("r", encoding="utf-8") as fh:
            dados = json.load(fh)
        return dados if isinstance(dados, dict) else None
    except (OSError, ValueError):
        return None


def _escrever_json_atomico(caminho: Path, dados: dict[str, Any]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(f".{caminho.name}.{secrets.token_hex(6)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(dados, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        try:
            os.chmod(tmp, 0o664)
        except OSError:
            pass
        os.replace(tmp, caminho)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _parse_iso(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt
    except ValueError:
        return None


def obter_host_info() -> dict[str, Any] | None:
    dados = _ler_json(diretorio_atualizacao() / "host_info.json")
    if not dados:
        return None
    hb = _parse_iso(dados.get("heartbeat_em"))
    ativo = bool(hb and (timezone.now() - hb) <= _JANELA_AGENTE)
    dados["agente_ativo"] = ativo
    return dados


def _rodar_git(*args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(settings.BASE_DIR),
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if res.returncode != 0:
        return ""
    return res.stdout.strip()


def _git_local_info() -> dict[str, Any] | None:
    if not (Path(settings.BASE_DIR) / ".git").exists():
        return None
    try:
        commit = _rodar_git("rev-parse", "HEAD")
        if not commit or not RE_SHA.match(commit):
            return None
        branch = _rodar_git("rev-parse", "--abbrev-ref", "HEAD")
        log_raw = _rodar_git("log", "-1", "--format=%cI%n%an%n%s")
        partes = log_raw.splitlines() if log_raw else []
        data_commit = partes[0] if len(partes) > 0 else ""
        autor = partes[1] if len(partes) > 1 else ""
        mensagem = partes[2] if len(partes) > 2 else ""
        remote_url = _rodar_git("remote", "get-url", "origin")
        porcelain = _rodar_git("status", "--porcelain")
        return {
            "commit": commit,
            "commit_curto": commit[:7],
            "branch": "" if branch == "HEAD" else branch,
            "data_commit": data_commit,
            "autor": autor,
            "mensagem": mensagem,
            "repositorio_detectado": normalizar_repositorio(remote_url),
            "alteracoes_locais": bool(porcelain),
        }
    except (OSError, subprocess.SubprocessError):
        return None


def obter_versao_atual() -> dict[str, Any]:
    host = obter_host_info()
    if host and host.get("commit"):
        commit = str(host.get("commit", "")).strip()
        return {
            "commit": commit,
            "commit_curto": str(host.get("commit_curto") or commit[:7]),
            "branch": str(host.get("branch") or "main"),
            "mensagem": str(host.get("mensagem") or ""),
            "autor": str(host.get("autor") or ""),
            "data_commit": str(host.get("data_commit") or ""),
            "repositorio_detectado": normalizar_repositorio(str(host.get("repositorio") or "")),
            "alteracoes_locais": bool(host.get("alteracoes_locais", False)),
            "origem": "host_info",
            "agente_ativo": bool(host.get("agente_ativo", False)),
            "heartbeat_em": str(host.get("heartbeat_em") or ""),
        }

    env_commit = os.environ.get("APP_GIT_COMMIT", "").strip()
    env_branch = os.environ.get("APP_GIT_BRANCH", "").strip()
    env_date = os.environ.get("APP_GIT_DATE", "").strip()
    if env_commit:
        return {
            "commit": env_commit,
            "commit_curto": env_commit[:7],
            "branch": env_branch or "main",
            "mensagem": "",
            "autor": "",
            "data_commit": env_date,
            "repositorio_detectado": "",
            "alteracoes_locais": False,
            "origem": "build_env",
            "agente_ativo": bool(host and host.get("agente_ativo")),
            "heartbeat_em": str(host.get("heartbeat_em") or "") if host else "",
        }

    local = _git_local_info()
    if local:
        return {
            **local,
            "origem": "git_local",
            "agente_ativo": bool(host and host.get("agente_ativo")),
            "heartbeat_em": str(host.get("heartbeat_em") or "") if host else "",
        }

    return {
        "commit": "",
        "commit_curto": "",
        "branch": env_branch or "main",
        "mensagem": "",
        "autor": "",
        "data_commit": env_date,
        "repositorio_detectado": "",
        "alteracoes_locais": False,
        "origem": "desconhecida",
        "agente_ativo": bool(host and host.get("agente_ativo")),
        "heartbeat_em": str(host.get("heartbeat_em") or "") if host else "",
    }


def obter_config_git() -> dict[str, Any]:
    integ = Integracao.objects.filter(provedor=Integracao.GIT).first()
    cfg = (integ.config or {}) if integ else {}
    versao = obter_versao_atual()

    repo = normalizar_repositorio(
        cfg.get("repositorio")
        or os.environ.get("GIT_UPDATE_REPO", "")
        or versao.get("repositorio_detectado", "")
    )
    branch = validar_branch(
        cfg.get("branch")
        or os.environ.get("GIT_UPDATE_BRANCH", "")
        or versao.get("branch", "")
        or "main"
    ) or "main"

    token_db = (integ.segredo if integ and integ.segredo else "").strip()
    token_env = os.environ.get("GIT_UPDATE_TOKEN", "").strip()
    if token_db:
        token, token_origem = token_db, "painel"
    elif token_env:
        token, token_origem = token_env, "env"
    else:
        token, token_origem = "", ""

    return {
        "repositorio": repo,
        "branch": branch,
        "token": token,
        "token_origem": token_origem,
        "token_mascarado": _mascarar_token(token),
        "ultima_verificacao": cfg.get("ultima_verificacao"),
    }


def salvar_config_git(
    usuario, repositorio: str, branch: str, token: str = "", remover_token: bool = False
) -> Integracao:
    repo_norm = normalizar_repositorio(repositorio)
    branch_norm = validar_branch(branch) or "main"
    integ, _ = Integracao.objects.get_or_create(provedor=Integracao.GIT)
    cfg = dict(integ.config or {})
    mudou_alvo = cfg.get("repositorio") != repo_norm or cfg.get("branch") != branch_norm
    cfg["repositorio"] = repo_norm
    cfg["branch"] = branch_norm
    if mudou_alvo:
        cfg.pop("ultima_verificacao", None)
    integ.ativo = bool(repo_norm)
    integ.config = cfg
    if token.strip():
        integ.segredo = token.strip()
    elif remover_token:
        integ.segredo = ""
    integ.atualizado_por = usuario
    integ.save()
    return integ


def _github_get(path: str, token: str) -> dict[str, Any]:
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Financeiro-OLPI",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("Token do GitHub inválido ou expirado (HTTP 401).") from exc
        if exc.code == 403:
            raise RuntimeError(
                "Acesso negado ou limite da API do GitHub atingido (HTTP 403). Verifique o token."
            ) from exc
        if exc.code == 404:
            raise FileNotFoundError(
                "Repositório, branch ou commit não encontrado no GitHub (HTTP 404). "
                "Em repositórios privados, confira se o token tem permissão de leitura (Contents: Read-only)."
            ) from exc
        raise RuntimeError(f"GitHub retornou erro HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Falha de conexão com o GitHub: {sanitizar_segredos(str(exc.reason), token)}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Erro ao consultar GitHub: {sanitizar_segredos(str(exc), token)}") from exc


def _resumir_commit(item: dict[str, Any]) -> dict[str, str]:
    sha = str(item.get("sha") or "")
    commit_data = item.get("commit") or {}
    author = commit_data.get("author") or {}
    mensagem_completa = str(commit_data.get("message") or "").strip()
    primeira_linha = mensagem_completa.splitlines()[0] if mensagem_completa else ""
    return {
        "sha": sha,
        "sha_curto": sha[:7],
        "mensagem": primeira_linha[:160],
        "autor": str(author.get("name") or "")[:80],
        "data": str(author.get("date") or ""),
        "url": str(item.get("html_url") or ""),
    }


def _persistir_ultima_verificacao(resultado: dict[str, Any]) -> None:
    integ, _ = Integracao.objects.get_or_create(provedor=Integracao.GIT)
    cfg = dict(integ.config or {})
    cfg["ultima_verificacao"] = resultado
    integ.config = cfg
    integ.save(update_fields=["config", "atualizado_em"])


def verificar_atualizacoes() -> dict[str, Any]:
    cfg = obter_config_git()
    versao = obter_versao_atual()
    repo = cfg["repositorio"]
    branch = cfg["branch"]
    token = cfg["token"]

    if not repo:
        return {
            "ok": False,
            "erro": "Repositório Git não configurado. Informe owner/repositorio ou URL do GitHub.",
            "verificado_em": timezone.now().isoformat(),
        }

    branch_enc = urllib.parse.quote(branch, safe="")
    commit_local = versao.get("commit", "")
    agora_iso = timezone.now().isoformat()

    try:
        if RE_SHA.match(commit_local):
            try:
                comp = _github_get(f"/repos/{repo}/compare/{commit_local}...{branch_enc}", token)
            except FileNotFoundError:
                head_info = _github_get(f"/repos/{repo}/commits/{branch_enc}", token)
                ultimo = _resumir_commit(head_info)
                resultado = {
                    "ok": True,
                    "estado": "desconhecido",
                    "tem_atualizacao": False,
                    "mensagem_status": (
                        "O commit local não foi encontrado no histórico remoto (pode ser um commit local ainda não enviado)."
                    ),
                    "repositorio": repo,
                    "branch": branch,
                    "commit_local": commit_local,
                    "commit_remoto": ultimo["sha"],
                    "commit_remoto_curto": ultimo["sha_curto"],
                    "ahead_by": 0,
                    "behind_by": 0,
                    "commits": [ultimo] if ultimo["sha"] else [],
                    "tem_migracoes": False,
                    "tem_dependencias": False,
                    "verificado_em": agora_iso,
                }
                _persistir_ultima_verificacao(resultado)
                return resultado

            status_gh = comp.get("status", "")
            ahead_by = int(comp.get("ahead_by") or 0)
            behind_by = int(comp.get("behind_by") or 0)
            commits_raw = list(comp.get("commits") or [])
            commits = [_resumir_commit(c) for c in reversed(commits_raw[-20:])]
            arquivos = [str(f.get("filename") or "") for f in (comp.get("files") or [])]

            if status_gh == "identical" or (ahead_by == 0 and behind_by == 0):
                estado = "atualizado"
                tem_atualizacao = False
                msg_status = "O sistema já está na versão mais recente."
            elif status_gh == "ahead" and behind_by == 0:
                estado = "disponivel"
                tem_atualizacao = True
                msg_status = f"Há {ahead_by} alteração(ões) disponível(is) na branch {branch}."
            elif status_gh == "behind":
                estado = "local_a_frente"
                tem_atualizacao = False
                msg_status = f"O servidor está {behind_by} commit(s) à frente do repositório remoto."
            else:
                estado = "divergente"
                tem_atualizacao = False
                msg_status = (
                    f"Histórico divergente ({ahead_by} no remoto, {behind_by} no local). "
                    "Atualização automática bloqueada por segurança; resolva manualmente via Git."
                )

            commit_remoto = commits[0]["sha"] if commits else commit_local
            resultado = {
                "ok": True,
                "estado": estado,
                "tem_atualizacao": tem_atualizacao,
                "mensagem_status": msg_status,
                "repositorio": repo,
                "branch": branch,
                "commit_local": commit_local,
                "commit_remoto": commit_remoto,
                "commit_remoto_curto": commit_remoto[:7],
                "ahead_by": ahead_by,
                "behind_by": behind_by,
                "commits": commits,
                "tem_migracoes": any(a.startswith("financeiro/migrations/") for a in arquivos),
                "tem_dependencias": any(
                    a in {"requirements.txt", "Dockerfile", "docker-compose.yml"} for a in arquivos
                ),
                "verificado_em": agora_iso,
            }
            _persistir_ultima_verificacao(resultado)
            return resultado

        head_info = _github_get(f"/repos/{repo}/commits/{branch_enc}", token)
        ultimo = _resumir_commit(head_info)
        resultado = {
            "ok": True,
            "estado": "versao_local_desconhecida",
            "tem_atualizacao": bool(ultimo["sha"]),
            "mensagem_status": (
                "Commit local não identificado neste container; execute o sincronizador no host ou aplique a atualização para alinhar."
            ),
            "repositorio": repo,
            "branch": branch,
            "commit_local": "",
            "commit_remoto": ultimo["sha"],
            "commit_remoto_curto": ultimo["sha_curto"],
            "ahead_by": 0,
            "behind_by": 0,
            "commits": [ultimo] if ultimo["sha"] else [],
            "tem_migracoes": False,
            "tem_dependencias": False,
            "verificado_em": agora_iso,
        }
        _persistir_ultima_verificacao(resultado)
        return resultado

    except (FileNotFoundError, RuntimeError) as exc:
        resultado = {
            "ok": False,
            "erro": sanitizar_segredos(str(exc), token),
            "repositorio": repo,
            "branch": branch,
            "verificado_em": agora_iso,
        }
        _persistir_ultima_verificacao(resultado)
        return resultado


def obter_status_atualizacao() -> dict[str, Any]:
    pasta = diretorio_atualizacao()
    agora = timezone.now()

    pedido = _ler_json(pasta / "pedido.json")
    if pedido:
        solicitado_dt = _parse_iso(pedido.get("solicitado_em"))
        atrasado = bool(solicitado_dt and (agora - solicitado_dt) > _TIMEOUT_PENDENTE)
        return {
            "estado": "pendente",
            "id": pedido.get("id", ""),
            "branch": pedido.get("branch", ""),
            "commit_alvo": pedido.get("commit_alvo", ""),
            "solicitado_por": pedido.get("solicitado_por", ""),
            "solicitado_em": pedido.get("solicitado_em", ""),
            "etapa": "Aguardando o agente no host iniciar a atualização…",
            "atrasado": atrasado,
            "log": "",
            "erro": "",
        }

    status = _ler_json(pasta / "status.json")
    if status:
        estado = str(status.get("estado") or "ocioso")
        iniciado_dt = _parse_iso(status.get("iniciado_em") or status.get("atualizado_em"))
        atrasado = estado == "executando" and bool(iniciado_dt and (agora - iniciado_dt) > _TIMEOUT_EXECUTANDO)
        return {
            "estado": estado,
            "id": status.get("id", ""),
            "branch": status.get("branch", ""),
            "commit_alvo": status.get("commit_alvo", ""),
            "commit_anterior": status.get("commit_anterior", ""),
            "commit_novo": status.get("commit_novo", ""),
            "etapa": status.get("etapa", ""),
            "solicitado_por": status.get("solicitado_por", ""),
            "iniciado_em": status.get("iniciado_em", ""),
            "finalizado_em": status.get("finalizado_em", ""),
            "atualizado_em": status.get("atualizado_em", ""),
            "atrasado": atrasado,
            "log": sanitizar_segredos(str(status.get("log") or "")),
            "erro": sanitizar_segredos(str(status.get("erro") or "")),
        }

    return {"estado": "ocioso", "etapa": "", "atrasado": False, "log": "", "erro": ""}


def solicitar_atualizacao(
    usuario,
    branch: str,
    commit_alvo: str = "",
    commit_atual: str = "",
) -> tuple[bool, str, dict[str, Any] | None]:
    branch_norm = validar_branch(branch)
    if not branch_norm:
        return False, "Nome de branch inválido.", None

    alvo_norm = (commit_alvo or "").strip()
    if alvo_norm and not RE_SHA.match(alvo_norm):
        return False, "Hash do commit alvo inválido.", None

    atual = obter_status_atualizacao()
    if atual["estado"] in ("pendente", "executando") and not atual.get("atrasado"):
        return False, "Já existe uma atualização pendente ou em andamento.", None

    versao = obter_versao_atual()
    if versao.get("alteracoes_locais"):
        return (
            False,
            "Há alterações locais não commitadas no servidor. Resolva no servidor antes de atualizar pelo painel.",
            None,
        )

    pedido = {
        "id": secrets.token_hex(8),
        "branch": branch_norm,
        "commit_alvo": alvo_norm,
        "commit_atual": (commit_atual or versao.get("commit") or "").strip(),
        "solicitado_por": getattr(usuario, "username", str(usuario)),
        "solicitado_em": timezone.now().isoformat(),
    }
    try:
        _escrever_json_atomico(diretorio_atualizacao() / "pedido.json", pedido)
    except OSError as exc:
        return (
            False,
            f"Não foi possível gravar o pedido de atualização na pasta compartilhada: {exc}",
            None,
        )
    return True, "Atualização autorizada e enfileirada para execução no servidor.", pedido


def cancelar_pedido_pendente() -> bool:
    pedido_path = diretorio_atualizacao() / "pedido.json"
    if not pedido_path.exists():
        return False
    try:
        pedido_path.unlink()
        return True
    except OSError:
        return False
