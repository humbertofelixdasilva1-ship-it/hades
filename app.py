from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import json
import random
import os
import uuid
import hashlib
import requests
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "hades-football-default-secret-2026-change-me")
app.config['MAX_CONTENT_LENGTH'] = 6 * 1024 * 1024  # 6 MB pra suportar imagem em base64
DATA_FILE = 'data.json'
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_USER = "sclbypython"
ADMIN_PASS = "sclontopbit"
INITIAL_HCOINS = 1000

POSITIONS = ["GK", "LE", "ZAG", "ZAG", "LD", "MLE", "MAT", "MLD", "PTE", "CA", "PTD"]

# Configurações de formato de campeonato
FORMATS = ['groups_only', 'groups_ko', 'ko_only', 'league', 'league_ko']
KO_STAGE_BY_SIZE = {2: 'Final', 4: 'SF', 8: 'QF', 16: 'R16', 32: 'R32'}
KO_ORDER = ['R32', 'R16', 'QF', 'SF', 'Final']
DEFAULT_CONFIG = {
    "num_teams": 16,
    "num_groups": 4,
    "teams_per_group": 4,
    "advance_per_group": 2,
    "ko_size": 8,
    "ko_two_legs": True,
    "league_two_legs": True
}
DEFAULT_MECHANICS = {
    "scorers": True,
    "assists": True,
    "cards": True,
    "injuries": True,
    "ratings": True,
    "motm": True,
}
MECHANIC_KEYS = list(DEFAULT_MECHANICS.keys())
DEFAULT_FORMAT = 'groups_ko'


def get_mechanics(camp):
    cfg_mech = (camp.get('config') or {}).get('mechanics') or {}
    return {k: bool(cfg_mech.get(k, True)) for k in MECHANIC_KEYS}


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global_teams (
                    id VARCHAR(8) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    players JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS camps (
                    id VARCHAR(8) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    phase VARCHAR(50) NOT NULL DEFAULT 'registration',
                    state JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS bettors (
                    id VARCHAR(8) PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    hcoins INTEGER NOT NULL DEFAULT 1000,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    bettor_id VARCHAR(8) NOT NULL REFERENCES bettors(id) ON DELETE CASCADE,
                    camp_id VARCHAR(8) NOT NULL,
                    match_id INTEGER NOT NULL,
                    bet_type VARCHAR(20) NOT NULL,
                    selection VARCHAR(20) NOT NULL,
                    amount INTEGER NOT NULL,
                    odd NUMERIC(6,2) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    payout INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    settled_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_bets_bettor ON bets(bettor_id);
                CREATE INDEX IF NOT EXISTS idx_bets_camp_match ON bets(camp_id, match_id);
                CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
                CREATE TABLE IF NOT EXISTS players (
                    id VARCHAR(8) PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    player_name VARCHAR(100) NOT NULL,
                    team_name VARCHAR(100) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS hsocial_posts (
                    id SERIAL PRIMARY KEY,
                    player_id VARCHAR(8) NOT NULL REFERENCES players(id) ON DELETE CASCADE,
                    text TEXT,
                    image TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_posts_created ON hsocial_posts(created_at DESC);
            """)
        conn.commit()
    migrate_from_json()


def migrate_from_json():
    if not os.path.exists(DATA_FILE):
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM global_teams")
            teams_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM camps")
            camps_count = cur.fetchone()[0]
            if teams_count > 0 or camps_count > 0:
                return
            try:
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
            except Exception:
                return
            for t in data.get('global_teams', []):
                cur.execute(
                    "INSERT INTO global_teams (id, name, players) VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    (t['id'], t['name'], Json(t.get('players', [])))
                )
            for cid, camp in data.get('camps', {}).items():
                phase = camp.get('phase', 'registration')
                state = {
                    'format': camp.get('format', DEFAULT_FORMAT),
                    'config': camp.get('config', dict(DEFAULT_CONFIG)),
                    'teams': camp.get('teams', []),
                    'groups': camp.get('groups', {"A": [], "B": [], "C": [], "D": []}),
                    'matches': camp.get('matches', []),
                    'stats': camp.get('stats', {}),
                    'news': camp.get('news', []),
                    'suspensions': camp.get('suspensions', {}),
                    'injuries': camp.get('injuries', {})
                }
                cur.execute(
                    "INSERT INTO camps (id, name, phase, state) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    (camp['id'], camp['name'], phase, Json(state))
                )
        conn.commit()
    try:
        os.rename(DATA_FILE, DATA_FILE + '.migrated')
    except Exception:
        pass


def load_data():
    data = {"global_teams": [], "camps": {}}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name, players FROM global_teams ORDER BY created_at")
            for row in cur.fetchall():
                data['global_teams'].append({
                    "id": row['id'],
                    "name": row['name'],
                    "players": row['players']
                })
            cur.execute("SELECT id, name, phase, state FROM camps ORDER BY created_at")
            for row in cur.fetchall():
                state = row['state'] or {}
                data['camps'][row['id']] = {
                    "id": row['id'],
                    "name": row['name'],
                    "phase": row['phase'],
                    "format": state.get('format', DEFAULT_FORMAT),
                    "config": state.get('config', dict(DEFAULT_CONFIG)),
                    "teams": state.get('teams', []),
                    "groups": state.get('groups', {}),
                    "matches": state.get('matches', []),
                    "stats": state.get('stats', {}),
                    "news": state.get('news', []),
                    "suspensions": state.get('suspensions', {}),
                    "injuries": state.get('injuries', {}),
                    "betting_open": state.get('betting_open', False)
                }
    return data


def save_data(data):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM global_teams")
            existing_team_ids = {r[0] for r in cur.fetchall()}
            new_team_ids = {t['id'] for t in data['global_teams']}
            for t in data['global_teams']:
                cur.execute("""
                    INSERT INTO global_teams (id, name, players) VALUES (%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, players=EXCLUDED.players
                """, (t['id'], t['name'], Json(t.get('players', []))))
            for tid in existing_team_ids - new_team_ids:
                cur.execute("DELETE FROM global_teams WHERE id=%s", (tid,))

            cur.execute("SELECT id FROM camps")
            existing_camp_ids = {r[0] for r in cur.fetchall()}
            new_camp_ids = set(data['camps'].keys())
            for cid, camp in data['camps'].items():
                state = {
                    'format': camp.get('format', DEFAULT_FORMAT),
                    'config': camp.get('config', dict(DEFAULT_CONFIG)),
                    'teams': camp.get('teams', []),
                    'groups': camp.get('groups', {}),
                    'matches': camp.get('matches', []),
                    'stats': camp.get('stats', {}),
                    'news': camp.get('news', []),
                    'suspensions': camp.get('suspensions', {}),
                    'injuries': camp.get('injuries', {}),
                    'betting_open': camp.get('betting_open', False)
                }
                cur.execute("""
                    INSERT INTO camps (id, name, phase, state, updated_at) VALUES (%s,%s,%s,%s, NOW())
                    ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, phase=EXCLUDED.phase, state=EXCLUDED.state, updated_at=NOW()
                """, (cid, camp['name'], camp.get('phase', 'registration'), Json(state)))
            for cid in existing_camp_ids - new_camp_ids:
                cur.execute("DELETE FROM camps WHERE id=%s", (cid,))
        conn.commit()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/data', methods=['GET'])
def get_data():
    return jsonify(load_data())


# ------ ROTAS DE CAMPEONATOS ------

@app.route('/api/camps', methods=['POST'])
def create_camp():
    data = load_data()
    req = request.json
    camp_name = req.get('name', 'Novo Campeonato')
    fmt = req.get('format', DEFAULT_FORMAT)
    if fmt not in FORMATS:
        return jsonify({"error": f"Formato inválido. Use um destes: {', '.join(FORMATS)}"}), 400

    cfg = dict(DEFAULT_CONFIG)
    user_cfg = req.get('config', {}) or {}
    for k in ['num_teams', 'num_groups', 'teams_per_group', 'advance_per_group', 'ko_size']:
        if k in user_cfg:
            try:
                cfg[k] = int(user_cfg[k])
            except (TypeError, ValueError):
                return jsonify({"error": f"Valor inválido para {k}"}), 400
    for k in ['ko_two_legs', 'league_two_legs']:
        if k in user_cfg:
            cfg[k] = bool(user_cfg[k])

    user_mech = req.get('mechanics') or {}
    cfg['mechanics'] = {k: bool(user_mech.get(k, DEFAULT_MECHANICS[k])) for k in MECHANIC_KEYS}

    # Validações por formato
    if fmt in ('groups_only', 'groups_ko'):
        ng = cfg['num_groups']
        tpg = cfg['teams_per_group']
        if ng < 1 or tpg < 2:
            return jsonify({"error": "Configuração de grupos inválida."}), 400
        # Se num_teams não foi enviado explicitamente, derive de ng*tpg
        if 'num_teams' not in user_cfg:
            cfg['num_teams'] = ng * tpg
        if ng * tpg != cfg['num_teams']:
            return jsonify({"error": f"num_groups × teams_per_group ({ng}×{tpg}) deve ser igual a num_teams ({cfg['num_teams']})."}), 400

    n = cfg['num_teams']
    if n < 2:
        return jsonify({"error": "Número de times precisa ser pelo menos 2."}), 400

    if fmt == 'groups_ko':
        apg = cfg['advance_per_group']
        if apg < 1 or apg >= tpg:
            return jsonify({"error": "advance_per_group deve ser >=1 e < teams_per_group."}), 400
        ko_size = ng * apg
        if ko_size not in KO_STAGE_BY_SIZE:
            return jsonify({"error": f"Total de classificados ({ko_size}) precisa ser potência de 2 (2,4,8,16,32)."}), 400
        cfg['ko_size'] = ko_size

    if fmt == 'ko_only':
        if n not in KO_STAGE_BY_SIZE:
            return jsonify({"error": f"Para mata-mata puro, num_teams precisa ser potência de 2 (2,4,8,16,32). Recebido: {n}"}), 400
        cfg['ko_size'] = n

    if fmt == 'league_ko':
        ks = cfg['ko_size']
        if ks not in KO_STAGE_BY_SIZE or ks > n:
            return jsonify({"error": f"ko_size precisa ser potência de 2 (2,4,8,16,32) e <= num_teams."}), 400

    c_id = str(uuid.uuid4())[:8]
    data['camps'][c_id] = {
        "id": c_id,
        "name": camp_name,
        "phase": "registration",
        "format": fmt,
        "config": cfg,
        "teams": [],
        "groups": {},
        "matches": [],
        "stats": {},
        "news": [],
        "suspensions": {},
        "injuries": {}
    }
    save_data(data)
    return jsonify({"message": "Campeonato criado!", "id": c_id})


@app.route('/api/camps/<c_id>', methods=['DELETE'])
def delete_camp(c_id):
    data = load_data()
    if c_id in data['camps']:
        del data['camps'][c_id]
        save_data(data)
        return jsonify({"message": "Campeonato apagado com sucesso!"})
    return jsonify({"error": "Campeonato não encontrado"}), 404


# ------ ROTAS DE TIMES E JOGADORES ------

@app.route('/api/global_teams', methods=['POST'])
def add_global_team():
    data = load_data()
    req = request.json
    team_name = req.get('name')
    players = req.get('players')

    if not team_name:
        return jsonify({"error": "O nome do time é obrigatório."}), 400

    team_id = str(uuid.uuid4())[:8]
    new_team = {"id": team_id, "name": team_name, "players": players}
    data['global_teams'].append(new_team)
    save_data(data)
    return jsonify({"message": "Time salvo no banco de dados!", "team": new_team})


@app.route('/api/camps/<c_id>/add_team', methods=['POST'])
def add_team_to_camp(c_id):
    data = load_data()
    if c_id not in data['camps']:
        return jsonify({"error": "Camp não existe"}), 404

    camp = data['camps'][c_id]
    max_teams = camp.get('config', DEFAULT_CONFIG).get('num_teams', 16)
    if len(camp['teams']) >= max_teams:
        return jsonify({"error": f"Limite de {max_teams} times atingido!"}), 400

    req = request.json
    team_id = req.get('team_id')

    team = next((t for t in data['global_teams'] if t['id'] == team_id), None)
    if not team:
        return jsonify({"error": "Time não encontrado."}), 404
    if any(t['id'] == team_id for t in camp['teams']):
        return jsonify({"error": "Time já adicionado."}), 400

    team_copy = json.loads(json.dumps(team))
    camp['teams'].append(team_copy)

    for i, p in enumerate(team_copy['players']):
        stat_id = f"{team_copy['id']}_{i}"
        camp['stats'][stat_id] = {
            "name": p['name'], "team": team_copy['name'], "pos": p['pos'],
            "goals": 0, "assists": 0, "yellows": 0, "reds": 0, "injuries": 0
        }

    save_data(data)
    return jsonify({"message": "Time adicionado ao campeonato!"})


@app.route('/api/camps/<c_id>/teams/<t_id>/players', methods=['PUT'])
def update_players(c_id, t_id):
    data = load_data()
    camp = data['camps'].get(c_id)
    if not camp:
        return jsonify({"error": "Camp não existe"}), 404

    req = request.json
    new_players = req.get('players')

    team = next((t for t in camp['teams'] if t['id'] == t_id), None)
    if team:
        team['players'] = new_players
        for i, p in enumerate(new_players):
            stat_id = f"{t_id}_{i}"
            if stat_id in camp['stats']:
                camp['stats'][stat_id]['name'] = p['name']
            else:
                camp['stats'][stat_id] = {
                    "name": p['name'], "team": team['name'], "pos": p['pos'],
                    "goals": 0, "assists": 0, "yellows": 0, "reds": 0, "injuries": 0
                }
        save_data(data)
        return jsonify({"message": "Elenco atualizado com sucesso!"})
    return jsonify({"error": "Time não encontrado"}), 404


# ------ HELPERS DE GERAÇÃO DE JOGOS ------

def _group_letter(i):
    return chr(ord('A') + i)


def generate_group_matches(camp):
    cfg = camp['config']
    teams = camp['teams'].copy()
    random.shuffle(teams)
    ng = cfg['num_groups']
    tpg = cfg['teams_per_group']
    groups = {}
    for i in range(ng):
        letter = _group_letter(i)
        groups[letter] = teams[i*tpg:(i+1)*tpg]
    camp['groups'] = groups
    matches = []
    m_id = 1
    for g_name, g_teams in groups.items():
        for i in range(len(g_teams)):
            for j in range(len(g_teams)):
                if i != j:
                    matches.append({"id": m_id, "stage": f"Grupo {g_name}", "group": g_name,
                                    "home": g_teams[i], "away": g_teams[j], "played": False, "hg": 0, "ag": 0})
                    m_id += 1
    return matches


def generate_league_matches(teams_list, two_legs):
    matches = []
    m_id = 1
    n = len(teams_list)
    if two_legs:
        for i in range(n):
            for j in range(n):
                if i != j:
                    matches.append({"id": m_id, "stage": "Liga",
                                    "home": teams_list[i], "away": teams_list[j],
                                    "played": False, "hg": 0, "ag": 0})
                    m_id += 1
    else:
        for i in range(n):
            for j in range(i+1, n):
                matches.append({"id": m_id, "stage": "Liga",
                                "home": teams_list[i], "away": teams_list[j],
                                "played": False, "hg": 0, "ag": 0})
                m_id += 1
    return matches


def create_ko_round(camp, qualified, stage, two_legs):
    matches = camp['matches']
    m_id = max([m['id'] for m in matches], default=0) + 1
    new_ms = []
    actual_two_legs = two_legs and stage != 'Final'
    for i in range(0, len(qualified), 2):
        if i+1 >= len(qualified):
            break
        h, a = qualified[i], qualified[i+1]
        if actual_two_legs:
            new_ms.append({"id": m_id, "stage": f"{stage}_Ida",
                           "home": h, "away": a, "played": False, "hg": 0, "ag": 0}); m_id += 1
            new_ms.append({"id": m_id, "stage": f"{stage}_Volta",
                           "home": a, "away": h, "played": False, "hg": 0, "ag": 0}); m_id += 1
        else:
            new_ms.append({"id": m_id, "stage": stage,
                           "home": h, "away": a, "played": False, "hg": 0, "ag": 0}); m_id += 1
    return new_ms


def get_qualified_from_groups(camp, advance_per_group):
    qualified_by_pos = {}  # pos -> list of teams
    for g_name, g_teams in camp['groups'].items():
        standings = {t['id']: {'t': t, 'pts': 0, 'sg': 0, 'gp': 0} for t in g_teams}
        for m in camp['matches']:
            if m.get('group') == g_name and m.get('played'):
                h, a, hg, ag = m['home']['id'], m['away']['id'], m['hg'], m['ag']
                standings[h]['gp'] += hg
                standings[a]['gp'] += ag
                standings[h]['sg'] += (hg - ag)
                standings[a]['sg'] += (ag - hg)
                if hg > ag: standings[h]['pts'] += 3
                elif ag > hg: standings[a]['pts'] += 3
                else:
                    standings[h]['pts'] += 1
                    standings[a]['pts'] += 1
        ordered = sorted(standings.values(), key=lambda x: (x['pts'], x['sg'], x['gp']), reverse=True)
        for i in range(min(advance_per_group, len(ordered))):
            qualified_by_pos.setdefault(i+1, []).append({'team': ordered[i]['t'], 'group': g_name})
    # Cross-pair: 1st place from one group vs 2nd place from another
    result = []
    for pos in sorted(qualified_by_pos.keys()):
        random.shuffle(qualified_by_pos[pos])
    if len(qualified_by_pos) >= 2:
        # Interleave 1s and 2s alternating
        firsts = qualified_by_pos.get(1, [])
        rest = []
        for pos in sorted(qualified_by_pos.keys()):
            if pos == 1: continue
            rest.extend(qualified_by_pos[pos])
        for i, f in enumerate(firsts):
            result.append(f['team'])
            if i < len(rest):
                # try to find one not from same group
                idx = next((k for k, r in enumerate(rest) if r['group'] != f['group']), 0)
                result.append(rest.pop(idx)['team'])
        for r in rest:
            result.append(r['team'])
    else:
        for pos in sorted(qualified_by_pos.keys()):
            for q in qualified_by_pos[pos]:
                result.append(q['team'])
    return result


def get_league_top(camp, n):
    standings = {t['id']: {'t': t, 'pts': 0, 'sg': 0, 'gp': 0} for t in camp['teams']}
    for m in camp['matches']:
        if m.get('stage') == 'Liga' and m.get('played'):
            h, a, hg, ag = m['home']['id'], m['away']['id'], m['hg'], m['ag']
            standings[h]['gp'] += hg; standings[a]['gp'] += ag
            standings[h]['sg'] += (hg-ag); standings[a]['sg'] += (ag-hg)
            if hg > ag: standings[h]['pts'] += 3
            elif ag > hg: standings[a]['pts'] += 3
            else:
                standings[h]['pts'] += 1
                standings[a]['pts'] += 1
    ordered = sorted(standings.values(), key=lambda x: (x['pts'], x['sg'], x['gp']), reverse=True)
    return [o['t'] for o in ordered[:n]]


def get_round_winners(matches, stage_prefix, two_legs):
    winners = []
    if two_legs:
        voltas = sorted([m for m in matches if m['stage'] == f'{stage_prefix}_Volta'], key=lambda m: m['id'])
        for v in voltas:
            ida = next((m for m in matches if m['stage'] == f'{stage_prefix}_Ida'
                        and m['home']['id'] == v['away']['id']
                        and m['away']['id'] == v['home']['id']), None)
            if not ida:
                continue
            agg_h = v['hg'] + ida['ag']
            agg_a = v['ag'] + ida['hg']
            if agg_h > agg_a:
                winners.append(v['home'])
            elif agg_a > agg_h:
                winners.append(v['away'])
            else:
                if v['home']['name'] in v.get('penalties', ''):
                    winners.append(v['home'])
                else:
                    winners.append(v['away'])
    else:
        round_matches = sorted([m for m in matches if m['stage'] == stage_prefix], key=lambda m: m['id'])
        for m in round_matches:
            if m['hg'] > m['ag']:
                winners.append(m['home'])
            elif m['ag'] > m['hg']:
                winners.append(m['away'])
            else:
                if m['home']['name'] in m.get('penalties', ''):
                    winners.append(m['home'])
                else:
                    winners.append(m['away'])
    return winners


def matches_in_phase(m, phase):
    s = m.get('stage', '')
    if phase == 'groups':
        return s.startswith('Grupo')
    if phase == 'league':
        return s == 'Liga'
    if phase in ('r32', 'r16', 'qf', 'sf'):
        prefix = phase.upper()
        return s == prefix or s == f'{prefix}_Ida' or s == f'{prefix}_Volta'
    if phase == 'final':
        return s == 'Final'
    return False


# ------ ROTAS DE SIMULAÇÃO ------

@app.route('/api/camps/<c_id>/start', methods=['POST'])
def start_camp(c_id):
    data = load_data()
    if c_id not in data['camps']:
        return jsonify({"error": "Camp não existe"}), 404
    camp = data['camps'][c_id]
    cfg = camp.get('config', DEFAULT_CONFIG)
    fmt = camp.get('format', DEFAULT_FORMAT)

    if len(camp['teams']) != cfg['num_teams']:
        return jsonify({"error": f"Faltam times! ({len(camp['teams'])}/{cfg['num_teams']})"}), 400
    if camp['phase'] != 'registration':
        return jsonify({"error": "Campeonato já foi iniciado!"}), 400

    if fmt in ('groups_only', 'groups_ko'):
        camp['matches'] = generate_group_matches(camp)
        camp['phase'] = 'groups'
        msg = "Fase de Grupos iniciada!"
    elif fmt in ('league', 'league_ko'):
        teams = camp['teams'].copy()
        random.shuffle(teams)
        camp['matches'] = generate_league_matches(teams, cfg.get('league_two_legs', True))
        camp['groups'] = {}
        camp['phase'] = 'league'
        msg = "Liga iniciada!"
    elif fmt == 'ko_only':
        teams = camp['teams'].copy()
        random.shuffle(teams)
        stage = KO_STAGE_BY_SIZE[cfg['ko_size']]
        camp['matches'] = []
        camp['matches'] = create_ko_round(camp, teams, stage, cfg.get('ko_two_legs', True))
        camp['groups'] = {}
        camp['phase'] = stage.lower()
        msg = "Mata-mata iniciado!"
    else:
        return jsonify({"error": "Formato desconhecido"}), 400

    save_data(data)
    return jsonify({"message": msg})


# Alias retrocompatível
@app.route('/api/start_groups/<c_id>', methods=['POST'])
def start_groups(c_id):
    return start_camp(c_id)


def random_minute():
    half = random.choice([1, 2])
    if half == 1:
        n = random.randint(1, 45)
        return f"{n}'" if n < 45 else f"45+{random.randint(1,5)}'"
    n = random.randint(46, 90)
    return f"{n}'" if n < 90 else f"90+{random.randint(1,6)}'"


@app.route('/api/simulate/<c_id>/<int:m_id>', methods=['POST'])
def simulate_match(c_id, m_id):
    data = load_data()
    camp = data['camps'][c_id]
    match = next((m for m in camp['matches'] if m['id'] == m_id), None)
    if not match or match.get('played'):
        return jsonify({"error": "Jogo inválido"}), 400

    hg = random.choices([0, 1, 2, 3, 4, 5], weights=[20, 25, 25, 15, 10, 5])[0]
    ag = random.choices([0, 1, 2, 3, 4, 5], weights=[25, 25, 20, 15, 10, 5])[0]

    mech = get_mechanics(camp)

    def get_scorers(team, goals):
        events = []
        if not mech['scorers']:
            return events
        t_players = team['players']
        for _ in range(goals):
            roll = random.random()
            if roll < 0.10:
                goal_type = 'penalty'
            elif roll < 0.18:
                goal_type = 'freekick'
            else:
                goal_type = 'open'

            if goal_type == 'penalty':
                weights_g = [0 if p['pos'] == 'GK' else 1 if p['pos'] in ['ZAG', 'LE', 'LD'] else 2 if p['pos'] in ['MLE', 'MLD'] else 5 if p['pos'] == 'MAT' else 10 for p in t_players]
            elif goal_type == 'freekick':
                weights_g = [0 if p['pos'] == 'GK' else 1 if p['pos'] in ['ZAG'] else 4 if p['pos'] in ['LE', 'LD', 'MLE', 'MLD'] else 8 if p['pos'] in ['MAT', 'PTE', 'PTD'] else 6 for p in t_players]
            else:
                weights_g = [0 if p['pos'] == 'GK' else 1 if p['pos'] in ['ZAG', 'LE', 'LD'] else 3 if p['pos'] in ['MLE', 'MLD'] else 6 if p['pos'] in ['MAT', 'PTE', 'PTD'] else 10 for p in t_players]

            s_idx = random.choices(range(11), weights=weights_g)[0]
            scorer = t_players[s_idx]

            assist = None
            a_idx = None
            if goal_type == 'open' and mech['assists']:
                possible_a = [i for i in range(11) if i != s_idx and t_players[i]['name']]
                if possible_a:
                    weights_a = [1 if t_players[i]['pos'] == 'GK' else 2 if t_players[i]['pos'] in ['ZAG', 'CA'] else 5 if t_players[i]['pos'] in ['LE', 'LD'] else 8 for i in possible_a]
                    a_idx = random.choices(possible_a, weights=weights_a)[0]
                    assist = t_players[a_idx]

            events.append({
                "player": scorer['name'] if scorer['name'] else "Jogador Não Cadastrado",
                "assist": assist['name'] if assist and assist['name'] else None,
                "minute": random_minute(),
                "type": goal_type
            })

            if scorer['name']:
                camp['stats'][f"{team['id']}_{s_idx}"]['goals'] += 1
            if assist and assist['name']:
                camp['stats'][f"{team['id']}_{a_idx}"]['assists'] += 1

        return events

    def get_cards(team):
        cards = []
        if not mech['cards']:
            return cards
        n_yellow = random.choices([0, 1, 2, 3, 4], weights=[15, 30, 30, 18, 7])[0]
        n_red = random.choices([0, 1], weights=[92, 8])[0]
        t_players = [p for p in team['players'] if p['name']]
        if not t_players:
            return cards
        weights_card = [1 if p['pos'] == 'GK' else 4 if p['pos'] in ['ZAG', 'LE', 'LD'] else 5 if p['pos'] in ['MLE', 'MLD', 'MAT'] else 3 for p in t_players]
        used = set()
        for _ in range(n_yellow):
            idx = random.choices(range(len(t_players)), weights=weights_card)[0]
            p = t_players[idx]
            cards.append({"player": p['name'], "type": "yellow", "minute": random_minute()})
            used.add(p['name'])
            for i, sp in enumerate(team['players']):
                if sp['name'] == p['name']:
                    sid = f"{team['id']}_{i}"
                    if sid in camp['stats']:
                        camp['stats'][sid]['yellows'] = camp['stats'][sid].get('yellows', 0) + 1
                    break
        for _ in range(n_red):
            idx = random.choices(range(len(t_players)), weights=weights_card)[0]
            p = t_players[idx]
            cards.append({"player": p['name'], "type": "red", "minute": random_minute()})
            for i, sp in enumerate(team['players']):
                if sp['name'] == p['name']:
                    sid = f"{team['id']}_{i}"
                    if sid in camp['stats']:
                        camp['stats'][sid]['reds'] = camp['stats'][sid].get('reds', 0) + 1
                    break
        cards.sort(key=lambda c: int(c['minute'].split('+')[0].rstrip("'")))
        return cards

    def get_injuries(team):
        injuries = []
        if not mech['injuries']:
            return injuries
        if random.random() < 0.18:
            t_players = [p for p in team['players'] if p['name']]
            if t_players:
                p = random.choice(t_players)
                duration = random.choices([1, 2, 3, 4], weights=[40, 30, 20, 10])[0]
                injuries.append({
                    "player": p['name'],
                    "minute": random_minute(),
                    "duration": duration
                })
                for i, sp in enumerate(team['players']):
                    if sp['name'] == p['name']:
                        sid = f"{team['id']}_{i}"
                        if sid in camp['stats']:
                            camp['stats'][sid]['injuries'] = camp['stats'][sid].get('injuries', 0) + 1
                        break
        return injuries

    events_h = get_scorers(match['home'], hg)
    events_a = get_scorers(match['away'], ag)
    cards_h = get_cards(match['home'])
    cards_a = get_cards(match['away'])
    injuries_h = get_injuries(match['home'])
    injuries_a = get_injuries(match['away'])

    def generate_ratings(team, events, cards, injuries):
        ratings = {}
        if not mech['ratings']:
            return ratings
        for p in team['players']:
            if p['name']:
                ratings[p['name']] = {"pos": p['pos'], "val": random.uniform(5.0, 7.0)}

        for ev in events:
            if ev['player'] in ratings:
                ratings[ev['player']]['val'] += 1.5
            if ev['assist'] in ratings:
                ratings[ev['assist']]['val'] += 1.0

        for c in cards:
            if c['player'] in ratings:
                ratings[c['player']]['val'] -= (0.5 if c['type'] == 'yellow' else 2.0)

        for inj in injuries:
            if inj['player'] in ratings:
                ratings[inj['player']]['val'] -= 0.3

        for r in ratings.values():
            r['val'] = max(3.0, min(10.0, round(r['val'], 1)))

        return dict(sorted(ratings.items(), key=lambda item: item[1]['val'], reverse=True))

    ratings_h = generate_ratings(match['home'], events_h, cards_h, injuries_h)
    ratings_a = generate_ratings(match['away'], events_a, cards_a, injuries_a)

    match.update({
        "played": True,
        "hg": hg, "ag": ag,
        "events_h": events_h, "events_a": events_a,
        "cards_h": cards_h, "cards_a": cards_a,
        "injuries_h": injuries_h, "injuries_a": injuries_a,
        "ratings_h": ratings_h, "ratings_a": ratings_a
    })

    stage = match['stage']
    needs_pk = False
    if stage == 'Final':
        needs_pk = (hg == ag)
    elif stage.endswith('_Volta'):
        prefix = stage.replace('_Volta', '')
        ida = next((m for m in camp['matches'] if m['stage'] == f'{prefix}_Ida'
                    and m['home']['id'] == match['away']['id']
                    and m['away']['id'] == match['home']['id']), None)
        if ida:
            agg_h = hg + ida['ag']
            agg_a = ag + ida['hg']
            needs_pk = (agg_h == agg_a)
    elif stage in KO_ORDER:  # KO single-leg: R32, R16, QF, SF (Final tratado acima)
        needs_pk = (hg == ag)

    if needs_pk:
        match['penalties'] = f"{match['home']['name']} venceu nos pênaltis" if random.random() > 0.5 else f"{match['away']['name']} venceu nos pênaltis"

    save_data(data)
    # Liquida apostas pendentes desta partida (apenas se houve apostas)
    try:
        settle_bets_for_match(c_id, m_id, hg, ag)
    except Exception as e:
        print(f"[bets] erro liquidando apostas do jogo {m_id}: {e}")
    return jsonify({"message": "Simulado!"})


@app.route('/api/next_phase/<c_id>', methods=['POST'])
def next_phase(c_id):
    data = load_data()
    if c_id not in data['camps']:
        return jsonify({"error": "Camp não existe"}), 404
    camp = data['camps'][c_id]
    fmt = camp.get('format', DEFAULT_FORMAT)
    cfg = camp.get('config', DEFAULT_CONFIG)
    phase = camp['phase']
    matches = camp['matches']

    cur_matches = [m for m in matches if matches_in_phase(m, phase)]
    if not cur_matches:
        return jsonify({"error": "Nenhum jogo encontrado para a fase atual."}), 400
    if any(not m.get('played') for m in cur_matches):
        return jsonify({"error": "Simule todos os jogos da fase atual primeiro!"}), 400

    new_matches = []
    two_legs = cfg.get('ko_two_legs', True)

    if phase == 'groups':
        if fmt == 'groups_only':
            camp['phase'] = 'finished'
        else:
            qualified = get_qualified_from_groups(camp, cfg['advance_per_group'])
            ko_size = len(qualified)
            stage = KO_STAGE_BY_SIZE.get(ko_size)
            if not stage:
                return jsonify({"error": f"Total de classificados ({ko_size}) não é potência de 2."}), 400
            new_matches = create_ko_round(camp, qualified, stage, two_legs)
            camp['phase'] = stage.lower()

    elif phase == 'league':
        if fmt == 'league':
            camp['phase'] = 'finished'
        else:
            ko_size = cfg['ko_size']
            top = get_league_top(camp, ko_size)
            random.shuffle(top)
            stage = KO_STAGE_BY_SIZE[ko_size]
            new_matches = create_ko_round(camp, top, stage, two_legs)
            camp['phase'] = stage.lower()

    elif phase in ('r32', 'r16', 'qf', 'sf'):
        prefix = phase.upper()
        winners = get_round_winners(matches, prefix, two_legs)
        if len(winners) < 2:
            return jsonify({"error": "Vencedores insuficientes para próxima fase."}), 400
        idx = KO_ORDER.index(prefix) + 1
        next_stage = KO_ORDER[idx]
        new_matches = create_ko_round(camp, winners, next_stage, two_legs)
        camp['phase'] = next_stage.lower()

    elif phase == 'final':
        camp['phase'] = 'finished'

    elif phase == 'finished':
        return jsonify({"error": "Campeonato já finalizado."}), 400

    else:
        return jsonify({"error": f"Fase desconhecida: {phase}"}), 400

    camp['matches'].extend(new_matches)
    save_data(data)
    return jsonify({"message": "Avançamos de fase!"})


# ------ ROTA DE NOTÍCIAS (IA) ------

@app.route('/api/camps/<c_id>/generate_news', methods=['POST'])
def generate_news(c_id):
    data = load_data()
    camp = data['camps'].get(c_id)
    if not camp:
        return jsonify({"error": "Camp não existe"}), 404

    played_matches = [m for m in camp['matches'] if m.get('played')]
    recent_matches = played_matches[-3:] if played_matches else []

    stats = list(camp['stats'].values())
    top_scorers = sorted([s for s in stats if s['goals'] > 0 and s['name']], key=lambda x: x['goals'], reverse=True)[:3]

    context_str = f"Campeonato: {camp['name']}\n"
    if recent_matches:
        context_str += "Últimos resultados:\n"
        for m in recent_matches:
            context_str += f"- {m['home']['name']} {m['hg']} x {m['ag']} {m['away']['name']}\n"
    if top_scorers:
        context_str += "Artilheiros em destaque:\n"
        for s in top_scorers:
            context_str += f"- {s['name']} ({s['team']}): {s['goals']} gols\n"

    if not recent_matches and not top_scorers:
        context_str += "O campeonato acabou de começar e os times ainda estão se preparando para a estreia!"

    prompt = f"""Atue como um jornalista esportivo muito criativo, carismático e um pouco fofoqueiro.
Com base nos dados abaixo do 'Hades Football Simulator', crie UMA ÚNICA manchete impactante e um texto de até 4 linhas com uma notícia.
Você pode inventar que um jogador foi visto numa boate, criar uma aspas (fala) provocativa de um jogador após um jogo, analisar um placar elástico, ou falar de crise no time que perdeu.
Seja criativo e realista dentro do universo do futebol.
Dados do simulador no momento:
{context_str}
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8
    }

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=payload, headers=headers, timeout=20
        )
        res_json = res.json()
        if "error" in res_json:
            return jsonify({"error": res_json["error"]["message"]}), 500

        news_text = res_json['choices'][0]['message']['content']
    except requests.exceptions.Timeout:
        return jsonify({"error": "A IA demorou demais para responder. Tente de novo."}), 504
    except Exception as e:
        return jsonify({"error": f"Erro na IA: {str(e)}"}), 500

    if 'news' not in camp:
        camp['news'] = []
    new_article = {"id": str(uuid.uuid4())[:8], "text": news_text}
    camp['news'].insert(0, new_article)

    save_data(data)
    return jsonify({"message": "Notícia gerada!", "news": new_article})


# ============================================================
# ============== SISTEMA DE APOSTAS / HCOINS =================
# ============================================================

# ---- Helpers de odds ----
def compute_match_odds(c_id, m_id):
    """Gera odds determinísticas por jogo (1X2, Over/Under 2.5, Ambas marcam)."""
    seed = int(hashlib.md5(f"{c_id}_{m_id}".encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)

    # Probabilidades base (mandante leva pequena vantagem)
    p_h = 0.40 + rng.uniform(-0.18, 0.18)
    p_a = 0.32 + rng.uniform(-0.18, 0.18)
    p_h = max(0.10, min(0.75, p_h))
    p_a = max(0.10, min(0.75, p_a))
    p_d = max(0.08, 1.0 - p_h - p_a)
    total = p_h + p_d + p_a
    p_h, p_d, p_a = p_h/total, p_d/total, p_a/total

    p_over = 0.50 + rng.uniform(-0.20, 0.20)
    p_over = max(0.20, min(0.80, p_over))
    p_under = 1.0 - p_over

    p_btts_yes = 0.52 + rng.uniform(-0.18, 0.18)
    p_btts_yes = max(0.20, min(0.80, p_btts_yes))
    p_btts_no = 1.0 - p_btts_yes

    margin = 0.93  # margem da casa ~7%
    return {
        "1X2": {
            "1": round(margin / p_h, 2),
            "X": round(margin / p_d, 2),
            "2": round(margin / p_a, 2),
        },
        "OU25": {
            "over": round(margin / p_over, 2),
            "under": round(margin / p_under, 2),
        },
        "BTTS": {
            "yes": round(margin / p_btts_yes, 2),
            "no": round(margin / p_btts_no, 2),
        }
    }


def evaluate_bet(bet_type, selection, hg, ag):
    """Retorna True se a aposta foi vencedora."""
    if bet_type == "1X2":
        if selection == "1": return hg > ag
        if selection == "X": return hg == ag
        if selection == "2": return ag > hg
    if bet_type == "OU25":
        total = hg + ag
        if selection == "over": return total > 2.5
        if selection == "under": return total < 2.5
    if bet_type == "BTTS":
        if selection == "yes": return hg > 0 and ag > 0
        if selection == "no": return not (hg > 0 and ag > 0)
    return False


def settle_bets_for_match(c_id, m_id, hg, ag):
    """Liquida todas as apostas pendentes de uma partida."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, bettor_id, bet_type, selection, amount, odd FROM bets "
                "WHERE camp_id=%s AND match_id=%s AND status='pending'",
                (c_id, m_id)
            )
            pending = cur.fetchall()
            for b in pending:
                won = evaluate_bet(b['bet_type'], b['selection'], hg, ag)
                if won:
                    payout = int(round(float(b['amount']) * float(b['odd'])))
                    cur.execute(
                        "UPDATE bets SET status='won', payout=%s, settled_at=NOW() WHERE id=%s",
                        (payout, b['id'])
                    )
                    cur.execute(
                        "UPDATE bettors SET hcoins = hcoins + %s WHERE id=%s",
                        (payout, b['bettor_id'])
                    )
                else:
                    cur.execute(
                        "UPDATE bets SET status='lost', payout=0, settled_at=NOW() WHERE id=%s",
                        (b['id'],)
                    )
        conn.commit()


# ---- Auth helpers ----
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({"error": "Apenas admin pode executar isso."}), 403
        return fn(*args, **kwargs)
    return wrapper


def bettor_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('bettor_id'):
            return jsonify({"error": "Faça login como apostador."}), 401
        return fn(*args, **kwargs)
    return wrapper


def player_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('player_id'):
            return jsonify({"error": "Faça login como jogador."}), 401
        return fn(*args, **kwargs)
    return wrapper


def get_bettor(bettor_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, name, hcoins FROM bettors WHERE id=%s",
                (bettor_id,)
            )
            return cur.fetchone()


def get_player(player_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, player_name, team_name FROM players WHERE id=%s",
                (player_id,)
            )
            return cur.fetchone()


# ---- Auth: ADMIN ----
@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    req = request.json or {}
    if req.get('username') == ADMIN_USER and req.get('password') == ADMIN_PASS:
        session['is_admin'] = True
        session.permanent = True
        return jsonify({"message": "Login admin OK"})
    return jsonify({"error": "Usuário ou senha incorretos."}), 401


@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('is_admin', None)
    return jsonify({"message": "Logout OK"})


@app.route('/api/admin/me', methods=['GET'])
def admin_me():
    return jsonify({"is_admin": bool(session.get('is_admin'))})


# ---- Auth: APOSTADOR ----
@app.route('/api/bettor/register', methods=['POST'])
def bettor_register():
    req = request.json or {}
    username = (req.get('username') or '').strip().lower()
    password = req.get('password') or ''
    name = (req.get('name') or '').strip()

    if len(username) < 3 or len(username) > 50:
        return jsonify({"error": "Usuário precisa ter entre 3 e 50 caracteres."}), 400
    if not username.replace('_', '').replace('.', '').isalnum():
        return jsonify({"error": "Usuário só pode ter letras, números, _ ou ."}), 400
    if len(password) < 4:
        return jsonify({"error": "Senha precisa ter pelo menos 4 caracteres."}), 400
    if len(name) < 2:
        return jsonify({"error": "Informe um nome válido."}), 400

    bettor_id = str(uuid.uuid4())[:8]
    pwd_hash = generate_password_hash(password)

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bettors (id, username, password_hash, name, hcoins) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (bettor_id, username, pwd_hash, name, INITIAL_HCOINS)
                )
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Esse usuário já existe."}), 400
    except Exception as e:
        return jsonify({"error": f"Erro ao cadastrar: {str(e)}"}), 500

    session['bettor_id'] = bettor_id
    session.permanent = True
    return jsonify({
        "message": "Conta criada com sucesso!",
        "bettor": {"id": bettor_id, "username": username, "name": name, "hcoins": INITIAL_HCOINS}
    })


@app.route('/api/bettor/login', methods=['POST'])
def bettor_login():
    req = request.json or {}
    username = (req.get('username') or '').strip().lower()
    password = req.get('password') or ''

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, password_hash, name, hcoins FROM bettors WHERE username=%s",
                (username,)
            )
            row = cur.fetchone()

    if not row or not check_password_hash(row['password_hash'], password):
        return jsonify({"error": "Usuário ou senha inválidos."}), 401

    session['bettor_id'] = row['id']
    session.permanent = True
    return jsonify({
        "message": "Login OK",
        "bettor": {"id": row['id'], "username": row['username'], "name": row['name'], "hcoins": row['hcoins']}
    })


@app.route('/api/bettor/logout', methods=['POST'])
def bettor_logout():
    session.pop('bettor_id', None)
    return jsonify({"message": "Logout OK"})


@app.route('/api/bettor/me', methods=['GET'])
@bettor_required
def bettor_me():
    bettor = get_bettor(session['bettor_id'])
    if not bettor:
        session.pop('bettor_id', None)
        return jsonify({"error": "Apostador não encontrado."}), 404
    return jsonify({"bettor": dict(bettor)})


# ---- Admin: abre/fecha apostas em um campeonato ----
@app.route('/api/camps/<c_id>/betting/<action>', methods=['POST'])
@admin_required
def toggle_betting(c_id, action):
    if action not in ('open', 'close'):
        return jsonify({"error": "Ação inválida."}), 400
    data = load_data()
    if c_id not in data['camps']:
        return jsonify({"error": "Campeonato não encontrado."}), 404
    data['camps'][c_id]['betting_open'] = (action == 'open')
    save_data(data)
    msg = "Apostas liberadas!" if action == 'open' else "Apostas encerradas!"
    return jsonify({"message": msg, "betting_open": data['camps'][c_id]['betting_open']})


# ---- Apostador: visualiza campeonatos disponíveis ----
@app.route('/api/bettor/camps', methods=['GET'])
@bettor_required
def bettor_list_camps():
    data = load_data()
    out = []
    for cid, c in data['camps'].items():
        if not c.get('betting_open'):
            continue
        out.append({
            "id": cid,
            "name": c['name'],
            "phase": c['phase'],
            "format": c.get('format'),
            "num_teams": len(c.get('teams', [])),
            "matches_total": len(c.get('matches', [])),
            "matches_pending": sum(1 for m in c.get('matches', []) if not m.get('played')),
        })
    return jsonify({"camps": out})


@app.route('/api/bettor/camps/<c_id>', methods=['GET'])
@bettor_required
def bettor_camp_detail(c_id):
    data = load_data()
    camp = data['camps'].get(c_id)
    if not camp or not camp.get('betting_open'):
        return jsonify({"error": "Campeonato não disponível para apostas."}), 403

    pending_matches = []
    for m in camp.get('matches', []):
        if m.get('played'):
            continue
        odds = compute_match_odds(c_id, m['id'])
        pending_matches.append({
            "id": m['id'],
            "stage": m['stage'],
            "home": {"id": m['home']['id'], "name": m['home']['name']},
            "away": {"id": m['away']['id'], "name": m['away']['name']},
            "odds": odds
        })

    played_matches = []
    for m in camp.get('matches', []):
        if not m.get('played'):
            continue
        played_matches.append({
            "id": m['id'],
            "stage": m['stage'],
            "home": {"name": m['home']['name']},
            "away": {"name": m['away']['name']},
            "hg": m['hg'], "ag": m['ag'],
            "penalties": m.get('penalties')
        })

    return jsonify({
        "camp": {
            "id": c_id,
            "name": camp['name'],
            "phase": camp['phase'],
            "format": camp.get('format'),
        },
        "matches_pending": pending_matches,
        "matches_played": played_matches[-15:],  # últimos 15 resultados
    })


# ---- Apostador: faz aposta ----
@app.route('/api/bettor/bets', methods=['POST'])
@bettor_required
def place_bet():
    req = request.json or {}
    c_id = req.get('camp_id')
    m_id = req.get('match_id')
    bet_type = req.get('bet_type')
    selection = req.get('selection')
    amount = req.get('amount')

    if bet_type not in ('1X2', 'OU25', 'BTTS'):
        return jsonify({"error": "Tipo de aposta inválido."}), 400
    valid_selections = {'1X2': ['1','X','2'], 'OU25': ['over','under'], 'BTTS': ['yes','no']}
    if selection not in valid_selections[bet_type]:
        return jsonify({"error": "Seleção inválida."}), 400
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "Valor inválido."}), 400
    if amount < 10:
        return jsonify({"error": "Aposta mínima de 10 hcoins."}), 400

    data = load_data()
    camp = data['camps'].get(c_id)
    if not camp or not camp.get('betting_open'):
        return jsonify({"error": "Apostas indisponíveis para esse campeonato."}), 403

    match = next((m for m in camp['matches'] if m['id'] == m_id), None)
    if not match:
        return jsonify({"error": "Partida não encontrada."}), 404
    if match.get('played'):
        return jsonify({"error": "Essa partida já foi jogada."}), 400

    odds = compute_match_odds(c_id, m_id)
    odd = odds[bet_type][selection]

    bettor_id = session['bettor_id']
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT hcoins FROM bettors WHERE id=%s FOR UPDATE", (bettor_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Apostador não encontrado."}), 404
            if row['hcoins'] < amount:
                return jsonify({"error": "Saldo insuficiente de hcoins."}), 400
            cur.execute(
                "UPDATE bettors SET hcoins = hcoins - %s WHERE id=%s",
                (amount, bettor_id)
            )
            cur.execute(
                "INSERT INTO bets (bettor_id, camp_id, match_id, bet_type, selection, amount, odd) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (bettor_id, c_id, m_id, bet_type, selection, amount, odd)
            )
            bet_id = cur.fetchone()['id']
            cur.execute("SELECT hcoins FROM bettors WHERE id=%s", (bettor_id,))
            new_balance = cur.fetchone()['hcoins']
        conn.commit()

    return jsonify({
        "message": "Aposta registrada!",
        "bet": {"id": bet_id, "amount": amount, "odd": odd, "potential_payout": int(round(amount * odd))},
        "balance": new_balance
    })


# ---- Apostador: lista minhas apostas ----
@app.route('/api/bettor/bets', methods=['GET'])
@bettor_required
def list_my_bets():
    bettor_id = session['bettor_id']
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, camp_id, match_id, bet_type, selection, amount, odd, status, payout, "
                "created_at, settled_at FROM bets WHERE bettor_id=%s ORDER BY created_at DESC LIMIT 200",
                (bettor_id,)
            )
            rows = cur.fetchall()
    data = load_data()
    out = []
    for b in rows:
        camp = data['camps'].get(b['camp_id'])
        match_info = None
        if camp:
            m = next((mm for mm in camp.get('matches', []) if mm['id'] == b['match_id']), None)
            if m:
                match_info = {
                    "stage": m['stage'],
                    "home": m['home']['name'], "away": m['away']['name'],
                    "played": m.get('played'),
                    "hg": m.get('hg'), "ag": m.get('ag')
                }
        out.append({
            "id": b['id'],
            "camp_id": b['camp_id'],
            "camp_name": camp['name'] if camp else "—",
            "match_id": b['match_id'],
            "match": match_info,
            "bet_type": b['bet_type'],
            "selection": b['selection'],
            "amount": b['amount'],
            "odd": float(b['odd']),
            "status": b['status'],
            "payout": b['payout'],
            "created_at": b['created_at'].isoformat() if b['created_at'] else None,
            "settled_at": b['settled_at'].isoformat() if b['settled_at'] else None,
        })
    return jsonify({"bets": out})


# ---- Ranking público dos apostadores ----
@app.route('/api/bettor/ranking', methods=['GET'])
@bettor_required
def bettor_ranking():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, username, hcoins FROM bettors ORDER BY hcoins DESC LIMIT 20"
            )
            rows = cur.fetchall()
    return jsonify({"ranking": [dict(r) for r in rows]})


# =====================================================================
# ============== PAINEL DO JOGADOR + HSOCIAL + ELENCO GLOBAL ==========
# =====================================================================

# ---- Helper: vencedor de um campeonato ----
def compute_camp_winner(camp):
    """Retorna o nome do time campeão (ou None se não dá pra inferir)."""
    if camp.get('phase') != 'finished':
        return None
    fmt = camp.get('format', DEFAULT_FORMAT)
    matches = camp.get('matches', [])
    # Final do mata-mata
    final = next((m for m in matches if m.get('stage') == 'Final' and m.get('played')), None)
    if final:
        if final['hg'] > final['ag']:
            return final['home']['name']
        if final['ag'] > final['hg']:
            return final['away']['name']
        pen = final.get('penalties') or ''
        if final['home']['name'] in pen:
            return final['home']['name']
        if final['away']['name'] in pen:
            return final['away']['name']
        return None
    # Liga pura
    if fmt == 'league':
        top = get_league_top(camp, 1)
        if top:
            return top[0]['t']['name']
    # groups_only — sem critério único, devolve top do grupo A
    if fmt == 'groups_only':
        try:
            ordered = get_qualified_from_groups(camp, 1)
            if ordered:
                return ordered[0]['name']
        except Exception:
            return None
    return None


# ---- Auth: JOGADOR ----
@app.route('/api/player/register', methods=['POST'])
def player_register():
    req = request.json or {}
    username = (req.get('username') or '').strip().lower()
    password = req.get('password') or ''
    player_name = (req.get('player_name') or '').strip()
    team_name = (req.get('team_name') or '').strip()

    if len(username) < 3 or len(username) > 50:
        return jsonify({"error": "Usuário precisa ter entre 3 e 50 caracteres."}), 400
    if not username.replace('_', '').replace('.', '').isalnum():
        return jsonify({"error": "Usuário só pode ter letras, números, _ ou ."}), 400
    if len(password) < 4:
        return jsonify({"error": "Senha precisa ter pelo menos 4 caracteres."}), 400
    if len(player_name) < 2:
        return jsonify({"error": "Informe o nome do jogador."}), 400
    if len(team_name) < 2:
        return jsonify({"error": "Informe o nome do time."}), 400

    pid = str(uuid.uuid4())[:8]
    pwd_hash = generate_password_hash(password)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO players (id, username, password_hash, player_name, team_name) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (pid, username, pwd_hash, player_name, team_name)
                )
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Esse usuário já existe."}), 400
    except Exception as e:
        return jsonify({"error": f"Erro ao cadastrar: {str(e)}"}), 500

    session['player_id'] = pid
    session.permanent = True
    return jsonify({
        "message": "Conta criada com sucesso!",
        "player": {"id": pid, "username": username, "player_name": player_name, "team_name": team_name}
    })


@app.route('/api/player/login', methods=['POST'])
def player_login():
    req = request.json or {}
    username = (req.get('username') or '').strip().lower()
    password = req.get('password') or ''
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, password_hash, player_name, team_name FROM players WHERE username=%s",
                (username,)
            )
            row = cur.fetchone()
    if not row or not check_password_hash(row['password_hash'], password):
        return jsonify({"error": "Usuário ou senha inválidos."}), 401
    session['player_id'] = row['id']
    session.permanent = True
    return jsonify({
        "message": "Login OK",
        "player": {
            "id": row['id'], "username": row['username'],
            "player_name": row['player_name'], "team_name": row['team_name']
        }
    })


@app.route('/api/player/logout', methods=['POST'])
def player_logout():
    session.pop('player_id', None)
    return jsonify({"message": "Logout OK"})


@app.route('/api/player/me', methods=['GET'])
@player_required
def player_me():
    p = get_player(session['player_id'])
    if not p:
        session.pop('player_id', None)
        return jsonify({"error": "Jogador não encontrado."}), 404
    return jsonify({"player": dict(p)})


@app.route('/api/player/update', methods=['POST'])
@player_required
def player_update():
    """Permite ao jogador atualizar nome de jogador / time."""
    req = request.json or {}
    player_name = (req.get('player_name') or '').strip()
    team_name = (req.get('team_name') or '').strip()
    if len(player_name) < 2 or len(team_name) < 2:
        return jsonify({"error": "Preencha o nome do jogador e o time."}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE players SET player_name=%s, team_name=%s WHERE id=%s",
                (player_name, team_name, session['player_id'])
            )
        conn.commit()
    return jsonify({"message": "Dados atualizados!", "player_name": player_name, "team_name": team_name})


# ---- Painel do Jogador: Desempenho ----
@app.route('/api/player/performance', methods=['GET'])
@player_required
def player_performance():
    player = get_player(session['player_id'])
    if not player:
        return jsonify({"error": "Jogador não encontrado."}), 404

    pname = player['player_name'].strip().lower()
    tname = player['team_name'].strip().lower()
    data = load_data()

    tournaments = []
    tot_g = tot_a = tot_y = tot_r = tot_i = 0
    tot_motm = 0
    tot_matches_played_in = 0
    rating_sum = 0.0
    rating_count = 0
    champs_played = 0
    champs_won = 0

    for cid, camp in data['camps'].items():
        # O jogador participou? Procura em stats por nome+time
        match_stat = None
        for sid, s in (camp.get('stats') or {}).items():
            if (s.get('name') or '').strip().lower() == pname and (s.get('team') or '').strip().lower() == tname:
                match_stat = s
                break
        if not match_stat:
            continue
        champs_played += 1

        c_g = match_stat.get('goals', 0)
        c_a = match_stat.get('assists', 0)
        c_y = match_stat.get('yellows', 0)
        c_r = match_stat.get('reds', 0)
        c_i = match_stat.get('injuries', 0)

        c_rating_sum = 0.0
        c_rating_cnt = 0
        c_motm = 0
        c_matches = 0

        for m in camp.get('matches', []):
            if not m.get('played'):
                continue
            home_name = (m.get('home', {}).get('name') or '').strip().lower()
            away_name = (m.get('away', {}).get('name') or '').strip().lower()
            ratings_block = None
            if home_name == tname:
                ratings_block = m.get('ratings_h') or {}
            elif away_name == tname:
                ratings_block = m.get('ratings_a') or {}
            if ratings_block is None:
                continue
            # acha o rating do jogador (case-insensitive)
            mine = None
            for rname, rv in ratings_block.items():
                if rname.strip().lower() == pname:
                    mine = rv
                    break
            if mine is None:
                continue
            c_matches += 1
            c_rating_sum += float(mine.get('val', 0))
            c_rating_cnt += 1
            # MOTM = maior nota da partida (somando home+away)
            all_vals = []
            for rb in (m.get('ratings_h') or {}, m.get('ratings_a') or {}):
                for nm, vv in rb.items():
                    all_vals.append((nm.strip().lower(), float(vv.get('val', 0))))
            if all_vals:
                top_name, top_val = max(all_vals, key=lambda x: x[1])
                if top_name == pname and abs(top_val - float(mine.get('val', 0))) < 0.001:
                    c_motm += 1

        winner = compute_camp_winner(camp)
        won = bool(winner and winner.strip().lower() == tname)
        if camp.get('phase') == 'finished':
            if won:
                champs_won += 1

        tournaments.append({
            "camp_id": cid,
            "camp_name": camp['name'],
            "phase": camp['phase'],
            "format": camp.get('format'),
            "team": match_stat.get('team'),
            "pos": match_stat.get('pos'),
            "matches_played": c_matches,
            "goals": c_g,
            "assists": c_a,
            "yellows": c_y,
            "reds": c_r,
            "injuries": c_i,
            "rating_avg": round(c_rating_sum / c_rating_cnt, 2) if c_rating_cnt else None,
            "rating_count": c_rating_cnt,
            "motm_count": c_motm,
            "champion": winner,
            "won": won,
            "finished": camp.get('phase') == 'finished',
        })

        tot_g += c_g; tot_a += c_a; tot_y += c_y; tot_r += c_r; tot_i += c_i
        tot_motm += c_motm
        tot_matches_played_in += c_matches
        rating_sum += c_rating_sum
        rating_count += c_rating_cnt

    summary = {
        "champs_played": champs_played,
        "champs_won": champs_won,
        "matches_played": tot_matches_played_in,
        "goals": tot_g,
        "assists": tot_a,
        "yellows": tot_y,
        "reds": tot_r,
        "injuries": tot_i,
        "motm_count": tot_motm,
        "rating_avg": round(rating_sum / rating_count, 2) if rating_count else None,
        "rating_count": rating_count,
    }

    return jsonify({
        "player": dict(player),
        "summary": summary,
        "tournaments": tournaments
    })


# ---- HSocial: feed público + posts ----
@app.route('/api/hsocial/posts', methods=['GET'])
def hsocial_list():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT p.id, p.text, p.image, p.created_at,
                       pl.id AS player_id, pl.player_name, pl.team_name, pl.username
                FROM hsocial_posts p
                JOIN players pl ON pl.id = p.player_id
                ORDER BY p.created_at DESC
                LIMIT 200
            """)
            rows = cur.fetchall()
    posts = []
    for r in rows:
        posts.append({
            "id": r['id'],
            "text": r['text'],
            "image": r['image'],
            "created_at": r['created_at'].isoformat() if r['created_at'] else None,
            "player_id": r['player_id'],
            "player_name": r['player_name'],
            "team_name": r['team_name'],
            "username": r['username'],
        })
    return jsonify({"posts": posts})


@app.route('/api/hsocial/posts', methods=['POST'])
@player_required
def hsocial_create():
    req = request.json or {}
    text = (req.get('text') or '').strip()
    image = req.get('image')  # data URL base64 ou None
    if not text and not image:
        return jsonify({"error": "Escreva algo ou envie uma imagem."}), 400
    if text and len(text) > 2000:
        return jsonify({"error": "Texto longo demais (máx 2000)."}), 400
    if image:
        if not isinstance(image, str) or not image.startswith('data:image/'):
            return jsonify({"error": "Formato de imagem inválido."}), 400
        if len(image) > 5_500_000:
            return jsonify({"error": "Imagem muito grande (máx ~4MB)."}), 400
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO hsocial_posts (player_id, text, image) VALUES (%s,%s,%s) RETURNING id, created_at",
                (session['player_id'], text or None, image or None)
            )
            row = cur.fetchone()
        conn.commit()
    return jsonify({
        "message": "Post publicado!",
        "post": {
            "id": row['id'],
            "created_at": row['created_at'].isoformat() if row['created_at'] else None
        }
    })


@app.route('/api/hsocial/posts/<int:post_id>', methods=['DELETE'])
@player_required
def hsocial_delete(post_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT player_id FROM hsocial_posts WHERE id=%s", (post_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Post não encontrado."}), 404
            if row['player_id'] != session['player_id']:
                return jsonify({"error": "Você só pode apagar seus próprios posts."}), 403
            cur.execute("DELETE FROM hsocial_posts WHERE id=%s", (post_id,))
        conn.commit()
    return jsonify({"message": "Post removido."})


# ---- Admin: CRUD de elenco global (times + jogadores) ----
@app.route('/api/global_teams/<t_id>', methods=['PUT'])
@admin_required
def global_team_update(t_id):
    """Atualiza o nome do time e/ou jogadores (sem propagar a campeonatos já criados)."""
    req = request.json or {}
    new_name = (req.get('name') or '').strip()
    new_players = req.get('players')
    data = load_data()
    team = next((t for t in data['global_teams'] if t['id'] == t_id), None)
    if not team:
        return jsonify({"error": "Time não encontrado."}), 404
    if new_name:
        team['name'] = new_name
    if isinstance(new_players, list) and len(new_players) == len(team['players']):
        for i, p in enumerate(new_players):
            team['players'][i]['name'] = (p.get('name') or '').strip()
            if p.get('pos'):
                team['players'][i]['pos'] = p['pos']
    save_data(data)
    return jsonify({"message": "Time atualizado!", "team": team})


@app.route('/api/global_teams/<t_id>', methods=['DELETE'])
@admin_required
def global_team_delete(t_id):
    data = load_data()
    team = next((t for t in data['global_teams'] if t['id'] == t_id), None)
    if not team:
        return jsonify({"error": "Time não encontrado."}), 404
    # Verifica se está em uso em algum campeonato
    in_use = []
    for cid, camp in data['camps'].items():
        if any(ct['id'] == t_id for ct in camp.get('teams', [])):
            in_use.append(camp['name'])
    if in_use:
        return jsonify({"error": f"Time em uso nos campeonatos: {', '.join(in_use)}. Apague/remova de lá primeiro."}), 400
    data['global_teams'] = [t for t in data['global_teams'] if t['id'] != t_id]
    save_data(data)
    return jsonify({"message": "Time apagado do elenco."})


@app.route('/api/global_teams/transfer', methods=['POST'])
@admin_required
def global_team_transfer():
    """Transfere um jogador de um time pra outro no elenco global.
    Body: {from_team_id, from_index, to_team_id, to_index}
    Se houver jogador na posição de destino, faz swap (troca os dois).
    """
    req = request.json or {}
    from_id = req.get('from_team_id')
    from_idx = req.get('from_index')
    to_id = req.get('to_team_id')
    to_idx = req.get('to_index')
    try:
        from_idx = int(from_idx); to_idx = int(to_idx)
    except (TypeError, ValueError):
        return jsonify({"error": "Índices inválidos."}), 400
    if from_id == to_id and from_idx == to_idx:
        return jsonify({"error": "Origem e destino iguais."}), 400

    data = load_data()
    src = next((t for t in data['global_teams'] if t['id'] == from_id), None)
    dst = next((t for t in data['global_teams'] if t['id'] == to_id), None)
    if not src or not dst:
        return jsonify({"error": "Time origem/destino não encontrado."}), 404
    if from_idx < 0 or from_idx >= len(src['players']):
        return jsonify({"error": "Posição de origem inválida."}), 400
    if to_idx < 0 or to_idx >= len(dst['players']):
        return jsonify({"error": "Posição de destino inválida."}), 400

    src_p = src['players'][from_idx]
    dst_p = dst['players'][to_idx]
    if not (src_p.get('name') or '').strip():
        return jsonify({"error": "Não há jogador na posição de origem."}), 400

    # Swap apenas dos nomes (mantém posições do esquema 4-3-3)
    src['players'][from_idx]['name'] = dst_p.get('name', '')
    dst['players'][to_idx]['name'] = src_p.get('name', '')
    save_data(data)
    return jsonify({
        "message": (f"Jogador transferido para {dst['name']}!"
                    if not dst_p.get('name') else
                    f"Jogadores trocados entre {src['name']} e {dst['name']}!")
    })


init_db()
if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)