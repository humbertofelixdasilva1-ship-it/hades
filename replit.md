# Hades Football Simulator

App PT-BR de simulação de campeonatos de futebol (Flask + página única HTML).
Projeto pessoal do usuário (@slvthereal no Instagram).

## Arquitetura

- **Backend**: Flask + psycopg2 (SQL direto, sem ORM). Workflow: `python app.py`.
- **Frontend**: Single-page HTML em `templates/index.html` com CSS+JS embutidos.
- **Banco**: PostgreSQL (DATABASE_URL). Tabelas:
  - `camps` (PK VARCHAR(8)) — campeonatos. Estado JSONB inclui `betting_open`.
  - `global_teams` (PK VARCHAR(8)) — banco global de times.
  - `bettors` (PK VARCHAR(8)) — apostadores, com saldo `hcoins`.
  - `bets` (PK SERIAL) — apostas (pendente/ganha/perdida).
- **Estado das tabelas existentes preservado**: nunca fazer ALTER em PKs.
- **AI**: Groq (chave em `GROQ_API_KEY`) usada para gerar fofocas/notícias dos campeonatos.
- **Sessão**: Flask sessions com `SESSION_SECRET` (fallback embutido). `werkzeug` para hash de senhas.

## Dois painéis

1. **Painel Admin** (login `sclbypython`/`sclontopbit`):
   - Cria campeonatos (vários formatos: grupos, mata-mata, liga, mistos)
   - Adiciona times e jogadores, simula partidas, avança fases
   - Gera notícias com IA
   - **Botão por campeonato**: "Disponibilizar/Parar Apostas" (só após início do campeonato)
2. **Painel Apostador** (cadastro livre, recebe 1000 hcoins iniciais):
   - Lista apenas campeonatos com apostas abertas
   - Aposta nas partidas pendentes — odds determinísticas (1X2, OU 2.5, BTTS) com 7% de margem
   - Ao simular partida (admin), apostas pendentes são liquidadas automaticamente
   - Ranking público dos top 20 apostadores

## Fluxo de telas

1. `chooseScreen` → escolhe entre Admin ou Apostador
2. Admin → `loginScreen` → painel completo
3. Apostador → `bettorAuthScreen` (Entrar/Criar conta) → `bettorApp`

## Endpoints principais

- Admin: `/api/admin/{login,logout,me}`, `/api/data`, `/api/camps/...`, `/api/camps/<id>/betting/<open|close>`
- Apostador: `/api/bettor/{register,login,logout,me,camps,camps/<id>,bets,ranking}`

## Conta de teste já no banco

- Apostador: `joao` / `1234` (id `51951434`)

## Notas importantes

- Aposta mínima: 10 hcoins.
- Ao simular uma partida, o backend chama `settle_bets_for_match` automaticamente.
- Backgrounds dinâmicos: `data-bg="1..4"` no body, imagens em `static/img/bg{1..4}.jpg`.
- **Mecânicas opcionais por campeonato** (`config.mechanics`): `scorers`, `assists`, `cards`, `injuries`, `ratings`, `motm`. Definidas via checkboxes na criação do campeonato. Default: todas `True` (compatível com camps antigos via `get_mechanics()`). Quando desativadas, `simulate_match` não gera os dados correspondentes e o modal de detalhes esconde a seção.

## Painel de Jogador + HSocial

Tabelas novas: `players` (id, username, password_hash, player_name, team_name) e `hsocial_posts` (id serial, player_id FK, text, image base64, created_at).

Rotas:
- `/api/player/register|login|logout|me|update` — auth do jogador (sessão `player_id`).
- `/api/player/performance` — escaneia todos os campeonatos por nome+time do jogador, devolve totais (gols, assist, cartões, lesões, MOTM, nota média) e quebra por torneio + lista de campeonatos vencidos. Usa o helper `compute_camp_winner(camp)` que detecta vencedor pela final do mata-mata, top da liga, ou top do grupo A em "groups_only".
- `/api/hsocial/posts` GET (público), POST (player_required), DELETE (player_required, dono). Imagem em base64 data URL, limite ~4MB.

Frontend:
- 3º botão "Painel de Jogador" no chooseScreen, e botão "Ver HSocial (público)" para qualquer um ver o feed em modo leitura.
- Painel de jogador com 2 abas: **Desempenho** (cards de stats agregadas + por torneio) e **HSocial** (textarea + upload de imagem com preview, feed com botão de apagar nos próprios posts).

## Elenco Geral (admin)

- Times globais agora são gerenciados em uma aba dedicada **"Elenco Geral"** (top-level no admin nav). A criação de novo time foi removida da seção de cadastro do campeonato; lá agora só se escolhe times do elenco.
- Endpoints novos (admin): `PUT /api/global_teams/<id>` (renomeia + edita nomes dos jogadores), `DELETE /api/global_teams/<id>` (bloqueia se time estiver em algum camp), `POST /api/global_teams/transfer` (move/troca jogador entre dois times globais — se a posição de destino tiver alguém, faz swap).
- A transferência opera no elenco global; campeonatos já criados continuam com a foto antiga dos times (snapshot deep-copy ao adicionar ao camp).
