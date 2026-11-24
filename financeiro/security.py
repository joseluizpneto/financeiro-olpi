from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from .audit import obter_ip, registrar_auditoria
from .models import RegistroAuditoria, TentativaLogin, TokenAPI

EXTENSOES_COMPROVANTE = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
TAMANHO_MAX_COMPROVANTE = 10 * 1024 * 1024  # 10 MB
PREFIXOS_FORMULA_CSV = ("=", "+", "-", "@", "\t", "\r")


def csv_seguro(valor: object) -> str:
    """Neutraliza CSV Formula Injection para abertura segura em planilhas."""
    texto = str(valor if valor is not None else "")
    if not texto:
        return ""
    limpo = texto.lstrip()
    if texto[0] in PREFIXOS_FORMULA_CSV or (limpo and limpo[0] in ("=", "+", "-", "@")):
        return f"'{texto}"
    return texto


def validar_comprovante(arquivo) -> None:
    if not arquivo:
        return
    ext = os.path.splitext(getattr(arquivo, "name", "") or "")[1].lower()
    if ext not in EXTENSOES_COMPROVANTE:
        raise ValidationError(
            "Formato não permitido. Envie PDF, JPG, PNG ou WEBP."
        )
    if getattr(arquivo, "size", 0) > TAMANHO_MAX_COMPROVANTE:
        raise ValidationError("O comprovante deve ter no máximo 10 MB.")


def _chaves_tentativa(ip: str | None, username: str) -> tuple[str, str]:
    ip_norm = ip or "sem-ip"
    user_norm = (username or "").strip().lower()[:150]
    return f"u:{ip_norm}:{user_norm}", f"ip:{ip_norm}"


def _esta_bloqueado(chave: str) -> TentativaLogin | None:
    agora = timezone.now()
    t = TentativaLogin.objects.filter(chave=chave, bloqueado_ate__gt=agora).first()
    return t


def _registrar_falha(chave: str, ip: str | None, username: str, limite: int, minutos: int) -> bool:
    agora = timezone.now()
    t, _ = TentativaLogin.objects.get_or_create(
        chave=chave, defaults={"ip": ip, "username": username, "falhas": 0}
    )
    if t.bloqueado_ate and t.bloqueado_ate <= agora:
        t.falhas = 0
        t.bloqueado_ate = None
    t.ip = ip
    t.username = username
    t.falhas += 1
    bloqueou_agora = False
    if t.falhas >= limite:
        t.bloqueado_ate = agora + timedelta(minutes=minutos)
        bloqueou_agora = True
    t.save()
    return bloqueou_agora


class LoginProtegidoView(LoginView):
    """Login com proteção contra força bruta e auditoria de acesso."""

    template_name = "registration/login.html"

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        ip = obter_ip(request)
        username = (request.POST.get("username") or "").strip().lower()[:150]
        chave_user, chave_ip = _chaves_tentativa(ip, username)
        bloqueio = _esta_bloqueado(chave_user) or _esta_bloqueado(chave_ip)
        if bloqueio:
            form = self.get_form()
            minutos_restantes = max(1, int((bloqueio.bloqueado_ate - timezone.now()).total_seconds() // 60) + 1)
            msg = f"Muitas tentativas inválidas. Aguarde {minutos_restantes} min ou peça desbloqueio no Painel Admin."
            ctx = self.get_context_data(form=form, bloqueio_msg=msg)
            return self.render_to_response(ctx, status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form) -> HttpResponse:
        ip = obter_ip(self.request)
        username = (form.get_user().username or "").lower()
        chave_user, chave_ip = _chaves_tentativa(ip, username)
        TentativaLogin.objects.filter(chave__in=[chave_user, chave_ip]).delete()
        registrar_auditoria(
            form.get_user(),
            RegistroAuditoria.LOGIN,
            "auth",
            f"Login realizado: {form.get_user().username}",
            request=self.request,
        )
        return super().form_valid(form)

    def form_invalid(self, form) -> HttpResponse:
        ip = obter_ip(self.request)
        username = (self.request.POST.get("username") or "").strip().lower()[:150]
        chave_user, chave_ip = _chaves_tentativa(ip, username)
        limite = int(getattr(settings, "LOGIN_MAX_TENTATIVAS", 5))
        minutos = int(getattr(settings, "LOGIN_BLOQUEIO_MINUTOS", 15))
        bloq_u = _registrar_falha(chave_user, ip, username, limite, minutos)
        bloq_ip = _registrar_falha(chave_ip, ip, "", limite * 3, minutos)
        if bloq_u or bloq_ip:
            registrar_auditoria(
                None,
                RegistroAuditoria.LOGIN,
                "auth",
                f"Acesso bloqueado temporariamente ({username or 'IP'})",
                {"ip": ip, "username": username},
                request=self.request,
            )
            msg = f"Muitas tentativas inválidas. Aguarde {minutos} min ou peça desbloqueio no Painel Admin."
            return self.render_to_response(self.get_context_data(form=form, bloqueio_msg=msg), status=429)
        registrar_auditoria(
            None,
            RegistroAuditoria.LOGIN,
            "auth",
            f"Tentativa de login inválida ({username or 'vazio'})",
            {"ip": ip, "username": username},
            request=self.request,
        )
        return super().form_invalid(form)


def gerar_token_api(nome: str, usuario) -> tuple[TokenAPI, str]:
    token_plano = f"fin_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(token_plano.encode("utf-8")).hexdigest()
    inst = TokenAPI.objects.create(
        nome=nome.strip()[:80],
        usuario=usuario,
        prefixo=token_plano[:12],
        token_hash=token_hash,
        ativo=True,
    )
    return inst, token_plano


def autenticar_token_api(request: HttpRequest) -> TokenAPI | None:
    auth = (request.META.get("HTTP_AUTHORIZATION") or "").strip()
    if not auth.lower().startswith("bearer "):
        return None
    token_plano = auth[7:].strip()
    if not token_plano:
        return None
    digest = hashlib.sha256(token_plano.encode("utf-8")).hexdigest()
    tok = (
        TokenAPI.objects.select_related("usuario")
        .filter(token_hash=digest, ativo=True, usuario__is_active=True)
        .first()
    )
    if tok is None or not hmac.compare_digest(tok.token_hash, digest):
        return None
    tok.ultimo_uso = timezone.now()
    tok.save(update_fields=["ultimo_uso"])
    return tok
