
import os, re, sqlite3, hmac
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, session, render_template
import requests
from dotenv import load_dotenv
load_dotenv()

API_URL = os.getenv('API_URL', 'https://draw.ar-lottery01.com/TrxWinGo/TrxWinGo_1M/GetHistoryIssuePage.json')
LOGIN_PASSWORD = os.getenv('LOGIN_PASSWORD', '').strip()
SECRET_KEY = os.getenv('SECRET_KEY', 'CHANGE-THIS-TO-A-LONG-RANDOM-SECRET')
DB_PATH = os.getenv('DB_PATH', 'wingo.sqlite3')
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE','0') == '1',
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS prediction_history (
            target_issue TEXT PRIMARY KEY, predicted_side TEXT NOT NULL,
            score INTEGER, basis TEXT, created_at TEXT NOT NULL,
            result_number INTEGER, actual_side TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING')""")

def utcnow():
    return datetime.now(timezone.utc)

def parse_rows(payload):
    candidates = []
    data = payload.get('data')
    if isinstance(data, dict):
        candidates += [data.get(k) for k in ('list','records','result','data')]
    candidates += [payload.get('list'), payload.get('records'), data]
    for value in candidates:
        if isinstance(value, list): return value
    return []

def number_of(row):
    raw = next((row.get(k) for k in ('number','openNumber','result','lotteryNumber','openCode') if row.get(k) is not None), None)
    if raw is None: return None
    m = re.search(r'\\d', str(raw))
    if not m: return None
    n = int(m.group())
    return n if 0 <= n <= 9 else None

def issue_of(row):
    return str(next((row.get(k) for k in ('issueNumber','issue','period','issue_no') if row.get(k) is not None), '—'))

def fetch_history():
    r = requests.get(API_URL, headers={'Accept':'application/json','User-Agent':'TRX-Wingo-Dashboard/2.0'}, timeout=15)
    r.raise_for_status()
    clean, seen = [], set()
    for row in parse_rows(r.json()):
        n, issue = number_of(row), issue_of(row)
        if n is not None and issue not in seen:
            seen.add(issue)
            clean.append({'issue':issue,'number':n,'size':'BIG' if n >= 5 else 'SMALL',
                          'color':str(row.get('colour') or row.get('color') or '—')})
    return clean

def hybrid_analysis(history):
    sides = [x['size'] for x in history]
    if len(sides) < 30:
        return {'side':'WAIT','score':None,'next_issue':None,
                'basis':f'Need 30 valid draws; currently have {len(sides)}',
                'components':{},'confidence_label':'INSUFFICIENT DATA'}
    chrono = list(reversed(sides))
    recent20, recent50 = chrono[-20:], chrono[-50:]
    big20, big50 = recent20.count('BIG')/20, recent50.count('BIG')/50
    current = sides[0]
    trans = {'BIG':{'BIG':0,'SMALL':0},'SMALL':{'BIG':0,'SMALL':0}}
    for older,newer in zip(chrono[:-1],chrono[1:]): trans[older][newer] += 1
    counts = trans[current]
    t_total = sum(counts.values())
    trans_big = counts['BIG']/t_total if t_total else .5
    streak_side, streak = sides[0], 0
    for side in sides:
        if side != streak_side: break
        streak += 1
    streak_bias = (.06 if streak_side == 'BIG' else -.06) * (.5 if streak >= 4 else 1)
    p_big = .40*big20 + .25*big50 + .30*trans_big + .05*(.5+streak_bias)
    score = round(max(p_big,1-p_big)*100)
    side = ('BIG' if p_big > .5 else 'SMALL') if abs(p_big-.5) >= .08 and score >= 58 else 'WAIT'
    try: next_issue = str(int(history[0]['issue'])+1)
    except Exception: next_issue = None
    comp = {'big_20_pct':round(big20*100),'big_50_pct':round(big50*100),
            'transition_big_pct':round(trans_big*100),'current_streak':f'{streak_side} × {streak}',
            'sample_transitions':t_total}
    basis = f"20-draw BIG {round(big20*100)}% · 50-draw BIG {round(big50*100)}% · transition BIG {round(trans_big*100)}% · streak {streak_side}×{streak}"
    label = 'LOW' if score < 65 else ('MODERATE' if score < 72 else 'HISTORICAL EDGE')
    return {'side':side,'score':score,'next_issue':next_issue,'basis':basis,
            'components':comp,'confidence_label':label}

def backtest(history):
    chrono = list(reversed(history))
    wins=losses=skipped=0
    tested=[]
    for i in range(30,len(chrono)):
        pred=hybrid_analysis(list(reversed(chrono[:i])))
        actual=chrono[i]['size']
        if pred['side']=='WAIT':
            skipped+=1
            continue
        status='WIN' if pred['side']==actual else 'LOSE'
        if status=='WIN': wins+=1
        else: losses+=1
        tested.append({'issue':chrono[i]['issue'],'pick':pred['side'],'actual':actual,'status':status})
    settled=wins+losses
    return {'wins':wins,'losses':losses,'skipped':skipped,'tested':settled,
            'win_rate':round(wins/settled*100,1) if settled else None,
            'recent':list(reversed(tested[-12:]))}

def login_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        if not session.get('authenticated'):
            session.clear()
            return jsonify({'error':'Login required'}),401
        return fn(*args,**kwargs)
    return wrapper

@app.get('/')
def home(): return render_template('index.html')

@app.post('/api/login')
def api_login():
    data=request.get_json(silent=True) or {}
    supplied=str(data.get('password',''))
    if not LOGIN_PASSWORD or not hmac.compare_digest(supplied,LOGIN_PASSWORD):
        return jsonify({'ok':False,'error':'Incorrect password'}),401
    session.clear(); session.permanent=True; session['authenticated']=True
    return jsonify({'ok':True})

@app.post('/api/logout')
def api_logout():
    session.clear()
    return jsonify({'ok':True})

@app.get('/api/dashboard')
@login_required
def api_dashboard():
    try:
        history=fetch_history()
        if not history: raise ValueError('No valid results found in API response')
        sample=history[:50]
        big=sum(1 for x in sample if x['size']=='BIG')
        total=len(sample)
        pred=hybrid_analysis(history)
        bt=backtest(history)
        with db() as con:
            for result in history:
                con.execute("""UPDATE prediction_history SET result_number=?,actual_side=?,
                    status=CASE WHEN predicted_side=? THEN 'WIN' ELSE 'LOSE' END
                    WHERE target_issue=? AND status='PENDING'""",
                    (result['number'],result['size'],result['size'],result['issue']))
            if pred.get('next_issue') and pred.get('side') in ('BIG','SMALL'):
                con.execute("""INSERT OR IGNORE INTO prediction_history
                    (target_issue,predicted_side,score,basis,created_at,status)
                    VALUES(?,?,?,?,?,'PENDING')""",
                    (pred['next_issue'],pred['side'],pred.get('score'),pred.get('basis'),utcnow().isoformat()))
            rows=con.execute("""SELECT target_issue,predicted_side,score,basis,created_at,
                result_number,actual_side,status FROM prediction_history
                ORDER BY created_at DESC LIMIT 100""").fetchall()
        pred_rows=[dict(x) for x in rows]
        wins=sum(x['status']=='WIN' for x in pred_rows)
        losses=sum(x['status']=='LOSE' for x in pred_rows)
        settled=wins+losses
        trend=[{'issue':x['issue'][-5:],'big':1 if x['size']=='BIG' else 0,'number':x['number']}
               for x in reversed(sample)]
        return jsonify({'ok':True,'history':history[:50],'prediction_history':pred_rows,
            'prediction_stats':{'wins':wins,'losses':losses,
                'pending':sum(x['status']=='PENDING' for x in pred_rows),
                'win_rate':round(wins/settled*100,1) if settled else 0},
            'stats':{'total':total,'big':big,'small':total-big,
                'big_pct':round(big/total*100) if total else 0,
                'small_pct':round((total-big)/total*100) if total else 0},
            'prediction':pred,'backtest':bt,'trend':trend,'updated_at':utcnow().isoformat()})
    except Exception as e:
        return jsonify({'error':f'Live API unavailable: {str(e)[:180]}'}),502

if __name__=='__main__':
    init_db()
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
