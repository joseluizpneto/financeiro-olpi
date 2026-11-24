from __future__ import annotations

from django.db.utils import OperationalError, ProgrammingError
from django.http import HttpRequest

from .models import ConfiguracaoSistema
from .services import tem_empresa_ativa


def obter_identidade_sistema() -> tuple[str, str]:
    """Obtém nome e subtítulo configurados ou retorna os valores padrão."""
    nome_padrao = ConfiguracaoSistema.NOME_PADRAO
    subtitulo_padrao = ConfiguracaoSistema.SUBTITULO_PADRAO
    try:
        cfg = ConfiguracaoSistema.objects.order_by("pk").first()
    except (OperationalError, ProgrammingError):
        return nome_padrao, subtitulo_padrao
    if cfg is None:
        return nome_padrao, subtitulo_padrao
    nome = (cfg.nome_sistema or "").strip() or nome_padrao
    return nome, cfg.subtitulo_sistema


def configuracoes_globais(request: HttpRequest) -> dict[str, str]:
    nome, subtitulo = obter_identidade_sistema()
    return {
        "nome_sistema": nome,
        "subtitulo_sistema": subtitulo,
    }


def contexto_global(request: HttpRequest) -> dict[str, bool]:
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"tem_empresa": False}
    return {"tem_empresa": tem_empresa_ativa(request)}
