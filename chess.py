#!/usr/bin/env python3
""" chess_coach.py - a personal game analyser built around one question: "Am I spending my clock on the moves that actually matter?" It downloads your rated Lichess games (with clock times), runs Stockfish on every move you played, and measures how critical each position was: * normal - several good moves exist; play fast on general principles * important - one or two moves are clearly better; worth a real look * critical - only one move keeps the evaluation; this is where to think Then it produces: report.html dashboard: time vs criticality, mistakes by phase, by clock state, by opening, monthly trend, training log trainer.html "Critical or normal?" quiz built from YOUR positions puzzles_N.pgn your own mistakes as puzzles (import into a Lichess study) Commands python chess_coach.py fetch --user Narenzxy [--max 200] [--perf rapid,classical] python chess_coach.py analyse --user Narenzxy [--depth 14] [--stockfish PATH] python chess_coach.py report --user Narenzxy python chess_coach.py all --user Narenzxy (fetch + analyse + report) python chess_coach.py log calc 60 "Edouard ch.1, ex 1-6" Requires: Python 3.9+, `pip install chess`, and a Stockfish binary. """

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import random
import shutil
import statistics
import sys
import time
import urllib.parse
import urllib.request

try:
    import chess
    import chess.engine
    import chess.pgn
    import chess.svg
except ImportError:
    sys.exit("python-chess is missing. Install it with: pip install chess")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coach_data")
PGN_FILE = os.path.join(DATA_DIR, "games.pgn")
CACHE_FILE = os.path.join(DATA_DIR, "analysis.json")
LOG_FILE = os.path.join(DATA_DIR, "training_log.csv")

# Thresholds in win-percentage points (0-100 scale, same model Lichess uses)
INACCURACY, MISTAKE, BLUNDER = 5.0, 10.0, 15.0
GAP_IMPORTANT, GAP_CRITICAL = 5.0, 10.0
WEEKLY_TARGET_MIN = 600  # 10 hours

LOG_CATEGORIES = ["calc", "tactics", "endgame", "strategy", "openings", "games", "analysis", "other"]


# --------------------------------------------------------------------------- helpers
def win_pct(score, pov):
    """Stockfish score -> winning chance (0-100) for `pov`, Lichess formula."""
    cp = score.pov(pov).score(mate_score=10000)
    cp = max(-2000, min(2000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(loss):
    """Lichess per-move accuracy from win% loss."""
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * loss) - 3.1669))


def parse_tc(tc):
    try:
        base, inc = tc.split("+")
        return int(base), int(inc)
    except Exception:
        return None, None


def speed_of(base, inc):
    est = base + 40 * inc
    if est < 180:
        return "bullet"
    if est < 480:
        return "blitz"
    if est < 1500:
        return "rapid"
    return "classical"


def phase_of(board):
    if board.fullmove_number <= 12:
        return "opening"
    values = {chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
    npm = sum(len(board.pieces(pt, c)) * v for pt, v in values.items() for c in chess.COLORS)
    return "endgame" if npm <= 26 else "middlegame"


def clock_state(clock_before, base):
    if clock_before is None or not base:
        return "plenty"
    if clock_before < max(30, 0.10 * base):
        return "time trouble"
    if clock_before < 0.33 * base:
        return "low"
    return "plenty"


def find_stockfish(explicit):
    for cand in [explicit, os.environ.get("STOCKFISH"), shutil.which("stockfish"),
                 shutil.which("stockfish.exe"), "./stockfish", "./stockfish.exe"]:
        if cand and os.path.exists(cand):
            return cand
        if cand and shutil.which(cand):
            return shutil.which(cand)
    sys.exit("Stockfish not found. Download it from https://stockfishchess.org/download/ "
             "and pass --stockfish PATH (or set the STOCKFISH environment variable).")


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def game_id(headers):
    site = headers.get("Site", "")
    if "lichess.org/" in site:
        return site.rstrip("/").split("/")[-1][:8]
    return headers.get("GameId") or f"{headers.get('UTCDate','')}-{headers.get('White','')}-{headers.get('Black','')}-{headers.get('UTCTime','')}"


# --------------------------------------------------------------------------- fetch
def cmd_fetch(args):
    os.makedirs(DATA_DIR, exist_ok=True)
    params = {
        "max": args.max, "rated": "true", "clocks": "true", "opening": "true",
        "perfType": args.perf, "sort": "dateDesc",
    }
    url = f"https://lichess.org/api/games/user/{urllib.parse.quote(args.user)}?" + urllib.parse.urlencode(params)
    print(f"Downloading up to {args.max} {args.perf} games for {args.user} ...")
    req = urllib.request.Request(url, headers={"Accept": "application/x-chess-pgn",
                                               "User-Agent": "chess_coach.py (personal training tool)"})
    if args.token:
        req.add_header("Authorization", f"Bearer {args.token}")
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read().decode("utf-8")
    with open(PGN_FILE, "w", encoding="utf-8") as f:
        f.write(data)
    n = data.count("[Event ")
    print(f"Saved {n} games to {PGN_FILE}")


# --------------------------------------------------------------------------- analyse
def analyse_game(engine, game, user, limit):
    h = game.headers
    white, black = h.get("White", ""), h.get("Black", "")
    if user.lower() == white.lower():
        color = chess.WHITE
    elif user.lower() == black.lower():
        color = chess.BLACK
    else:
        return None
    base, inc = parse_tc(h.get("TimeControl", "-"))
    if base is None:
        return None

    result = h.get("Result", "*")
    if result == "1/2-1/2":
        outcome = "draw"
    elif (result == "1-0") == (color == chess.WHITE) and result != "*":
        outcome = "win"
    elif result in ("1-0", "0-1"):
        outcome = "loss"
    else:
        return None

    my_elo = h.get("WhiteElo" if color == chess.WHITE else "BlackElo", "?")
    opp_elo = h.get("BlackElo" if color == chess.WHITE else "WhiteElo", "?")
    site = h.get("Site", "")
    rec = {
        "id": game_id(h), "url": site, "date": h.get("UTCDate", h.get("Date", "")).replace(".", "-"),
        "color": "white" if color == chess.WHITE else "black",
        "opponent": black if color == chess.WHITE else white,
        "my_elo": my_elo, "opp_elo": opp_elo, "outcome": outcome,
        "termination": h.get("Termination", ""), "base": base, "inc": inc,
        "speed": speed_of(base, inc), "opening": h.get("Opening", "?"), "eco": h.get("ECO", "?"),
        "moves": [],
    }

    board = game.board()
    last_own_clock = None
    prev_move = None
    ply = 0
    for node in game.mainline():
        move = node.move
        ply += 1
        mover = board.turn
        if mover == color:
            clk = node.clock()
            spent = None
            if clk is not None and last_own_clock is not None:
                spent = max(0.0, last_own_clock + inc - clk)
            clock_before = last_own_clock
            legal = board.legal_moves.count()
            m = {
                "ply": ply, "no": board.fullmove_number, "san": board.san(move),
                "fen": board.fen(), "last": prev_move.uci() if prev_move else None,
                "spent": spent, "clock_before": clock_before, "phase": phase_of(board),
                "cstate": clock_state(clock_before, base),
            }
            if legal == 1:
                m.update(crit="forced", loss=0.0, gap=0.0, tag=None, best=m["san"], line="")
            else:
                infos = engine.analyse(board, limit, multipv=2)
                infos = [i for i in infos if i.get("pv")]
                best = infos[0]
                best_move = best["pv"][0]
                wb = win_pct(best["score"], mover)
                ws = win_pct(infos[1]["score"], mover) if len(infos) > 1 else wb
                if move == best_move:
                    wp = wb
                elif len(infos) > 1 and move == infos[1]["pv"][0]:
                    wp = ws
                else:
                    board.push(move)
                    after = engine.analyse(board, limit)
                    board.pop()
                    wp = win_pct(after["score"], mover)
                loss = max(0.0, wb - wp)
                gap = max(0.0, wb - ws)
                crit = "critical" if gap >= GAP_CRITICAL else "important" if gap >= GAP_IMPORTANT else "normal"
                tag = "blunder" if loss >= BLUNDER else "mistake" if loss >= MISTAKE else \
                      "inaccuracy" if loss >= INACCURACY else None
                m.update(crit=crit, loss=round(loss, 2), gap=round(gap, 2), tag=tag,
                         win_before=round(wb, 1), best=board.san(best_move),
                         line=board.variation_san(best["pv"][:6]))
            rec["moves"].append(m)
            if clk is not None:
                last_own_clock = clk
        board.push(move)
        prev_move = move
    return rec


def cmd_analyse(args):
    if not os.path.exists(PGN_FILE):
        sys.exit(f"No games found at {PGN_FILE}. Run the fetch command first.")
    cache = load_cache()
    sf = find_stockfish(args.stockfish)
    engine = chess.engine.SimpleEngine.popen_uci(sf)
    engine.configure({"Threads": args.threads, "Hash": 256})
    limit = chess.engine.Limit(depth=args.depth)

    games = []
    with open(PGN_FILE, encoding="utf-8") as f:
        while True:
            g = chess.pgn.read_game(f)
            if g is None:
                break
            games.append(g)
    todo = [g for g in games if game_id(g.headers) not in cache]
    print(f"{len(games)} games in file, {len(todo)} new to analyse (depth {args.depth}).")
    t0 = time.time()
    try:
        for i, g in enumerate(todo, 1):
            rec = analyse_game(engine, g, args.user, limit)
            gid = game_id(g.headers)
            cache[gid] = rec if rec else {"skip": True}
            if i % 5 == 0 or i == len(todo):
                save_cache(cache)
                rate = (time.time() - t0) / i
                print(f" {i}/{len(todo)} done (~{rate * (len(todo) - i) / 60:.1f} min left)")
    except KeyboardInterrupt:
        print("\nInterrupted - progress saved, run analyse again to continue.")
    finally:
        save_cache(cache)
        engine.quit()


# --------------------------------------------------------------------------- report helpers
def pct(a, b):
    return 100.0 * a / b if b else 0.0


def fmt_s(sec):
    if sec is None:
        return "-"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}" if sec >= 60 else f"{sec}s"


def move_link(g, m):
    if "lichess.org" not in g["url"]:
        return html.escape(f"{m['no']}. {m['san']}")
    return (f'<a href="{g["url"]}/{g["color"]}#{m["ply"] - 1}" target="_blank">'
            f'{m["no"]}{"." if g["color"] == "white" else "..."} {html.escape(m["san"])}</a>')


def bar(value, vmax, cls=""):
    w = 0 if not vmax else max(0, min(100, 100 * value / vmax))
    return f'<div class="bar {cls}"><span style="width:{w:.0f}%"></span></div>'


def errors_row(moves):
    n = len(moves)
    c = {t: sum(1 for m in moves if m["tag"] == t) for t in ("inaccuracy", "mistake", "blunder")}
    acc = statistics.mean(move_accuracy(m["loss"]) for m in moves) if moves else 0
    return n, c, acc


def load_log():
    rows = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({"date": dt.date.fromisoformat(r["date"]), "cat": r["category"],
                                 "min": int(r["minutes"]), "note": r.get("note", "")})
                except Exception:
                    pass
    return rows


# --------------------------------------------------------------------------- report
def cmd_report(args):
    cache = load_cache()
    speeds = set(args.speeds.split(","))
    games = [g for g in cache.values() if not g.get("skip") and g["speed"] in speeds]
    games.sort(key=lambda g: g["date"])
    if not games:
        sys.exit("No analysed games for those speeds yet. Run fetch and analyse first.")
    os.makedirs(DATA_DIR, exist_ok=True)

    all_moves = [(g, m) for g in games for m in g["moves"] if m["crit"] != "forced"]
    mv = [m for _, m in all_moves]
    timed = [(g, m) for g, m in all_moves if m["spent"] is not None and m["phase"] != "opening"]

    # ---- summary
    n = len(games)
    w = sum(g["outcome"] == "win" for g in games)
    d = sum(g["outcome"] == "draw" for g in games)
    l = n - w - d
    _, ec, acc = errors_row(mv)
    tt_games = sum(any(m["cstate"] == "time trouble" for m in g["moves"]) for g in games)
    time_losses = sum(g["outcome"] == "loss" and "time" in g["termination"].lower() for g in games)

    cards = [
        ("Games", f"{n}", f"+{w} ={d} -{l} ({pct(w + d / 2, n):.0f}%)"),
        ("Accuracy", f"{acc:.1f}", "average per move (Lichess formula)"),
        ("Blunders / game", f"{ec['blunder'] / n:.2f}", f"mistakes / game {ec['mistake'] / n:.2f}"),
        ("Reached time trouble", f"{pct(tt_games, n):.0f}%", "of games (under 10% of base time)"),
        ("Lost on time", f"{time_losses}", f"{pct(time_losses, l):.0f}% of your losses"),
    ]

    # ---- time vs criticality
    crit_rows = []
    total_time = sum(m["spent"] for _, m in timed) or 1
    groups = {}
    for c in ("normal", "important", "critical"):
        ms = [m for _, m in timed if m["crit"] == c]
        groups[c] = ms
        if not ms:
            crit_rows.append((c, 0, 0, 0, 0, 0))
            continue
        spent = [m["spent"] for m in ms]
        err = sum(m["tag"] in ("mistake", "blunder") for m in ms)
        crit_rows.append((c, len(ms), statistics.mean(spent), statistics.median(spent),
                          pct(sum(spent), total_time), pct(err, len(ms))))
    mean_norm = crit_rows[0][2] or 0.0001
    mean_crit = crit_rows[2][2]
    ratio = mean_crit / mean_norm if mean_norm else 0
    crit_ms = groups["critical"]
    rushed_share = pct(sum(1 for m in crit_ms if m["spent"] < 15), len(crit_ms))

    insights = []
    insights.append(f"<b>{crit_rows[0][4]:.0f}%</b> of your thinking time (after the opening) goes into "
                    f"<b>normal</b> positions, where several moves were fine.")
    insights.append(f"In <b>critical</b> positions you think <b>{fmt_s(mean_crit)}</b> on average, versus "
                    f"<b>{fmt_s(crit_rows[0][2])}</b> in normal ones: a ratio of <b>{ratio:.1f}x</b>. "
                    f"Strong players typically show a clear gap here; aim for 3x or more.")
    insights.append(f"<b>{rushed_share:.0f}%</b> of critical moments got less than 15 seconds.")
    insights.append(f"Your mistake rate is <b>{crit_rows[2][5]:.0f}%</b> in critical positions and "
                    f"<b>{crit_rows[0][5]:.0f}%</b> in normal ones.")

    by_base = {}
    for g, m in timed:
        by_base.setdefault(g["base"], []).append(m)

    overthinks = sorted([(g, m) for g, m in timed if m["crit"] == "normal"],
                        key=lambda gm: -gm[1]["spent"] / max(gm[0]["base"], 1))[:15]
    rushed = sorted([(g, m) for g, m in timed if m["crit"] in ("critical", "important")
                     and m["tag"] in ("mistake", "blunder") and m["spent"] < max(20, 0.02 * g["base"])],
                    key=lambda gm: -gm[1]["loss"])[:15]

    # ---- by phase / clock state
    def table_by(key, order):
        rows = []
        for k in order:
            ms = [m for m in mv if m[key] == k]
            if not ms:
                continue
            cnt, c, a = errors_row(ms)
            rows.append((k, cnt, a, pct(c["inaccuracy"], cnt), pct(c["mistake"], cnt), pct(c["blunder"], cnt)))
        return rows

    phase_rows = table_by("phase", ["opening", "middlegame", "endgame"])
    clock_rows = table_by("cstate", ["plenty", "low", "time trouble"])

    # ---- openings
    op = {}
    for g in games:
        fam = g["opening"].split(":")[0].strip()
        key = (g["color"], fam)
        op.setdefault(key, []).append(g)
    op_rows = []
    for (color, fam), gs in op.items():
        sc = sum(1 if g["outcome"] == "win" else 0.5 if g["outcome"] == "draw" else 0 for g in gs)
        om = [m for g in gs for m in g["moves"] if m["phase"] == "opening" and m["crit"] != "forced"]
        otime = [m["spent"] for g in gs for m in g["moves"] if m["phase"] == "opening" and m["spent"] is not None]
        base_avg = statistics.mean(g["base"] for g in gs)
        op_rows.append((color, fam, len(gs), pct(sc, len(gs)),
                        statistics.mean(m["loss"] for m in om) if om else 0,
                        pct(sum(otime), base_avg * len(gs)) if otime else 0))
    op_rows.sort(key=lambda r: -r[2])

    # ---- monthly trend
    months = {}
    for g in games:
        months.setdefault(g["date"][:7], []).append(g)
    trend = []
    for mo in sorted(months):
        gs = months[mo]
        ms = [m for g in gs for m in g["moves"] if m["crit"] != "forced"]
        tms = [m for g in gs for m in g["moves"] if m["spent"] is not None and m["phase"] != "opening"]
        cn = [m["spent"] for m in tms if m["crit"] == "normal"]
        cc = [m["spent"] for m in tms if m["crit"] == "critical"]
        r = (statistics.mean(cc) / statistics.mean(cn)) if cn and cc and statistics.mean(cn) else 0
        trend.append((mo, len(gs), statistics.mean(move_accuracy(m["loss"]) for m in ms) if ms else 0,
                      sum(m["tag"] == "blunder" for m in ms) / len(gs),
                      pct(sum(any(m["cstate"] == "time trouble" for m in g["moves"]) for g in gs), len(gs)), r))

    # ---- training log
    log = load_log()
    log_html = "<p class='muted'>No training logged yet. After each session run e.g. " \
               "<code>python chess_coach.py log calc 60 \"Edouard ex 1-6\"</code></p>"
    if log:
        today = dt.date.today()
        monday = today - dt.timedelta(days=today.weekday())
        weeks = []
        for k in range(7, -1, -1):
            start = monday - dt.timedelta(weeks=k)
            rows = [r for r in log if start <= r["date"] < start + dt.timedelta(days=7)]
            per = {c: sum(r["min"] for r in rows if r["cat"] == c) for c in LOG_CATEGORIES}
            weeks.append((start, sum(per.values()), per))
        days = {r["date"] for r in log}
        streak, day = 0, today if today in days else today - dt.timedelta(days=1)
        while day in days:
            streak += 1
            day -= dt.timedelta(days=1)
        head = "".join(f"<th>{c}</th>" for c in LOG_CATEGORIES)
        body = ""
        for start, tot, per in weeks:
            cells = "".join(f"<td>{per[c] or ''}</td>" for c in LOG_CATEGORIES)
            body += (f"<tr><td>{start:%d %b}</td><td>{tot / 60:.1f} h</td>"
                     f"<td style='width:160px'>{bar(tot, WEEKLY_TARGET_MIN, 'good' if tot >= WEEKLY_TARGET_MIN else '')}</td>{cells}</tr>")
        log_html = (f"<p>Current streak: <b>{streak} day{'s' if streak != 1 else ''}</b>. "
                    f"Weekly target: {WEEKLY_TARGET_MIN / 60:.0f} h.</p>"
                    f"<table><tr><th>Week of</th><th>Total</th><th>vs target</th>{head}</tr>{body}</table>")

    # ---- assemble HTML
    def tr(cells):
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    cards_html = "".join(f"<div class='card'><div class='lbl'>{a}</div><div class='val'>{b}</div>"
                         f"<div class='sub'>{c}</div></div>" for a, b, c in cards)
    maxshare = max(r[4] for r in crit_rows) or 1
    crit_html = "".join(tr([f"<b>{c}</b>", cnt, fmt_s(mean), fmt_s(med), f"{share:.0f}%" + bar(share, maxshare),
                            f"{err:.1f}%"]) for c, cnt, mean, med, share, err in crit_rows)
    over_html = "".join(tr([move_link(g, m), g["date"], f"{fmt_s(m['spent'])}",
                            f"{pct(m['spent'], g['base']):.0f}% of base", fmt_s(m["clock_before"]),
                            html.escape(m["best"])]) for g, m in overthinks) or tr(["none", "", "", "", "", ""])
    rush_html = "".join(tr([move_link(g, m), g["date"], m["crit"], fmt_s(m["spent"]), m["tag"],
                            f"-{m['loss']:.0f}%", html.escape(m["line"])]) for g, m in rushed) or tr(["none"] * 7)

    def err_table(rows):
        return "".join(tr([f"<b>{k}</b>", cnt, f"{a:.1f}", f"{i:.1f}", f"{mi:.1f}", f"{b:.1f}" + bar(b, 6, "bad")])
                       for k, cnt, a, i, mi, b in rows)

    op_html = "".join(tr([c, html.escape(f), cnt, f"{s:.0f}%", f"{lo:.1f}", f"{t:.0f}%"])
                      for c, f, cnt, s, lo, t in op_rows[:20])
    trend_html = "".join(tr([mo, cnt, f"{a:.1f}", f"{b:.2f}", f"{t:.0f}%", f"{r:.1f}x" + bar(r, 4, "good")])
                         for mo, cnt, a, b, t, r in trend)

    page = REPORT_TEMPLATE
    for k, v in {
        "{{USER}}": html.escape(args.user), "{{GENERATED}}": dt.datetime.now().strftime("%d %b %Y %H:%M"),
        "{{SPEEDS}}": ", ".join(sorted(speeds)), "{{CARDS}}": cards_html,
        "{{INSIGHTS}}": "".join(f"<li>{i}</li>" for i in insights), "{{CRIT}}": crit_html,
        "{{OVER}}": over_html, "{{RUSH}}": rush_html, "{{PHASE}}": err_table(phase_rows),
        "{{CLOCK}}": err_table(clock_rows), "{{OPEN}}": op_html, "{{TREND}}": trend_html, "{{LOG}}": log_html,
    }.items():
        page = page.replace(k, str(v))
    out = os.path.join(DATA_DIR, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Report: {out}")

    write_trainer(games, args.user)
    write_puzzles(games)


# --------------------------------------------------------------------------- trainer + puzzles
def board_svg(m, color):
    board = chess.Board(m["fen"])
    last = chess.Move.from_uci(m["last"]) if m.get("last") else None
    return chess.svg.board(board, orientation=chess.WHITE if color == "white" else chess.BLACK,
                           lastmove=last, size=380, coordinates=True)


def write_trainer(games, user, n_items=60):
    rnd = random.Random(42)
    pool = [(g, m) for g in games for m in g["moves"] if m["phase"] != "opening" and m["crit"] != "forced"]
    crit = [gm for gm in pool if gm[1]["crit"] == "critical"]
    norm = [gm for gm in pool if gm[1]["crit"] == "normal" and 25 <= gm[1].get("win_before", 50) <= 75]
    # prefer critical moments you got wrong, then the rest
    crit.sort(key=lambda gm: (gm[1]["tag"] not in ("mistake", "blunder"), rnd.random()))
    half = n_items // 2
    picked = crit[:half] + rnd.sample(norm, min(len(norm), n_items - min(half, len(crit))))
    rnd.shuffle(picked)
    items = []
    for g, m in picked:
        items.append({
            "svg": board_svg(m, g["color"]), "answer": m["crit"], "best": m["best"], "line": m["line"],
            "played": m["san"], "loss": m["loss"], "spent": fmt_s(m["spent"]), "tag": m["tag"] or "fine",
            "toMove": g["color"], "game": f"{g['date']} vs {g['opponent']}",
            "url": f"{g['url']}/{g['color']}#{m['ply'] - 1}" if "lichess.org" in g["url"] else "",
        })
    page = TRAINER_TEMPLATE.replace("{{DATA}}", json.dumps(items)).replace("{{USER}}", html.escape(user))
    out = os.path.join(DATA_DIR, "trainer.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Trainer: {out} ({len(items)} positions)")


def write_puzzles(games, chunk=64):
    picks = [(g, m) for g in games for m in g["moves"]
             if m["tag"] in ("mistake", "blunder") and m["gap"] >= GAP_IMPORTANT and m.get("line")]
    picks.sort(key=lambda gm: -gm[1]["loss"])
    for old in [f for f in os.listdir(DATA_DIR) if f.startswith("puzzles_") and f.endswith(".pgn")]:
        os.remove(os.path.join(DATA_DIR, old))
    files = 0
    for start in range(0, len(picks), chunk):
        files += 1
        path = os.path.join(DATA_DIR, f"puzzles_{files}.pgn")
        with open(path, "w", encoding="utf-8") as f:
            for g, m in picks[start:start + chunk]:
                board = chess.Board(m["fen"])
                pg = chess.pgn.Game()
                pg.setup(board)
                pg.headers["Event"] = f"{m['phase'].title()} {m['tag']} vs {g['opponent']} ({g['date']})"
                pg.headers["White"] = "You" if g["color"] == "white" else g["opponent"]
                pg.headers["Black"] = "You" if g["color"] == "black" else g["opponent"]
                pg.headers["Result"] = "*"
                pg.comment = (f"{'White' if board.turn else 'Black'} to move. In the game you played "
                              f"{m['san']} after {fmt_s(m['spent'])} and lost {m['loss']:.0f}% winning chances.")
                node = pg
                for san in m["line"].replace("...", ". ").split():
                    if san[0].isdigit():
                        continue
                    try:
                        mv = board.parse_san(san)
                    except ValueError:
                        break
                    node = node.add_variation(mv)
                    board.push(mv)
                print(pg, file=f, end="\n\n")
    print(f"Puzzles: {len(picks)} positions in {files} file(s) puzzles_N.pgn (64 per file = one Lichess study)")


# --------------------------------------------------------------------------- log
def cmd_log(args):
    os.makedirs(DATA_DIR, exist_ok=True)
    if args.category not in LOG_CATEGORIES:
        sys.exit(f"Category must be one of: {', '.join(LOG_CATEGORIES)}")
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        if new:
            wr.writerow(["date", "category", "minutes", "note"])
        wr.writerow([(args.date or dt.date.today().isoformat()), args.category, args.minutes, args.note])
    rows = load_log()
    today = dt.date.today()
    week = sum(r["min"] for r in rows if r["date"] >= today - dt.timedelta(days=today.weekday()))
    print(f"Logged {args.minutes} min of {args.category}. This week: {week / 60:.1f} h / {WEEKLY_TARGET_MIN / 60:.0f} h")


# --------------------------------------------------------------------------- templates
REPORT_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"> <meta name="viewport" content="width=device-width,initial-scale=1"><title>Chess report - {{USER}}</title> <style> :root{--bg:#f7f6f3;--fg:#1d1d1b;--muted:#6b6a66;--card:#fff;--line:#e4e2dc;--acc:#2f6f4f;--bad:#b4452f} @media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#9a9993;--card:#201f1d;--line:#34332f;--acc:#6fbf94;--bad:#e07a62}} *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif} main{max-width:1080px;margin:0 auto;padding:32px 20px 64px}h1{font-size:26px;margin:0}h2{font-size:18px;margin:40px 0 6px} .muted,.sub{color:var(--muted)}.sub{font-size:12.5px}p.lead{color:var(--muted);margin:4px 0 0} .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:24px} .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px} .lbl{font-size:12.5px;color:var(--muted)}.val{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums} table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px} th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:middle;font-variant-numeric:tabular-nums} th{font-weight:600;font-size:12.5px;color:var(--muted)}tr:last-child td{border-bottom:none} .bar{height:6px;background:var(--line);border-radius:3px;margin-top:4px;min-width:60px}.bar span{display:block;height:100%;background:var(--acc);border-radius:3px} .bar.bad span{background:var(--bad)}a{color:var(--acc)}code{background:var(--line);padding:1px 5px;border-radius:4px} ul.ins{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--acc);border-radius:10px;padding:14px 18px 14px 34px;margin:10px 0 14px} ul.ins li{margin:4px 0}.note{font-size:13px;color:var(--muted);margin:6px 0 10px} </style></head><body><main> <h1>Chess report: {{USER}}</h1><p class="lead">Generated {{GENERATED}} &middot; speeds: {{SPEEDS}}</p> <div class="cards">{{CARDS}}</div> <h2>1. Where your clock goes</h2> <p class="note">Each of your moves after move 12 is classed by how much the best move beat the second best: <b>normal</b> (under 5% winning chances, several moves are fine), <b>important</b> (5&ndash;10%), <b>critical</b> (10%+, essentially one right move).</p> <ul class="ins">{{INSIGHTS}}</ul> <table><tr><th>Position type</th><th>Moves</th><th>Avg think</th><th>Median</th><th>Share of your time</th><th>Mistake + blunder rate</th></tr>{{CRIT}}</table> <h2>2. Overthinking: long thinks in normal positions</h2> <p class="note">Positions where several moves were fine, but you spent a big part of your clock. Open them and ask: what told me this was critical, and what should have told me it wasn't?</p> <table><tr><th>Move</th><th>Date</th><th>Time spent</th><th>Share</th><th>Clock before</th><th>Engine move</th></tr>{{OVER}}</table> <h2>3. Rushed critical moments that cost you</h2> <p class="note">Critical or important positions where you moved quickly and made a mistake or blunder. These are the moments your time was meant for.</p> <table><tr><th>Move</th><th>Date</th><th>Type</th><th>Time spent</th><th>Result</th><th>Win% lost</th><th>Best line</th></tr>{{RUSH}}</table> <h2>4. Mistakes by game phase</h2> <p class="note">Rates are per 100 moves.</p> <table><tr><th>Phase</th><th>Moves</th><th>Accuracy</th><th>Inaccuracies</th><th>Mistakes</th><th>Blunders</th></tr>{{PHASE}}</table> <h2>5. Mistakes by clock state</h2> <p class="note">Plenty: over a third of your base time left. Low: 10&ndash;33%. Time trouble: under 10%.</p> <table><tr><th>Clock</th><th>Moves</th><th>Accuracy</th><th>Inaccuracies</th><th>Mistakes</th><th>Blunders</th></tr>{{CLOCK}}</table> <h2>6. Openings</h2> <p class="note">Opening time = share of your base time used in moves 1&ndash;12. High numbers mean you are thinking in positions you should know.</p> <table><tr><th>Colour</th><th>Opening</th><th>Games</th><th>Score</th><th>Avg loss / move (moves 1&ndash;12)</th><th>Opening time</th></tr>{{OPEN}}</table> <h2>7. Monthly trend</h2> <p class="note">Signs of progress: accuracy up, blunders down, fewer time-trouble games, and a higher critical/normal thinking ratio.</p> <table><tr><th>Month</th><th>Games</th><th>Accuracy</th><th>Blunders / game</th><th>Time-trouble games</th><th>Critical / normal think ratio</th></tr>{{TREND}}</table> <h2>8. Training log</h2> {{LOG}} </main></body></html>"""

TRAINER_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"> <meta name="viewport" content="width=device-width,initial-scale=1"><title>Critical or normal? - {{USER}}</title> <style> :root{--bg:#f7f6f3;--fg:#1d1d1b;--muted:#6b6a66;--card:#fff;--line:#e4e2dc;--acc:#2f6f4f;--bad:#b4452f} @media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#9a9993;--card:#201f1d;--line:#34332f;--acc:#6fbf94;--bad:#e07a62}} body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif} main{max-width:820px;margin:0 auto;padding:28px 18px 60px}h1{font-size:22px;margin:0 0 4px}.muted{color:var(--muted)} .wrap{display:flex;gap:22px;flex-wrap:wrap;margin-top:18px}.board svg{width:380px;max-width:92vw;height:auto;border-radius:6px} .side{flex:1;min-width:250px}.btns{display:flex;gap:10px;margin:14px 0} button{font:inherit;padding:10px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer} button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}button:disabled{opacity:.45;cursor:default} .timer{font-size:28px;font-weight:650;font-variant-numeric:tabular-nums}.res{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:none} .ok{color:var(--acc);font-weight:650}.no{color:var(--bad);font-weight:650}a{color:var(--acc)} </style></head><body><main> <h1>Critical or normal?</h1> <p class="muted">Positions from your own games. You have 10 seconds per position: decide whether it needs deep calculation (critical) or can be played on principles (normal). Train the judgement, not the move.</p> <div class="wrap"><div class="board" id="board"></div> <div class="side"><div class="muted" id="meta"></div><div class="timer" id="timer">10</div> <div class="btns"><button id="bc">Critical: calculate</button><button id="bn">Normal: play fast</button></div> <div class="res" id="res"></div> <div class="btns"><button class="primary" id="next" disabled>Next position</button></div> <div class="muted" id="score"></div></div></div> </main><script> const D={{DATA}};let i=0,right=0,done=0,t=10,tick=null,answered=false; const $=id=>document.getElementById(id); function show(){if(i>=D.length){$('board').innerHTML='';$('meta').textContent='';$('timer').textContent=''; $('res').style.display='block';$('res').innerHTML='<b>Finished.</b> '+right+' / '+done+' correct. Reload to go again.'; $('bc').disabled=$('bn').disabled=$('next').disabled=true;return;} const p=D[i];answered=false;$('board').innerHTML=p.svg;$('meta').textContent=(p.toMove==='white'?'White':'Black')+' to move · '+p.game+' · '+(i+1)+'/'+D.length; $('res').style.display='none';$('bc').disabled=$('bn').disabled=false;$('next').disabled=true;t=10;$('timer').textContent=t; clearInterval(tick);tick=setInterval(()=>{t--;$('timer').textContent=Math.max(t,0);if(t<=0){answer(null)}},1000);} function answer(g){if(answered)return;answered=true;clearInterval(tick);const p=D[i];const truth=p.answer==='critical'?'critical':'normal'; const ok=g===truth;done++;if(ok)right++; $('res').style.display='block'; $('res').innerHTML=(g===null?'<span class="no">Time up.</span> ':ok?'<span class="ok">Correct.</span> ':'<span class="no">Not quite.</span> ')+ 'This position was <b>'+truth+'</b>.<br>Engine: <b>'+p.best+'</b> &nbsp;<span class="muted">'+p.line+'</span><br>'+ 'In the game you played <b>'+p.played+'</b> after '+p.spent+' ('+p.tag+(p.loss>=5?', -'+p.loss.toFixed(0)+'%':'')+').'+ (p.url?' <a href="'+p.url+'" target="_blank">Open game</a>':''); $('bc').disabled=$('bn').disabled=true;$('next').disabled=false;$('score').textContent='Score: '+right+' / '+done;} $('bc').onclick=()=>answer('critical');$('bn').onclick=()=>answer('normal');$('next').onclick=()=>{i++;show()};show(); </script></body></html>"""


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Personal chess game analyser (Lichess + Stockfish)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--user", required=True, help="your Lichess username")

    p = sub.add_parser("fetch"); common(p)
    p.add_argument("--max", type=int, default=200)
    p.add_argument("--perf", default="rapid,classical", help="comma list: blitz,rapid,classical")
    p.add_argument("--token", default=None, help="optional Lichess API token (faster downloads)")

    for name in ("analyse", "all"):
        p = sub.add_parser(name); common(p)
        p.add_argument("--depth", type=int, default=14)
        p.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) - 1))
        p.add_argument("--stockfish", default=None)
        p.add_argument("--speeds", default="rapid,classical")
        if name == "all":
            p.add_argument("--max", type=int, default=200)
            p.add_argument("--perf", default="rapid,classical")
            p.add_argument("--token", default=None)

    p = sub.add_parser("report"); common(p)
    p.add_argument("--speeds", default="rapid,classical", help="which speeds to include")

    p = sub.add_parser("log")
    p.add_argument("category", help="/".join(LOG_CATEGORIES))
    p.add_argument("minutes", type=int)
    p.add_argument("note", nargs="?", default="")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default today)")

    args = ap.parse_args()
    if args.cmd == "fetch":
        cmd_fetch(args)
    elif args.cmd == "analyse":
        cmd_analyse(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "log":
        cmd_log(args)
    elif args.cmd == "all":
        cmd_fetch(args)
        cmd_analyse(args)
        cmd_report(args)


if __name__ == "__main__":
    main()