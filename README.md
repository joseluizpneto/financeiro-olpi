# OLPI — Orçamento, Lançamentos, Patrimônio e Investimentos

**OLPI** (**Orçamento, Lançamentos, Patrimônio e Investimentos**) é um sistema de controle financeiro pessoal e empresarial (PF e PJ): contas bancárias, cartões de crédito com faturas e parcelas, despesas recorrentes, contas a vencer, fluxo de caixa projetado, orçamento com vigência, metas, rateio entre titulares, importação OFX/CSV, conciliação e fechamento mensal, controle do veículo, relatório da empresa e Painel Admin da aplicação.

Feito em **Django 5.2 + PostgreSQL 16**, empacotado com **Docker Compose**. As telas são responsivas para o celular (com suporte a instalação como PWA), permitem personalizar o nome e o subtítulo do sistema diretamente pelo Django Admin e funcionam sem internet externa (bibliotecas embutidas).

---

## 1. Subir o sistema

### Requisitos
- Docker e Docker Compose (v2) instalados no servidor (WSL2 ou Linux).

### No servidor WSL2 com Cloudflare Tunnel (recomendado)

Por padrão, o `docker-compose.yml` vincula a aplicação exclusivamente a `127.0.0.1:8000` (sem exposição na rede local ou internet) e mantém o PostgreSQL isolado em uma rede interna do Docker (`backend`).

```bash
cp .env.example .env
nano .env                 # preencha SECRET_KEY (>= 50 chars), POSTGRES_PASSWORD e DJANGO_SUPERUSER_PASSWORD
                          # ajuste ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS e COOKIE_SECURE=True
docker compose up -d --build
```

No `.env`:
```dotenv
ALLOWED_HOSTS=localhost,127.0.0.1,financas.seudominio.com.br
CSRF_TRUSTED_ORIGINS=https://financas.seudominio.com.br
COOKIE_SECURE=True
SECURE_SSL_REDIRECT=True
```

Configure o agente `cloudflared` local (Cloudflare Tunnel) para encaminhar o domínio público para `http://127.0.0.1:8000`.
Após o primeiro login, recomenda-se remover `DJANGO_SUPERUSER_PASSWORD` do `.env`.

> Para gerar `SECRET_KEY`: `python3 -c "import secrets; print(secrets.token_urlsafe(60))"`

### Acesso local HTTP ou VPS com Caddy (opcional)

- **Teste local sem HTTPS:** defina `COOKIE_SECURE=False` e `SECURE_SSL_REDIRECT=False` no `.env` e acesse `http://127.0.0.1:8000` na máquina local.
- **VPS com Caddy:** defina `DOMINIO=financas.seudominio.com.br` no `.env` e suba com `docker compose --profile https up -d --build`. Os binds do Caddy ficam em `127.0.0.1:${CADDY_HTTP_PORT:-80}` e `127.0.0.1:${CADDY_HTTPS_PORT:-443}` por padrão.

---

## 2. Painel Admin da Aplicação, Usuários e Integrações (`/painel-admin/`)

No menu do usuário (ou em **Configurações › Painel Admin da Aplicação**):

- **Usuários:** crie e edite os acessos dos usuários, vincule cada usuário ao seu **Titular** e configure o **Telegram Chat ID** individual, além de redefinir senhas.
- **Auditoria:** todos os lançamentos, edições (com diff campo a campo), exclusões, pagamentos, conciliações e ações administrativas registram **quem fez, quando e de qual IP**.
- **Sessões & Login:** visualize sessões abertas, encerre sessões antigas e desbloqueie tentativas de login travadas pela proteção contra força bruta (`LOGIN_MAX_TENTATIVAS` / `LOGIN_BLOQUEIO_MINUTOS`).
- **Integrações (Telegram, IA e Alertas):** configure o Bot do Telegram, credenciais/instruções para futuro Assistente de IA (Gemini, OpenAI, Anthropic ou Ollama) e regras de alertas automáticos (sem expor chaves já salvas na tela).
- **Tokens de API:** emita e revogue tokens `Bearer` para automações/bots consumirem `GET /api/v1/resumo/` em JSON.
- **Atualizações via Git:** administradores (`is_staff` / `is_superuser`) podem verificar alterações disponíveis no GitHub (incluindo repositórios privados via token PAT) e autorizar o rebuild automático do container com confirmação explícita.

---

## 3. Primeiros passos e recursos

Em **Configurações**:

1. **Titulares**: vêm com padrão genérico (*Titular 1*, *Titular 2*) ou podem ser definidos via `SEED_TITULARES` no `.env` antes da primeira subida (e editados depois em **Configurações** e vinculados aos usuários no Painel Admin).
2. **Módulo empresarial (opcional)**: o menu **Empresa** e o filtro **PF × PJ** só aparecem quando houver ao menos um centro de custo ativo marcado como **é empresa** — preencha `SEED_EMPRESA_NOME` no `.env` antes da primeira subida ou cadastre depois em **Configurações › Centros de custo**.
3. **Contas bancárias**: cadastre com o **saldo inicial** e a **data do saldo inicial** (movimentos anteriores a essa data não distorcem o saldo atual).
   - Na conta da empresa (PJ), caso utilize, escolha **Centro de custo padrão = Empresa** (ou o nome cadastrado para sua empresa).
4. **Cartões de crédito**: nome, limite, **dia de fechamento**, **dia de vencimento** e a conta que paga a fatura.
5. **Veículos**: cadastre o carro com o hodômetro atual em "km inicial".
6. **Categorias e Regras automáticas**: ajuste as categorias e cadastre regras por palavra-chave (ex.: `IFOOD`, `POSTO`) para categorizar importações OFX/CSV automaticamente.
7. **Recorrentes**: cadastre aluguel, energia, internet, plano de saúde, DAS…

### Guia rápido de uso

| Situação | Como lançar |
|---|---|
| Compra no débito/PIX | **Despesa**, pagar com *Conta bancária* |
| Compra no cartão | **Despesa**, pagar com *Cartão*. Informe as **parcelas**: cada uma cai numa fatura consecutiva |
| Conta a pagar futura (boleto) | **Despesa** com "Já foi pago" desmarcado e o **vencimento** (anexe o boleto/comprovante se desejar) |
| Salário, recebimento de cliente | **Receita** na conta correspondente |
| Mover dinheiro / Pró-labore | **Transferência**: não conta como receita nem despesa e aparece como retirada/aporte na empresa |
| Estorno ou reembolso | **Estorno**: abate da fatura/conta e das despesas da categoria sem inflar receitas |
| Importar extrato do banco/cartão | **Lançamentos → ▾ → Importar extrato (OFX/CSV)** com deduplicação automática |
| Fluxo futuro / Rateio / Metas | Menu **Fluxo** e **Relatórios** (*Rateio entre titulares*, *Metas e reservas*, *Fechamento mensal*) |
| Abastecimento e manutenção | **Veículo → Abastecer / Manutenção** |

**Escopo PF × PJ.** Quando houver empresa cadastrada, alterne no **Painel**, no **Fluxo** e nos **Relatórios** entre *Consolidado*, *Pessoal (PF)* e *Empresa (PJ)*.

**Exportar.** Em **Lançamentos → ▾ → Exportar CSV** (protegido contra fórmulas maliciosas em planilhas).

---

## 4. Backup, restauração e cron

```bash
./scripts/backup.sh                                    # cria backups/financas_AAAA-MM-DD_HHMM.sql.gz (ou .age)
./scripts/restaurar.sh backups/financas_....sql.gz     # valida, para o web, restaura em transação única e reinicia
```

Para criptografar backups automaticamente antes de enviar à nuvem, defina `BACKUP_AGE_RECIPIENT=<chave_publica_age>` no `.env` com o utilitário `age` instalado.

Agende no cron do servidor (diariamente):
```
0 3 * * * cd /caminho/para/financas && ./scripts/backup.sh >> backups/backup.log 2>&1
5 0 * * * docker compose -f /caminho/para/financas/docker-compose.yml exec -T web python manage.py gerar_recorrencias
0 8 * * * docker compose -f /caminho/para/financas/docker-compose.yml exec -T web python manage.py enviar_alertas
* * * * * cd /caminho/para/financas && ./scripts/atualizar.sh --daemon >> data/atualizacao/cron.log 2>&1
```
O último agendamento permite que o painel admin (`Painel Admin › Atualizações`) dispare `git pull --ff-only` e `docker compose up -d --build` no host com segurança, sem expor o socket do Docker para dentro do container (rode `./scripts/atualizar.sh --sync` uma vez na primeira instalação).
O script de backup mantém os 30 arquivos válidos mais recentes com permissão restrita (`umask 077`).

---

## 5. Manutenção e desenvolvimento local

```bash
docker compose logs -f web
docker compose exec web python manage.py recalcular_faturas
docker compose exec web python manage.py enviar_alertas --dry-run
```

Sem `DATABASE_URL` nem `POSTGRES_HOST`, usa SQLite local em `data/db.sqlite3` (ou no caminho definido em `SQLITE_PATH`):

```bash
pip install -r requirements.txt
DEBUG=1 python manage.py migrate
DEBUG=1 python manage.py seed_inicial
DEBUG=1 python manage.py createsuperuser
DEBUG=1 python manage.py runserver
DEBUG=1 python manage.py test financeiro
```

---

## 6. Estrutura

```
config/                  configurações do Django (lê tudo do .env)
financeiro/
  models.py              domínio financeiro, constraints, auditoria, integrações, metas, fechamento
  services.py            saldos, faturas, parcelas, recorrências, fluxo projetado, rateio, metas
  audit.py               registro de auditoria e diff de alterações
  security.py            rate-limit de login, tokens API, validação de comprovantes, CSV seguro
  importacao.py          parsers OFX e CSV com deduplicação e regras automáticas
  notificacoes.py        alertas e envio pelo Telegram
  views/                 views separadas por domínio (painel, lançamentos, contas, cartões, admin, api...)
  forms.py               formulários e validações
  tests.py               suíte automatizada
  management/commands/   seed_inicial, gerar_recorrencias, enviar_alertas, recalcular_faturas
  static/vendor/         Bootstrap, ícones, Chart.js e fonte (sem CDN)
templates/               HTML das telas e Painel Admin
docker/                  entrypoint e Caddyfile com cabeçalhos de segurança
scripts/                 backup, restauração e agente de atualização via Git
```
