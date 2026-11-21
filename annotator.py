"""annotator.py - coach-style annotation of a single game. Stockfish supplies every fact (evaluations, best moves, lines, refutations, critical moments). An optional AI model (Gemini free tier by default) turns those facts into coach explanations. Every move the AI mentions is checked against the engine's facts; sentences that mention a move the engine never produced are removed. Without an AI key you still get engine-based notes. """
import base64
import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import chess
import chess.engine
import chess.pgn
import chess.svg

from chess_coach import (BLUNDER, GAP_CRITICAL, GAP_IMPORTANT, INACCURACY, MISTAKE, find_stockfish,
                         move_accuracy, parse_tc, win_pct)

MAX_MOMENTS = 14
LINE_PLIES = 8


# ----------------------------------------------------------------------------- parsing
class AnnotateError(Exception):
    pass


def parse_pgn(text):
    text = (text or "").strip()
    if not text:
        raise AnnotateError("Paste a PGN first.")
    # chess.com sometimes puts the moves on the same line as the last header
    text = re.sub(r'\]\s*1\.', ']\n\n1.', text, count=1)
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None or not list(game.mainline_moves()):
        raise AnnotateError("Couldn't read any moves from that PGN. Copy the full PGN including the moves.")
    if game.errors:
        raise AnnotateError(f"The PGN has an illegal or unreadable move: {game.errors[0]}")
    if game.headers.get("Variant", "Standard").lower() not in ("standard", "", "from position"):
        raise AnnotateError("Only standard chess games are supported.")
    return game


def detect_color(game, usernames):
    names = {u.strip().lower() for u in usernames if u.strip()}
    w, b = game.headers.get("White", "").lower(), game.headers.get("Black", "").lower()
    if w in names and b not in names:
        return chess.WHITE
    if b in names and w not in names:
        return chess.BLACK
    return None


# ----------------------------------------------------------------------------- engine facts
def _score_white(score):
    return score.pov(chess.WHITE).score(mate_score=10000)


def fmt_eval(cp):
    if cp is None:
        return "?"
    if abs(cp) >= 9000:
        n = 10000 - abs(cp)
        return f"#{n}" if cp > 0 else f"#-{n}"
    return f"{cp / 100:+.2f}"


def fmt_time(sec):
    if sec is None:
        return None
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def material(board):
    v = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
    return tuple(sum(len(board.pieces(pt, c)) * val for pt, val in v.items()) for c in (chess.WHITE, chess.BLACK))


def pinned_pieces(board):
    out = []
    for color in chess.COLORS:
        for sq in chess.SquareSet(board.occupied_co[color]):
            p = board.piece_at(sq)
            if p.piece_type != chess.KING and board.is_pinned(color, sq):
                out.append(f"{'White' if color else 'Black'} {chess.piece_name(p.piece_type)} on {chess.square_name(sq)}")
    return out


def hanging_pieces(board):
    """Pieces attacked by the opponent and not defended (simple check)."""
    out = []
    for color in chess.COLORS:
        for sq in chess.SquareSet(board.occupied_co[color]):
            p = board.piece_at(sq)
            if p.piece_type in (chess.KING, chess.PAWN):
                continue
            if board.is_attacked_by(not color, sq) and not board.is_attacked_by(color, sq):
                out.append(f"{'White' if color else 'Black'} {chess.piece_name(p.piece_type)} on {chess.square_name(sq)}")
    return out


def analyse_game(game, my_color, stockfish=None, depth=14, threads=2, hash_mb=128, progress=None):
    sf = find_stockfish(stockfish)
    engine = chess.engine.SimpleEngine.popen_uci(sf)
    engine.configure({"Threads": threads, "Hash": hash_mb})
    limit = chess.engine.Limit(depth=depth)
    base, inc = parse_tc(game.headers.get("TimeControl", "-"))
    inc = inc or 0
    nodes = list(game.mainline())
    board = game.board()
    last_clock = {chess.WHITE: None, chess.BLACK: None}
    plies = []
    try:
        for i, node in enumerate(nodes):
            move = node.move
            mover = board.turn
            infos = [x for x in engine.analyse(board, limit, multipv=2) if x.get("pv")]
            best = infos[0]
            wb = win_pct(best["score"], mover)
            ws = win_pct(infos[1]["score"], mover) if len(infos) > 1 else wb
            legal = board.legal_moves.count()
            if move == best["pv"][0]:
                after_score, refutation_pv = best["score"], None
            else:
                board.push(move)
                after = engine.analyse(board, limit)
                board.pop()
                after_score, refutation_pv = after["score"], after.get("pv", [])
            wp = win_pct(after_score, mover)
            loss = max(0.0, wb - wp)
            gap = max(0.0, wb - ws) if legal > 1 else 0.0
            clk = node.clock()
            spent = None
            if clk is not None and last_clock[mover] is not None:
                spent = max(0.0, last_clock[mover] + inc - clk)
            if clk is not None:
                last_clock[mover] = clk

            san = board.san(move)
            best_line = board.variation_san(best["pv"][:LINE_PLIES])
            refutation = None
            if refutation_pv:
                b2 = board.copy()
                b2.push(move)
                refutation = b2.variation_san(refutation_pv[:LINE_PLIES - 2]) if refutation_pv else None
            second = board.san(infos[1]["pv"][0]) if len(infos) > 1 else None
            mat_before = material(board)
            no, fen_before, best_san = board.fullmove_number, board.fen(), board.san(best["pv"][0])
            board.push(move)
            plies.append({
                "i": i, "id": f"{no}{'W' if mover else 'B'}", "no": no,
                "side": "W" if mover == chess.WHITE else "B", "mine": mover == my_color,
                "san": san, "uci": move.uci(), "fen_before": fen_before,
                "best_san": best_san,
                "best_uci": best["pv"][0].uci(), "best_line": best_line, "second": second,
                "refutation": refutation,
                "eval_best": _score_white(best["score"]), "eval_after": _score_white(after_score),
                "win_before": round(wb, 1), "loss": round(loss, 1), "gap": round(gap, 1),
                "forced": legal == 1,
                "crit": "forced" if legal == 1 else "critical" if gap >= GAP_CRITICAL else
                        "important" if gap >= GAP_IMPORTANT else "normal",
                "tag": "blunder" if loss >= BLUNDER else "mistake" if loss >= MISTAKE else
                       "inaccuracy" if loss >= INACCURACY else None,
                "material": mat_before, "spent": spent, "clock": clk,
                "pinned_after": pinned_pieces(board), "hanging_after": hanging_pieces(board),
            })
            if progress:
                progress(i + 1, len(nodes))
    finally:
        engine.quit()
    return {"plies": plies, "base": base, "inc": inc, "depth": depth,
            "has_clock": any(p["clock"] is not None for p in plies)}


# ----------------------------------------------------------------------------- key moments
def pick_moments(plies, max_n=MAX_MOMENTS):
    scored = []
    for p in plies:
        if p["forced"]:
            continue
        s = 0
        if p["mine"]:
            if p["tag"] == "blunder":
                s = 100 + p["loss"]
            elif p["tag"] == "mistake":
                s = 80 + p["loss"]
            elif p["tag"] == "inaccuracy" and p["crit"] != "normal":
                s = 50 + p["loss"]
            elif p["crit"] == "critical" and not p["tag"]:
                s = 45 + p["gap"]           # found the only move
            elif p["tag"] == "inaccuracy":
                s = 30 + p["loss"]
        else:
            if p["tag"] in ("blunder", "mistake"):
                s = 60 + p["loss"]
        if s:
            scored.append((s, p))
    scored.sort(key=lambda sp: -sp[0])
    chosen = sorted((p for _, p in scored[:max_n]), key=lambda p: p["i"])
    for p in chosen:
        if p["mine"] and p["tag"]:
            p["kind"] = f"your {p['tag']}"
        elif p["mine"]:
            p["kind"] = "you found the only good move"
        else:
            p["kind"] = f"opponent's {p['tag']}"
    return chosen


def game_story(plies):
    """Evaluation (White's view) every few moves, for the AI and the chart."""
    pts = []
    for p in plies:
        if p["side"] == "B" or p is plies[-1]:
            pts.append((p["no"], p["eval_after"]))
    return pts


def move_label(p):
    return f"{p['no']}{'.' if p['side'] == 'W' else '...'}{p['san']}"


# ----------------------------------------------------------------------------- AI prompt
SYSTEM_RULES = """You are an experienced chess coach writing annotations for your student, a club player rated about 2000. You receive engine-verified FACTS about one game. Write explanations a coach would give: the idea, the plan, the threat, the weakness, the tactic, and a short practical rule the student can reuse. STRICT RULES 1. Every evaluation and every move you mention must come from the FACTS. Never invent moves or variations. You may only mention moves that appear in the game or in the lines given for that moment. 2. Do not contradict the evaluations. If the engine says a move lost, it lost. 3. Address the student as "you". Be direct and concrete, 1-3 sentences per note, no filler, no praise inflation. 4. Prefer the "why" over listing moves: which piece, which square, which plan, what the opponent threatened. 5. If time-spent data is given, mention it only when it teaches something (long think in a quiet position, or a fast move in a critical position). 6. Output ONLY valid JSON with exactly this shape: {"summary": "3-4 sentences telling the story of the game", "lessons": ["rule 1 tied to a move", "rule 2", "rule 3"], "notes": [{"id": "<moment id exactly as given>", "text": "<your note>"}]} Write one note for every moment id given, in the same order."""


def build_prompt(game, my_color, facts, moments):
    h = game.headers
    plies = facts["plies"]
    moves_text = " ".join((f"{p['no']}." if p["side"] == "W" else "") + p["san"] for p in plies)
    story = ", ".join(f"after move {n}: {fmt_eval(cp)}" for n, cp in game_story(plies)[::3])
    lines = [
        f"GAME: {h.get('White', '?')} ({h.get('WhiteElo', '?')}) vs {h.get('Black', '?')} ({h.get('BlackElo', '?')}), "
        f"result {h.get('Result', '?')}, time control {h.get('TimeControl', '?')}.",
        f"The student played {'White' if my_color == chess.WHITE else 'Black'}.",
        f"Opening (if known): {h.get('Opening') or h.get('ECO') or 'not given'}.",
        f"MOVES: {moves_text}",
        f"EVALUATION TREND (White's view, pawns): {story}",
        "", "MOMENTS:",
    ]
    for p in moments:
        w, b = p["material"]
        parts = [
            f"- id {p['id']}: {move_label(p)} ({'student' if p['mine'] else 'opponent'} move) - {p['kind']}.",
            f" Position type: {p['crit']} (best move is {p['gap']:.0f} winning-% points better than the second best).",
            f" Eval with best play {fmt_eval(p['eval_best'])}, after the move played {fmt_eval(p['eval_after'])} "
            f"(winning chances lost: {p['loss']:.0f}%).",
            f" Engine best move {p['best_san']}; best line: {p['best_line']}.",
        ]
        if p["second"] and p["second"] != p["best_san"]:
            parts.append(f" Second-best move: {p['second']}.")
        if p["refutation"]:
            parts.append(f" Engine's best continuation after the move played: {p['refutation']}.")
        parts.append(f" Material before the move: White {w}, Black {b} (pawn=1, minor=3, rook=5, queen=9).")
        if p["pinned_after"]:
            parts.append(f" Pinned after the move: {', '.join(p['pinned_after'])}.")
        if p["hanging_after"]:
            parts.append(f" Undefended and attacked after the move: {', '.join(p['hanging_after'])}.")
        if p["spent"] is not None:
            parts.append(f" Time spent on this move: {fmt_time(p['spent'])}; clock left: {fmt_time(p['clock'])}.")
        lines += parts
    return "\n".join(lines)


# ----------------------------------------------------------------------------- AI providers
class AIError(Exception):
    pass


_GEMINI_MODEL_CACHE = {}


def _http_error(e):
    body = e.read().decode("utf-8", "ignore")[:400]
    if e.code == 429:
        return AIError("The AI's free quota is used up for now (HTTP 429). Engine notes were used instead; "
                       "try 'Write AI notes again' later.")
    if e.code in (401, 403):
        return AIError(f"The AI key was rejected (HTTP {e.code}). Check the key in your app's Secrets.")
    return AIError(f"AI request failed (HTTP {e.code}): {body}")


def _http_json(url, payload=None, headers=None, timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _http_error(e)
    except urllib.error.URLError as e:
        raise AIError(f"Couldn't reach the AI service: {e.reason}")
    except (TimeoutError, OSError) as e:
        raise AIError(f"The AI took too long to answer ({type(e).__name__}). Engine notes were used instead; "
                      "try 'Write AI notes again'.")
    except ValueError:
        raise AIError("The AI service sent an unreadable reply.")


def _gemini_stream(name, payload, key, read_timeout=150, total_limit=300):
    """Streamed generateContent: text arrives in pieces, so long answers don't hit a read timeout."""
    url = f"https://generativelanguage.googleapis.com/v1beta/{name}:streamGenerateContent?alt=sse"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    chunks, start = [], time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=read_timeout) as r:
            for raw in r:
                if time.monotonic() - start > total_limit:
                    raise AIError("The AI took more than 5 minutes. Engine notes were used instead.")
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except ValueError:
                    continue
                block = (obj.get("promptFeedback") or {}).get("blockReason")
                if block:
                    raise AIError(f"Gemini refused the request ({block}).")
                for cand in obj.get("candidates", []):
                    for part in (cand.get("content") or {}).get("parts", []):
                        if part.get("text") and not part.get("thought"):
                            chunks.append(part["text"])
    except urllib.error.HTTPError as e:
        raise _http_error(e)
    except urllib.error.URLError as e:
        raise AIError(f"Couldn't reach the AI service: {e.reason}")
    except (TimeoutError, OSError) as e:
        raise AIError(f"The AI took too long to answer ({type(e).__name__}). Engine notes were used instead; "
                      "try 'Write AI notes again', or set a faster model with AI_MODEL in Secrets.")
    text = "".join(chunks)
    if not text.strip():
        raise AIError("Gemini returned an empty answer. Try again.")
    return text


def _thinking_config(name):
    low = name.lower()
    if "gemini-2.5" in low:
        return {"thinkingBudget": 1024}
    if re.search(r"gemini-[3-9]", low):
        return {"thinkingLevel": "low"}
    return None


def pick_gemini_model(key):
    if key in _GEMINI_MODEL_CACHE:
        return _GEMINI_MODEL_CACHE[key]
    data = _http_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                      headers={"x-goog-api-key": key}, timeout=30)
    best, best_score = None, None
    for m in data.get("models", []):
        name = m.get("name", "")
        if "generateContent" not in m.get("supportedGenerationMethods", []) or "gemini" not in name:
            continue
        low = name.lower()
        if any(bad in low for bad in ("image", "tts", "audio", "live", "embed", "vision", "learnlm", "robotics")):
            continue
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", low)
        score = (
            "flash" in low,                       # fast and on the free tier
            "lite" not in low,                    # lite models explain less well
            not any(x in low for x in ("preview", "exp")),  # stable first
            float(ver.group(1)) if ver else 0.0,  # newest version
        )
        if best_score is None or score > best_score:
            best, best_score = name, score
    if not best:
        raise AIError("No usable Gemini model found for this key.")
    _GEMINI_MODEL_CACHE[key] = best
    return best


def call_ai(prompt, provider, key, model=None, base_url=None):
    provider = (provider or "gemini").lower()
    if provider == "gemini":
        name = model if (model or "").startswith("models/") else (f"models/{model}" if model else pick_gemini_model(key))
        gen = {"temperature": 0.4, "responseMimeType": "application/json", "maxOutputTokens": 8192}
        think = _thinking_config(name)
        if think:
            gen["thinkingConfig"] = think
        payload = {"systemInstruction": {"parts": [{"text": SYSTEM_RULES}]},
                   "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": gen}
        try:
            text = _gemini_stream(name, payload, key)
        except AIError as e:
            if think and "HTTP 400" in str(e) and "thinking" in str(e).lower():
                gen.pop("thinkingConfig")       # this model doesn't accept the setting; try plain
                text = _gemini_stream(name, payload, key)
            else:
                raise
        return text, name.replace("models/", "")
    if provider == "anthropic":
        model = model or "claude-sonnet-5-5"
        data = _http_json("https://api.anthropic.com/v1/messages",
                          {"model": model, "max_tokens": 6000, "system": SYSTEM_RULES,
                           "messages": [{"role": "user", "content": prompt}]},
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in data.get("content", [])), model
    if provider == "openai":
        if not base_url or not model:
            raise AIError("For an OpenAI-compatible provider set AI_BASE_URL and AI_MODEL in Secrets.")
        data = _http_json(base_url.rstrip("/") + "/chat/completions",
                          {"model": model, "temperature": 0.4,
                           "messages": [{"role": "system", "content": SYSTEM_RULES},
                                        {"role": "user", "content": prompt}]},
                          headers={"Authorization": f"Bearer {key}"})
        return data["choices"][0]["message"]["content"], model
    raise AIError(f"Unknown AI provider '{provider}'. Use gemini, anthropic or openai.")


def parse_ai_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise AIError("The AI didn't return JSON.")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"The AI returned broken JSON ({e}).")
    if not isinstance(data, dict):
        raise AIError("The AI returned an unexpected format.")
    return data


# ----------------------------------------------------------------------------- validation
SAN_TOKEN = re.compile(r"(?<![A-Za-z0-9])(O-O-O|O-O|[KQRBN][a-h]?[1-9]?x?[a-h][1-9]|[a-h]x[a-h][1-9](?:=[QRBN])?)[+#]?(?![A-Za-z0-9])")


def _norm(tok):
    return tok.rstrip("+#!?")


def allowed_moves(facts, moment=None):
    toks = set()
    for p in facts["plies"]:
        toks.add(_norm(p["san"]))
    srcs = [moment] if moment else facts.get("moments", [])
    for p in srcs:
        for line in (p.get("best_line"), p.get("refutation"), p.get("second"), p.get("best_san")):
            if line:
                toks.update(_norm(t) for t in SAN_TOKEN.findall(line))
    return toks


def clean_text(text, allowed):
    """Drop sentences that mention a move the engine never produced. Returns (text, dropped_count)."""
    if not isinstance(text, str):
        return "", 0
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])", text.strip())
    keep, dropped = [], 0
    for s in sentences:
        bad = [t for t in SAN_TOKEN.findall(s) if _norm(t) not in allowed]
        if bad:
            dropped += 1
        else:
            keep.append(s)
    return " ".join(keep).strip(), dropped


# ----------------------------------------------------------------------------- engine-only notes
def engine_note(p):
    ev = fmt_eval(p["eval_after"])
    if p["mine"] and p["tag"]:
        crit = " This was a critical moment: only one move kept the evaluation." if p["crit"] == "critical" else ""
        ref = f" After {p['san']} the engine continues {p['refutation']}." if p["refutation"] else ""
        return f"{p['tag'].capitalize()} [{ev}]. Better was {p['best_san']} ({p['best_line']}).{crit}{ref}"
    if p["mine"]:
        return f"Critical moment and you found the only good move [{ev}]. The next best move was clearly worse."
    return (f"Your opponent's {p['tag']} [{ev}]. {p['best_san']} was needed. "
            f"Engine line from here: {p['refutation'] or p['best_line']}.")


# ----------------------------------------------------------------------------- full pipeline
def annotate(game, my_color, facts, ai=None):
    """ai = dict(provider, key, model, base_url) or None. Returns dict with notes, summary, lessons, pgn, info."""
    moments = pick_moments(facts["plies"])
    facts["moments"] = moments
    notes = {p["id"]: engine_note(p) for p in moments}
    summary, lessons, info = "", [], {"ai_used": None, "ai_error": None, "dropped": 0}

    if ai and ai.get("key"):
        try:
            text, model_used = call_ai(build_prompt(game, my_color, facts, moments), ai.get("provider"),
                                       ai["key"], ai.get("model"), ai.get("base_url"))
            data = parse_ai_json(text)
            by_id = {p["id"]: p for p in moments}
            for n in data.get("notes", []):
                if not isinstance(n, dict) or n.get("id") not in by_id:
                    continue
                cleaned, dropped = clean_text(n.get("text", ""), allowed_moves(facts, by_id[n["id"]]))
                info["dropped"] += dropped
                if cleaned:
                    ev = fmt_eval(by_id[n["id"]]["eval_after"])
                    notes[n["id"]] = f"{cleaned} [{ev}]"
            allow_all = allowed_moves(facts)
            summary, d1 = clean_text(data.get("summary", ""), allow_all)
            info["dropped"] += d1
            for l in data.get("lessons", [])[:4]:
                c, d = clean_text(l, allow_all)
                info["dropped"] += d
                if c:
                    lessons.append(c)
            info["ai_used"] = model_used
        except AIError as e:
            info["ai_error"] = str(e)
        except Exception as e:   # never lose the engine analysis because of the AI step
            info["ai_error"] = f"AI step failed ({type(e).__name__}). Engine notes were used instead."

    if not summary:
        summary = auto_summary(facts, my_color)
    pgn = build_pgn(game, my_color, facts, moments, notes, summary, lessons, info)
    return {"moments": moments, "notes": notes, "summary": summary, "lessons": lessons, "pgn": pgn, "info": info}


def auto_summary(facts, my_color):
    mine = [p for p in facts["plies"] if p["mine"] and not p["forced"]]
    acc = sum(move_accuracy(p["loss"]) for p in mine) / max(1, len(mine))
    crit = [p for p in mine if p["crit"] == "critical"]
    crit_ok = sum(1 for p in crit if p["tag"] not in ("mistake", "blunder"))
    bl = [move_label(p) for p in mine if p["tag"] == "blunder"]
    mi = [move_label(p) for p in mine if p["tag"] == "mistake"]
    s = (f"Your accuracy: {acc:.0f}. You faced {len(crit)} critical positions and handled {crit_ok} of them correctly.")
    if bl:
        s += f" Blunders: {', '.join(bl)}."
    if mi:
        s += f" Mistakes: {', '.join(mi)}."
    return s


def build_pgn(game, my_color, facts, moments, notes, summary, lessons, info):
    nag = {"blunder": chess.pgn.NAG_BLUNDER, "mistake": chess.pgn.NAG_MISTAKE,
           "inaccuracy": chess.pgn.NAG_DUBIOUS_MOVE}
    out = chess.pgn.Game()
    for k, v in game.headers.items():
        out.headers[k] = v
    out.headers["Annotator"] = (f"Stockfish depth {facts['depth']}"
                                + (f" + {info['ai_used']}" if info.get("ai_used") else ""))
    head = summary
    if lessons:
        head += " LESSONS: " + " ".join(f"({i}) {l}" for i, l in enumerate(lessons, 1))
    out.comment = head
    by_i = {p["i"]: p for p in moments}
    board = game.board()
    node = out
    for p, mv in zip(facts["plies"], game.mainline_moves()):
        parent = node
        node = parent.add_variation(mv)
        if p["clock"] is not None:
            node.set_clock(p["clock"])
        if p["i"] in by_i:
            if p["tag"]:
                node.nags.add(nag[p["tag"]])
            elif p["mine"] and p["crit"] == "critical":
                node.nags.add(chess.pgn.NAG_GOOD_MOVE)
            text = notes.get(p["id"], "")
            if p["spent"] is not None:
                text += f" (time used: {fmt_time(p['spent'])})"
            node.comment = (node.comment + " " + text).strip() if node.comment else text
            if p["tag"] in ("mistake", "blunder") or (p["tag"] == "inaccuracy" and p["mine"]):
                b = board.copy()
                try:
                    line = [b.parse_san(s) for s in _strip_numbers(p["best_line"])]
                except ValueError:
                    line = []
                if line and line[0] != mv:
                    var = parent.add_variation(line[0])
                    cur = var
                    for m2 in line[1:]:
                        cur = cur.add_variation(m2)
                    parent.promote_to_main(mv)
        board.push(mv)
    exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
    return out.accept(exporter)


def _strip_numbers(line):
    out = []
    for tok in line.replace("...", ". ").split():
        if re.match(r"^\d+\.$", tok):
            continue
        out.append(tok)
    return out


# ----------------------------------------------------------------------------- visuals + export
def board_svg(p, my_color, size=360):
    board = chess.Board(p["fen_before"])
    played = chess.Move.from_uci(p["uci"])
    arrows = [chess.svg.Arrow(played.from_square, played.to_square, color="#d9534fcc")]
    if p["best_uci"] != p["uci"]:
        best = chess.Move.from_uci(p["best_uci"])
        arrows.append(chess.svg.Arrow(best.from_square, best.to_square, color="#2f8f5bcc"))
    return chess.svg.board(board, orientation=my_color, arrows=arrows, size=size)


def svg_img_tag(svg, max_width=360):
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f'<img src="data:image/svg+xml;base64,{b64}" style="width:100%;max-width:{max_width}px;border-radius:6px">'


def study_id_from(value):
    value = (value or "").strip()
    m = re.search(r"lichess\.org/study/([A-Za-z0-9]{8})", value)
    if m:
        return m.group(1)
    return value if re.fullmatch(r"[A-Za-z0-9]{8}", value) else None


def send_to_study(token, study, pgn, name, orientation):
    sid = study_id_from(study)
    if not sid:
        raise AnnotateError("Study not recognised. Paste the study link, e.g. https://lichess.org/study/AbCd1234")
    data = urllib.parse.urlencode({"pgn": pgn, "name": name[:80], "orientation": orientation}).encode()
    req = urllib.request.Request(f"https://lichess.org/api/study/{sid}/import-pgn", data=data, method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/x-www-form-urlencoded",
                                          "User-Agent": "chess_coach.py (personal training tool)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AnnotateError("Lichess refused: the token needs the 'Write private studies and broadcasts' "
                                "permission, and the study must be yours.")
        raise AnnotateError(f"Lichess returned HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")
    except urllib.error.URLError as e:
        raise AnnotateError(f"Couldn't reach Lichess: {e.reason}")
    chapters = resp.get("chapters") or []
    if resp.get("error") and not chapters:
        raise AnnotateError(f"Lichess: {resp['error']} (a study holds at most 64 chapters)")
    if chapters:
        return f"https://lichess.org/study/{sid}/{chapters[0]['id']}"
    return f"https://lichess.org/study/{sid}"