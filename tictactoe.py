"""
Complete Tic Tac Toe Game System for Telegram Bot.

States: WAITING_FOR_PLAYER, ACTIVE, PLAYER_X_WON, PLAYER_O_WON, DRAW, CANCELLED, EXPIRED
"""

import uuid
import logging
import threading
from datetime import datetime, timedelta, timezone

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import db

logger = logging.getLogger(__name__)
lock = threading.Lock()

_TZ = timezone(timedelta(hours=6))
LOBBY_TIMEOUT_SECONDS = 120

__all__ = [
    'init_db', 'now', 'display_board', 'check_winner', 'is_draw',
    'get_game', 'get_active_game', 'create_lobby', 'join_lobby',
    'create_game_from_lobby', 'make_move', 'update_score',
    'get_scores', 'get_global_scores', 'cancel_game',
    'build_lobby_markup', 'build_board_markup', 'build_game_over_markup',
    'format_lobby_message', 'format_game_message', 'format_game_over_message',
    'get_ttt_board_display'
]

def now():
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")

def _is_pg():
    """True when the app is running on PostgreSQL (production)."""
    try:
        return getattr(db, "backend", "sqlite") == "pg"
    except Exception:
        return False

def _row_to_dict(cur, row):
    """Convert a DB row to a plain dict on both SQLite and Postgres.

    SQLite rows are sqlite3.Row sequences (needs cursor.description for
    column names); Postgres RealDictCursor rows are already dicts.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        cols = [d[0] for d in cur.description]
    except Exception:
        return None
    try:
        return dict(zip(cols, list(row)))
    except Exception:
        return None

def _rows_to_dicts(cur, rows):
    out = []
    for r in rows or []:
        d = _row_to_dict(cur, r)
        if d is not None:
            out.append(d)
    return out

def _begin_atomic():
    """Start a write transaction (SQLite: BEGIN IMMEDIATE, PG: BEGIN)."""
    if _is_pg():
        db.conn.execute("BEGIN")
    else:
        db.conn.execute("BEGIN IMMEDIATE")

def _for_update():
    """Row-locking clause for SELECT inside a transaction (PG only)."""
    return " FOR UPDATE" if _is_pg() else ""

def display_board(board):
    symbols = {'X': '❌', 'O': '⭕', '.': '✨'}
    rows = []
    for i in range(0, 9, 3):
        row = [symbols.get(board[i+j], str(i+j+1)) for j in range(3)]
        rows.append(' | '.join(row))
    return '\n'.join(f" {r} " for r in rows)

def check_winner(board):
    lines = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for a,b,c in lines:
        if board[a] != '.' and board[a] == board[b] == board[c]:
            return board[a]
    return None

def is_draw(board):
    return '.' not in board

# ── DATABASE METHODS ──

def init_db():
    try:
        c = db.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS ttt_lobbies (
            game_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, message_id INTEGER,
            player1_id TEXT NOT NULL, player1_name TEXT NOT NULL,
            player2_id TEXT, player2_name TEXT, board TEXT DEFAULT '.........',
            current_turn TEXT DEFAULT 'X', status TEXT DEFAULT 'WAITING_FOR_PLAYER',
            created_at TEXT, updated_at TEXT, expires_at TEXT)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_ttt_lobby_chat ON ttt_lobbies(chat_id, status)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ttt_games (
            game_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, message_id INTEGER,
            player1_id TEXT NOT NULL, player1_name TEXT NOT NULL,
            player2_id TEXT, player2_name TEXT, board TEXT DEFAULT '.........',
            current_turn TEXT DEFAULT 'X', status TEXT DEFAULT 'ACTIVE',
            winner TEXT, created_at TEXT, updated_at TEXT)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_ttt_game_chat ON ttt_games(chat_id, status)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ttt_scores (
            chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
            games_played INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0,
            total_points INTEGER DEFAULT 0, consecutive_wins INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id))""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_ttt_score_chat ON ttt_scores(chat_id, total_points DESC)""")
        db.conn.commit()
    except Exception as e:
        logger.error(f"TTT DB init error: {e}")

def get_game(game_id):
    try:
        c = db.conn.cursor()
        c.execute("SELECT * FROM ttt_lobbies WHERE game_id=?", (game_id,))
        game = _row_to_dict(c, c.fetchone())
        if game:
            return game
        c.execute("SELECT * FROM ttt_games WHERE game_id=?", (game_id,))
        return _row_to_dict(c, c.fetchone())
    except Exception as e:
        logger.error(f"Get game error: {e}")
        return None

def get_active_game(chat_id):
    try:
        c = db.conn.cursor()
        c.execute("SELECT * FROM ttt_lobbies WHERE chat_id=? AND status='WAITING_FOR_PLAYER' ORDER BY created_at DESC LIMIT 1", (str(chat_id),))
        game = _row_to_dict(c, c.fetchone())
        if game:
            # Auto-purge expired lobbies so they never block /ttt forever
            if game.get('expires_at') and game['expires_at'] < now():
                try:
                    c.execute("DELETE FROM ttt_lobbies WHERE game_id=?", (game['game_id'],))
                    db.conn.commit()
                except Exception:
                    pass
                game = None
            else:
                return game
        c.execute("SELECT * FROM ttt_games WHERE chat_id=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1", (str(chat_id),))
        return _row_to_dict(c, c.fetchone())
    except Exception as e:
        logger.error(f"Get active game error: {e}")
        return None

def create_lobby(chat_id, player1_id, player1_name):
    game_id = uuid.uuid4().hex[:8]
    now_val = now()
    expires_at = (datetime.now(_TZ) + timedelta(seconds=LOBBY_TIMEOUT_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        c = db.conn.cursor()
        c.execute("""INSERT INTO ttt_lobbies (game_id, chat_id, player1_id, player1_name, status, created_at, updated_at, expires_at)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (game_id, str(chat_id), str(player1_id), player1_name, 'WAITING_FOR_PLAYER', now_val, now_val, expires_at))
        db.conn.commit()
        return game_id
    except Exception as e:
        logger.error(f"Create lobby error: {e}")
        return None

def join_lobby(game_id, player2_id, player2_name):
    try:
        _begin_atomic()
        c = db.conn.cursor()
        c.execute("SELECT * FROM ttt_lobbies WHERE game_id=? AND status='WAITING_FOR_PLAYER'" + _for_update(), (game_id,))
        game = _row_to_dict(c, c.fetchone())
        if not game:
            db.conn.rollback()
            return False, "Game not found or already full"
        if game['player2_id']:
            db.conn.rollback()
            return False, "This game is already full"
        if game['player1_id'] == str(player2_id):
            db.conn.rollback()
            return False, "You are already Player 1"
        now_val = now()
        c.execute("""UPDATE ttt_lobbies SET player2_id=?, player2_name=?, status='ACTIVE', updated_at=?
                     WHERE game_id=? AND status='WAITING_FOR_PLAYER'""",
                  (str(player2_id), player2_name, now_val, game_id))
        if c.rowcount == 0:
            db.conn.rollback()
            return False, "Race condition: someone joined first"
        db.conn.commit()
        return True, "Joined!"
    except Exception as e:
        try:
            db.conn.rollback()
        except:
            pass
        logger.error(f"Join lobby error: {e}")
        return False, "Error joining"

def create_game_from_lobby(game_id):
    try:
        c = db.conn.cursor()
        c.execute("SELECT * FROM ttt_lobbies WHERE game_id=?", (game_id,))
        lobby = _row_to_dict(c, c.fetchone())
        if not lobby:
            return False
        now_val = now()
        params = (game_id, lobby['chat_id'], lobby['message_id'], lobby['player1_id'], lobby['player1_name'],
                  lobby['player2_id'], lobby['player2_name'], lobby['board'], lobby['current_turn'], 'ACTIVE', None, now_val, now_val)
        if _is_pg():
            c.execute("""INSERT INTO ttt_games (game_id, chat_id, message_id, player1_id, player1_name, player2_id, player2_name, board, current_turn, status, winner, created_at, updated_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT (game_id) DO UPDATE SET
                           chat_id=excluded.chat_id, message_id=excluded.message_id,
                           player1_id=excluded.player1_id, player1_name=excluded.player1_name,
                           player2_id=excluded.player2_id, player2_name=excluded.player2_name,
                           board=excluded.board, current_turn=excluded.current_turn,
                           status='ACTIVE', winner=NULL,
                           created_at=excluded.created_at, updated_at=excluded.updated_at""", params)
        else:
            c.execute("""INSERT OR REPLACE INTO ttt_games (game_id, chat_id, message_id, player1_id, player1_name, player2_id, player2_name, board, current_turn, status, winner, created_at, updated_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", params)
        c.execute("DELETE FROM ttt_lobbies WHERE game_id=?", (game_id,))
        db.conn.commit()
        return True
    except Exception as e:
        logger.error(f"Create game from lobby error: {e}")
        return False

def make_move(game_id, player_id, position):
    try:
        _begin_atomic()
        c = db.conn.cursor()
        c.execute("SELECT * FROM ttt_games WHERE game_id=? AND status='ACTIVE'" + _for_update(), (game_id,))
        game = _row_to_dict(c, c.fetchone())
        if not game:
            game = get_game(game_id)
            if not game:
                db.conn.rollback()
                return None, "Game not found"
            db.conn.rollback()
            return None, f"Game is {game.get('status', 'unknown')}"
        if str(player_id) not in (game['player1_id'], game['player2_id']):
            db.conn.rollback()
            return None, "You are not a player in this game"
        current_player = game['player1_id'] if game['current_turn'] == 'X' else game['player2_id']
        if str(player_id) != str(current_player):
            db.conn.rollback()
            return None, "⏳ Not your turn!"
        board = list(game['board'])
        if position < 0 or position > 8:
            db.conn.rollback()
            return None, "Invalid cell"
        if board[position] != '.':
            db.conn.rollback()
            return None, "⚠️ This cell is already occupied!"
        symbol = 'X' if game['player1_id'] == str(player_id) else 'O'
        board[position] = symbol
        new_board = ''.join(board)
        winner = check_winner(board)
        draw = is_draw(board)
        now_val = now()
        if winner:
            c.execute("""UPDATE ttt_games SET board=?, status=?, winner=?, updated_at=? WHERE game_id=?""",
                      (new_board, f'PLAYER_{winner}_WON', winner, now_val, game_id))
            db.conn.commit()
            update_score(game['chat_id'], game['player1_id'] if winner == 'X' else game['player2_id'], 'win')
            update_score(game['chat_id'], game['player2_id'] if winner == 'X' else game['player1_id'], 'loss')
            return board, f"🏆 {'Player 1' if winner == 'X' else 'Player 2'} wins!"
        elif draw:
            c.execute("""UPDATE ttt_games SET board=?, status=?, updated_at=? WHERE game_id=?""",
                      (new_board, 'DRAW', now_val, game_id))
            db.conn.commit()
            update_score(game['chat_id'], game['player1_id'], 'draw')
            update_score(game['chat_id'], game['player2_id'], 'draw')
            return board, "🤝 It's a Draw!"
        else:
            next_turn = 'O' if game['current_turn'] == 'X' else 'X'
            c.execute("""UPDATE ttt_games SET board=?, current_turn=?, updated_at=? WHERE game_id=?""",
                      (new_board, next_turn, now_val, game_id))
            db.conn.commit()
            next_name = game['player2_name'] if next_turn == 'O' else game['player1_name']
            next_emoji = '⭕' if next_turn == 'O' else '❌'
            return board, f"{next_emoji} {next_name}'s turn"
    except Exception as e:
        try:
            db.conn.rollback()
        except:
            pass
        logger.error(f"Make move error: {e}")
        return None, "Error processing move"

def update_score(chat_id, user_id, result):
    try:
        c = db.conn.cursor()
        c.execute("SELECT * FROM ttt_scores WHERE chat_id=? AND user_id=?", (str(chat_id), str(user_id)))
        score = _row_to_dict(c, c.fetchone())
        if not score:
            c.execute("INSERT INTO ttt_scores (chat_id, user_id, games_played, wins, losses, draws, total_points, consecutive_wins) VALUES (?,?,0,0,0,0,0,0)", (str(chat_id), str(user_id)))
            db.conn.commit()
            score = {'games_played': 0, 'wins': 0, 'losses': 0, 'draws': 0, 'total_points': 0, 'consecutive_wins': 0}
        updates = {}
        if result == 'win':
            updates['wins'] = score['wins'] + 1
            updates['games_played'] = score['games_played'] + 1
            updates['total_points'] = score['total_points'] + 20
            updates['consecutive_wins'] = score['consecutive_wins'] + 1
            if updates['consecutive_wins'] >= 3:
                updates['total_points'] = updates['total_points'] + 10
        elif result == 'loss':
            updates['losses'] = score['losses'] + 1
            updates['games_played'] = score['games_played'] + 1
            updates['consecutive_wins'] = 0
        elif result == 'draw':
            updates['draws'] = score['draws'] + 1
            updates['games_played'] = score['games_played'] + 1
            updates['total_points'] = score['total_points'] + 20
            updates['consecutive_wins'] = 0
        set_clause = ', '.join([f"{k}=?" for k in updates.keys()])
        values = list(updates.values()) + [str(chat_id), str(user_id)]
        c.execute(f"UPDATE ttt_scores SET {set_clause} WHERE chat_id=? AND user_id=?", values)
        db.conn.commit()
    except Exception as e:
        logger.error(f"Update score error: {e}")

def cancel_game(chat_id, user_id):
    """Cancel a waiting lobby or active game.

    Player 1 can cancel their lobby; players can cancel their active game.
    Expired lobbies are purged automatically. Returns (ok, msg).
    """
    try:
        c = db.conn.cursor()
        c.execute("SELECT * FROM ttt_lobbies WHERE chat_id=? AND status='WAITING_FOR_PLAYER' ORDER BY created_at DESC LIMIT 1", (str(chat_id),))
        lobby = _row_to_dict(c, c.fetchone())
        if lobby:
            if lobby.get('expires_at') and lobby['expires_at'] < now():
                c.execute("DELETE FROM ttt_lobbies WHERE game_id=?", (lobby['game_id'],))
                db.conn.commit()
                return True, "🚫 Expired lobby cleared. Send /ttt to start a new game."
            if str(lobby.get('player1_id')) == str(user_id):
                c.execute("DELETE FROM ttt_lobbies WHERE game_id=?", (lobby['game_id'],))
                db.conn.commit()
                return True, "🚫 Lobby cancelled."
            return False, "⚠️ Only Player 1 can cancel this lobby."
        c.execute("SELECT * FROM ttt_games WHERE chat_id=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1", (str(chat_id),))
        game = _row_to_dict(c, c.fetchone())
        if game:
            if str(user_id) in (str(game.get('player1_id')), str(game.get('player2_id'))):
                c.execute("DELETE FROM ttt_games WHERE game_id=?", (game['game_id'],))
                db.conn.commit()
                return True, "🚫 Game cancelled."
            return False, "⚠️ Only players can cancel this game."
        return False, "❌ No active game to cancel."
    except Exception as e:
        logger.error(f"Cancel game error: {e}")
        return False, "❌ Error cancelling game."

def get_scores(chat_id, limit=10):
    try:
        c = db.conn.cursor()
        try:
            c.execute("""SELECT s.*, u.name FROM ttt_scores s LEFT JOIN users u ON s.user_id=u.user_id
                         WHERE s.chat_id=? ORDER BY s.total_points DESC LIMIT ?""", (str(chat_id), limit))
            return _rows_to_dicts(c, c.fetchall())
        except Exception:
            try:
                db.conn.rollback()
            except Exception:
                pass
            c = db.conn.cursor()
            c.execute("""SELECT * FROM ttt_scores
                         WHERE chat_id=? ORDER BY total_points DESC LIMIT ?""", (str(chat_id), limit))
            return _rows_to_dicts(c, c.fetchall())
    except Exception as e:
        logger.error(f"Get scores error: {e}")
        return []

def get_global_scores(limit=20):
    try:
        c = db.conn.cursor()
        c.execute("""SELECT user_id, SUM(games_played) as games_played, SUM(wins) as wins,
                     SUM(losses) as losses, SUM(draws) as draws, SUM(total_points) as total_points
                     FROM ttt_scores GROUP BY user_id ORDER BY total_points DESC LIMIT ?""", (limit,))
        return _rows_to_dicts(c, c.fetchall())
    except Exception as e:
        logger.error(f"Get global scores error: {e}")
        return []

# ── BUILD MARKUP ──

def build_lobby_markup(game_id, chat_id):
    mk = InlineKeyboardMarkup()
    mk.row(InlineKeyboardButton("🎮 Join Game", callback_data=f"ttt_join:{game_id}"))
    return mk

def build_board_markup(game_id, board):
    mk = InlineKeyboardMarkup()
    for i in range(0, 9, 3):
        row = []
        for j in range(3):
            pos = i + j
            symbol = board[pos]
            if symbol == 'X':
                emoji = '❌'
            elif symbol == 'O':
                emoji = '⭕'
            else:
                emoji = '✨'
            row.append(InlineKeyboardButton(emoji, callback_data=f"ttt_move:{game_id}:{pos}"))
        mk.row(*row)
    return mk

def build_game_over_markup(board):
    mk = InlineKeyboardMarkup()
    for i in range(0, 9, 3):
        row = []
        for j in range(3):
            pos = i + j
            symbol = board[pos]
            if symbol == 'X':
                emoji = '❌'
            elif symbol == 'O':
                emoji = '⭕'
            else:
                emoji = '✨'
            row.append(InlineKeyboardButton(emoji, callback_data=f"ttt_disabled:{pos}"))
        mk.row(*row)
    return mk

# ── DISPLAY HELPERS ──

def format_lobby_message(game):
    if not game:
        return "❌ Game not found. It may have expired — send /ttt to start a new one."
    p1 = game.get('player1_name') or 'Unknown'
    return (f"❌ Tic Tac Toe ⭕\n\n"
            f"⚔️ {p1} vs ⏳ Waiting...\n\n"
            f"✨ Click below to join and play!")

def format_game_message(game):
    if not game:
        return "❌ Game not found."
    p1 = game.get('player1_name') or 'Unknown'
    p2 = game.get('player2_name') or 'Unknown'
    turn_emoji = '❌' if game.get('current_turn') == 'X' else '⭕'
    turn_name = p1 if game.get('current_turn') == 'X' else p2
    return (f"❌ Tic Tac Toe ⭕\n\n"
            f"⚔️ {p1} vs ⚔️ {p2}\n\n"
            f"➡️ Turn: {turn_name} ({turn_emoji})")

def format_game_over_message(game):
    if not game:
        return "❌ Game not found."
    p1 = game.get('player1_name') or 'Unknown'
    p2 = game.get('player2_name') or 'Unknown'
    board = game.get('board', '.........')
    winner = game.get('winner')
    text = "❌ Tic Tac Toe ⭕\n\n"
    if winner == 'X':
        text += f"🏆 {p1} (❌) wins!\n\n⚔️ {p1} defeated ⚔️ {p2}\n\n"
    elif winner == 'O':
        text += f"🏆 {p2} (⭕) wins!\n\n⚔️ {p2} defeated ⚔️ {p1}\n\n"
    else:
        text += f"🤝 It's a Draw!\n\n⚔️ {p1} vs ⚔️ {p2}\n\n"
    text += display_board(board)
    return text

def get_ttt_board_display(board):
    return display_board(list(board))
