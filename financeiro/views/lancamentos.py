from __future__ import annotations

import csv
import io
import os
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import services as sv
from ..audit import CAMPOS_LANCAMENTO, diff_snapshots, registrar_auditoria, snapshot_instancia
from ..forms import ImportacaoForm, LancamentoForm, categorias_qs
from ..importacao import analisar_arquivo_extrato, confirmar_importacao
from ..models import Cartao, Categoria, CentroCusto, Conta, Lancamento, RegistroAuditoria
from ..security import csv_seguro
from ..utils import fim_mes, hoje, mes_str, parse_mes
from ._comum import _nav_mes, _render_form, _voltar

User = get_user_model()


class _Echo:
    def write(self, value: str) -> str:
        return value


def _filtrar_lancamentos(request: HttpRequest):
    g = request.GET
    ini = parse_mes(g.get("mes"))
    qs = Lancamento.objects.select_related(
        "conta__titular",
        "conta_destino__titular",
        "cartao",
        "fatura",
        "categoria__pai",
        "centro",
        "fatura_paga__cartao",
        "veiculo",
        "criado_por",
        "atualizado_por",
    )
    todos = g.get("mes") == "todos"
    if not todos:
        qs = qs.filter(data__gte=ini, data__lte=fim_mes(ini))
    conta = g.get("conta", "")
    if conta.isdigit():
        qs = qs.filter(Q(conta_id=conta) | Q(conta_destino_id=conta))
    for campo in ("cartao", "centro", "veiculo", "criado_por"):
        valor = g.get(campo, "")
        if valor.isdigit():
            qs = qs.filter(**{f"{campo}_id": valor})
    categoria = g.get("categoria", "")
    if categoria.isdigit():
        qs = qs.filter(Q(categoria_id=categoria) | Q(categoria__pai_id=categoria))
    tipo = g.get("tipo", "")
    if tipo in ("R", "D", "E", "T", "F"):
        qs = qs.filter(tipo=tipo)
    situacao = g.get("situacao", "")
    if situacao == "pago":
        qs = qs.filter(pago=True)
    elif situacao == "pendente":
        qs = qs.filter(pago=False)
    elif situacao == "atrasado":
        qs = qs.filter(pago=False, vencimento__lt=hoje())
    conciliado = g.get("conciliado", "")
    if conciliado == "sim":
        qs = qs.filter(conciliado=True)
    elif conciliado == "nao":
        qs = qs.filter(conciliado=False)
    busca = g.get("q", "").strip()
    if busca:
        qs = qs.filter(Q(descricao__icontains=busca) | Q(observacao__icontains=busca))
    return qs, ini, todos


@login_required
def lancamentos(request: HttpRequest) -> HttpResponse:
    qs, ini, todos = _filtrar_lancamentos(request)
    pagina = Paginator(qs, 50).get_page(request.GET.get("page"))
    g = request.GET.copy()
    g.pop("page", None)
    g_sem_mes = g.copy()
    g_sem_mes.pop("mes", None)
    filtros_ativos = any(
        request.GET.get(k)
        for k in ("conta", "cartao", "centro", "categoria", "tipo", "situacao", "q", "veiculo", "criado_por", "conciliado")
    )
    return render(
        request,
        "financeiro/lancamentos.html",
        {
            "pagina": pagina,
            "nav": _nav_mes(request, ini),
            "todos": todos,
            "receitas": sv.soma(qs.filter(tipo="R")),
            "despesas": sv.despesas_liquidas(qs),
            "querystring": g.urlencode(),
            "querystring_sem_mes": g_sem_mes.urlencode(),
            "filtros_ativos": filtros_ativos,
            "f": request.GET,
            "contas": Conta.objects.select_related("titular"),
            "cartoes": Cartao.objects.all(),
            "centros": CentroCusto.objects.all(),
            "categorias": categorias_qs(),
            "usuarios": User.objects.order_by("username"),
            "hoje": hoje(),
        },
    )


@login_required
def lancamento_form(request: HttpRequest, pk: int | None = None, duplicar_pk: int | None = None) -> HttpResponse:
    inst = get_object_or_404(Lancamento, pk=pk) if pk else None
    if inst and inst.tipo == Lancamento.PAGAMENTO_FATURA:
        messages.info(request, "Pagamentos de fatura são gerenciados na própria fatura.")
        return redirect("fatura", pk=inst.fatura_paga_id)
    if inst and hasattr(inst, "abastecimento"):
        messages.info(request, "Este lançamento pertence a um abastecimento. Edite pela tela do veículo.")
        return redirect("abastecimento_editar", pk=inst.abastecimento.pk)
    if inst and hasattr(inst, "manutencao"):
        messages.info(request, "Este lançamento pertence a uma manutenção. Edite pela tela do veículo.")
        return redirect("manutencao_editar", pk=inst.manutencao.pk)

    initial: dict = {}
    if inst is None:
        if duplicar_pk:
            orig = get_object_or_404(Lancamento, pk=duplicar_pk)
            if orig.tipo in (Lancamento.RECEITA, Lancamento.DESPESA, Lancamento.ESTORNO, Lancamento.TRANSFERENCIA):
                initial = {
                    "tipo": orig.tipo,
                    "descricao": orig.descricao,
                    "valor": orig.valor,
                    "data": hoje(),
                    "pago": True,
                    "forma": "cartao" if orig.cartao_id else "conta",
                    "conta": orig.conta_id,
                    "conta_destino": orig.conta_destino_id,
                    "cartao": orig.cartao_id,
                    "categoria": orig.categoria_id,
                    "centro": orig.centro_id,
                    "veiculo": orig.veiculo_id,
                    "compartilhado": orig.compartilhado,
                    "pago_por": orig.pago_por_id,
                    "observacao": orig.observacao,
                }
        else:
            tipo = request.GET.get("tipo", "D")
            initial = {
                "tipo": tipo if tipo in ("R", "D", "E", "T") else "D",
                "data": hoje(),
                "pago": True,
                "forma": "cartao" if (request.GET.get("cartao") or request.GET.get("forma") == "cartao") else "conta",
            }
            for campo in ("conta", "cartao", "categoria", "centro", "veiculo"):
                valor = request.GET.get(campo, "")
                if valor.isdigit():
                    initial[campo] = valor

    antes = snapshot_instancia(inst, CAMPOS_LANCAMENTO) if inst else {}
    dados_antigos = (inst.data, inst.conta, inst.conta_destino) if inst else None

    form = LancamentoForm(request.POST or None, request.FILES or None, instance=inst, initial=initial)
    if request.method == "POST" and form.is_valid():
        lanc = form.save(commit=False)
        bloqueio = sv.verificar_bloqueio_lancamento(lanc.data, conta=lanc.conta, conta_destino=lanc.conta_destino)
        if not bloqueio and dados_antigos:
            bloqueio = sv.verificar_bloqueio_lancamento(
                dados_antigos[0], conta=dados_antigos[1], conta_destino=dados_antigos[2]
            )
        if bloqueio:
            form.add_error(None, bloqueio)
        else:
            if inst is None:
                lanc.criado_por = request.user
            lanc.atualizado_por = request.user
            parcelas = form.cleaned_data.get("parcelas") or 1
            aplicar_futuras = form.cleaned_data.get("aplicar_futuras")
            with transaction.atomic():
                if inst is None and lanc.cartao_id and lanc.tipo == Lancamento.DESPESA and parcelas > 1:
                    itens = sv.criar_parcelado(lanc, parcelas)
                    registrar_auditoria(
                        request.user,
                        RegistroAuditoria.CRIAR,
                        itens[0],
                        f"Compra parcelada ({parcelas}x): {lanc.descricao} (R$ {lanc.valor})",
                        {"parcelas": parcelas, "valor_total": str(lanc.valor)},
                        request=request,
                    )
                    messages.success(request, f"Compra lançada em {parcelas} parcelas.")
                else:
                    lanc.save()
                    if inst and aplicar_futuras and inst.grupo_parcelas and inst.parcela:
                        Lancamento.objects.filter(
                            grupo_parcelas=inst.grupo_parcelas, parcela__gt=inst.parcela
                        ).update(
                            descricao=lanc.descricao,
                            categoria=lanc.categoria,
                            centro=lanc.centro,
                            compartilhado=lanc.compartilhado,
                            atualizado_por=request.user,
                        )
                    depois = snapshot_instancia(lanc, CAMPOS_LANCAMENTO)
                    if inst:
                        diff = diff_snapshots(antes, depois)
                        registrar_auditoria(
                            request.user,
                            RegistroAuditoria.EDITAR,
                            lanc,
                            f"Editou lançamento: {lanc.descricao} (R$ {lanc.valor})",
                            {"alteracoes": diff},
                            request=request,
                        )
                    else:
                        registrar_auditoria(
                            request.user,
                            RegistroAuditoria.CRIAR,
                            lanc,
                            f"Novo lançamento: {lanc.descricao} (R$ {lanc.valor})",
                            {"dados": depois},
                            request=request,
                        )
                    messages.success(request, "Lançamento salvo.")
            if request.POST.get("acao") == "salvar_novo":
                qs_novo = f"?tipo={lanc.tipo}"
                if lanc.cartao_id:
                    qs_novo += f"&forma=cartao&cartao={lanc.cartao_id}"
                elif lanc.conta_id:
                    qs_novo += f"&conta={lanc.conta_id}"
                nxt = request.POST.get("next") or request.GET.get("next")
                if nxt:
                    from urllib.parse import quote
                    qs_novo += f"&next={quote(nxt)}"
                return redirect(f"{reverse('lancamento_novo')}{qs_novo}")
            return redirect(_voltar(request, f"{reverse('lancamentos')}?mes={mes_str(lanc.data)}"))

    extra = {"js_forma": True, "permitir_salvar_novo": inst is None, "auditoria_obj": inst}
    if inst:
        extra["excluir_url"] = reverse("lancamento_excluir", args=[inst.pk])
        extra["duplicar_url"] = reverse("lancamento_duplicar", args=[inst.pk])
        if inst.grupo_parcelas:
            extra["excluir_grupo"] = True
            extra["aviso"] = f"Parcela {inst.parcela_label}. As alterações de valor/data valem só para esta parcela."
        elif inst.recorrencia_id:
            extra["aviso"] = "Gerado por uma recorrência. Para mudar os próximos meses, edite a recorrência."
    return _render_form(
        request,
        form,
        "Editar lançamento" if inst else "Novo lançamento",
        _voltar(request, reverse("lancamentos")),
        **extra,
    )


@login_required
@require_POST
def lancamento_excluir(request: HttpRequest, pk: int) -> HttpResponse:
    lanc = get_object_or_404(Lancamento, pk=pk)
    if hasattr(lanc, "abastecimento") or hasattr(lanc, "manutencao"):
        messages.error(request, "Lançamentos do veículo devem ser excluídos pela tela do veículo.")
        return redirect(_voltar(request, reverse("lancamentos")))
    destino = _voltar(request, f"{reverse('lancamentos')}?mes={mes_str(lanc.data)}")
    if request.POST.get("todas") and lanc.grupo_parcelas:
        itens = list(Lancamento.objects.filter(grupo_parcelas=lanc.grupo_parcelas))
    else:
        itens = [lanc]

    for item in itens:
        bloq = sv.verificar_bloqueio_lancamento(item.data, conta=item.conta, conta_destino=item.conta_destino)
        if bloq:
            messages.error(request, bloq)
            return redirect(destino)

    with transaction.atomic():
        for item in itens:
            snap = snapshot_instancia(item, CAMPOS_LANCAMENTO)
            registrar_auditoria(
                request.user,
                RegistroAuditoria.EXCLUIR,
                item,
                f"Excluiu lançamento: {item.descricao} (R$ {item.valor})",
                {"snapshot": snap},
                request=request,
            )
            item.delete()
    messages.success(request, f"{len(itens)} lançamento(s) excluído(s).")
    if "/editar/" in destino:
        destino = reverse("lancamentos")
    return redirect(destino)


@login_required
@require_POST
def lancamento_pagar(request: HttpRequest, pk: int) -> HttpResponse:
    lanc = get_object_or_404(Lancamento, pk=pk)
    destino = _voltar(request, reverse("a_vencer"))
    if lanc.cartao_id or lanc.tipo == Lancamento.PAGAMENTO_FATURA:
        messages.error(request, "Compras no cartão são quitadas pelo pagamento da fatura.")
        return redirect(destino)
    bloq = sv.verificar_bloqueio_lancamento(lanc.data, conta=lanc.conta, conta_destino=lanc.conta_destino)
    if bloq:
        messages.error(request, bloq)
        return redirect(destino)

    with transaction.atomic():
        lanc.pago = not lanc.pago
        lanc.data_pagamento = hoje() if lanc.pago else None
        lanc.atualizado_por = request.user
        lanc.save()
        verbo = "recebido" if lanc.tipo == Lancamento.RECEITA else "pago"
        acao_txt = f"Marcou como {verbo}" if lanc.pago else "Voltou para pendente"
        registrar_auditoria(
            request.user,
            RegistroAuditoria.PAGAR,
            lanc,
            f"{acao_txt}: {lanc.descricao} (R$ {lanc.valor})",
            {"pago": lanc.pago},
            request=request,
        )
    messages.success(
        request,
        f"“{lanc.descricao}” marcado como {verbo}." if lanc.pago else f"“{lanc.descricao}” voltou para pendente.",
    )
    return redirect(destino)


@login_required
@require_POST
def lancamento_conciliar(request: HttpRequest, pk: int) -> HttpResponse:
    lanc = get_object_or_404(Lancamento, pk=pk)
    lanc.conciliado = not lanc.conciliado
    lanc.conciliado_em = timezone.now() if lanc.conciliado else None
    lanc.atualizado_por = request.user
    lanc.save(update_fields=["conciliado", "conciliado_em", "atualizado_por", "atualizado_em"])
    registrar_auditoria(
        request.user,
        RegistroAuditoria.CONCILIAR,
        lanc,
        f"{'Conciliou' if lanc.conciliado else 'Desconciliou'}: {lanc.descricao}",
        request=request,
    )
    return redirect(_voltar(request, reverse("lancamentos")))


@login_required
def exportar_csv(request: HttpRequest) -> StreamingHttpResponse:
    qs, ini, todos = _filtrar_lancamentos(request)
    nome = "lancamentos.csv" if todos else f"lancamentos_{mes_str(ini)}.csv"

    def linhas():
        writer = csv.writer(_Echo(), delimiter=";")
        yield "\ufeff"
        yield writer.writerow([
            "Data", "Vencimento", "Tipo", "Descrição", "Parcela", "Valor", "Situação",
            "Conciliado", "Data pagamento", "Conta", "Conta destino", "Cartão", "Fatura",
            "Categoria", "Centro de custo", "Veículo", "Lançado por", "Observação",
        ])
        for l in qs.order_by("data", "id").iterator(chunk_size=500):
            yield writer.writerow([
                l.data.strftime("%d/%m/%Y"),
                l.vencimento.strftime("%d/%m/%Y") if l.vencimento else "",
                l.get_tipo_display(),
                csv_seguro(l.descricao),
                l.parcela_label,
                str(l.valor).replace(".", ","),
                l.situacao,
                "Sim" if l.conciliado else "Não",
                l.data_pagamento.strftime("%d/%m/%Y") if l.data_pagamento else "",
                csv_seguro(l.conta),
                csv_seguro(l.conta_destino),
                csv_seguro(l.cartao),
                l.fatura.referencia.strftime("%m/%Y") if l.fatura_id else "",
                csv_seguro(l.categoria),
                csv_seguro(l.centro),
                csv_seguro(l.veiculo),
                csv_seguro(l.criado_por.username if l.criado_por else ""),
                csv_seguro(l.observacao),
            ])

    resposta = StreamingHttpResponse(linhas(), content_type="text/csv; charset=utf-8")
    resposta["Content-Disposition"] = f'attachment; filename="{nome}"'
    return resposta


@login_required
def comprovante_download(request: HttpRequest, pk: int) -> FileResponse:
    lanc = get_object_or_404(Lancamento, pk=pk)
    if not lanc.comprovante:
        raise Http404("Este lançamento não possui comprovante.")
    try:
        arq = lanc.comprovante.open("rb")
    except FileNotFoundError:
        raise Http404("Arquivo de comprovante não encontrado no disco.")
    return FileResponse(arq, as_attachment=False, filename=os.path.basename(lanc.comprovante.name))


@login_required
def importar_extrato(request: HttpRequest) -> HttpResponse:
    if request.method == "POST" and request.POST.get("confirmar") == "1":
        pendente = request.session.get("importacao_pendente")
        if not pendente:
            messages.error(request, "Sessão de importação expirada. Envie o arquivo novamente.")
            return redirect("importar_extrato")
        conta = Conta.objects.filter(pk=pendente.get("conta_id")).first() if pendente.get("conta_id") else None
        cartao = Cartao.objects.filter(pk=pendente.get("cartao_id")).first() if pendente.get("cartao_id") else None
        selecionados = []
        for idx, item in enumerate(pendente["itens"]):
            if not request.POST.get(f"incluir_{idx}"):
                continue
            tipo = request.POST.get(f"tipo_{idx}", item["tipo"])
            if tipo not in ("R", "D", "E"):
                tipo = item["tipo"]
            cat_str = request.POST.get(f"categoria_{idx}", "")
            cen_str = request.POST.get(f"centro_{idx}", "")
            selecionados.append({
                "data": date.fromisoformat(item["data"]),
                "valor": Decimal(item["valor"]),
                "tipo": tipo,
                "descricao": (request.POST.get(f"descricao_{idx}") or item["descricao"]).strip(),
                "hash": item["hash"],
                "categoria_id": int(cat_str) if cat_str.isdigit() else None,
                "centro_id": int(cen_str) if cen_str.isdigit() else None,
            })
        if not selecionados:
            messages.warning(request, "Nenhum lançamento selecionado para importação.")
            return redirect("importar_extrato")
        try:
            criados, ignorados = confirmar_importacao(
                selecionados, conta=conta, cartao=cartao, usuario=request.user, request=request
            )
            request.session.pop("importacao_pendente", None)
            messages.success(
                request,
                f"Importação concluída: {criados} lançamento(s) criado(s)"
                + (f", {ignorados} duplicado(s) ignorado(s)." if ignorados else "."),
            )
            return redirect("lancamentos")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("importar_extrato")

    form = ImportacaoForm(request.POST or None, request.FILES or None)
    preview = None
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            itens = analisar_arquivo_extrato(
                cd["arquivo"],
                conta=cd.get("conta"),
                cartao=cd.get("cartao"),
                inverter_sinais_cartao=bool(cd.get("inverter_sinais_cartao")),
            )
            request.session["importacao_pendente"] = {
                "conta_id": cd["conta"].pk if cd.get("conta") else None,
                "cartao_id": cd["cartao"].pk if cd.get("cartao") else None,
                "destino_nome": str(cd.get("cartao") or cd.get("conta")),
                "itens": [
                    {
                        "data": i["data"].isoformat(),
                        "valor": str(i["valor"]),
                        "tipo": i["tipo"],
                        "descricao": i["descricao"],
                        "hash": i["hash"],
                        "categoria_id": i.get("categoria_id"),
                        "centro_id": i.get("centro_id"),
                        "ja_existe": i["ja_existe"],
                    }
                    for i in itens
                ],
            }
            preview = request.session["importacao_pendente"]
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))

    return render(
        request,
        "financeiro/importar.html",
        {
            "form": form,
            "preview": preview,
            "categorias": list(categorias_qs().filter(ativo=True)),
            "centros": list(CentroCusto.objects.filter(ativo=True)),
        },
    )
