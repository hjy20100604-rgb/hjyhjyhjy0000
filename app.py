"""우리 가족 플레이룸 — Streamlit Community Cloud + PostgreSQL(Supabase 권장).

배포 (이 파일과 requirements.txt 두 개만 GitHub 저장소에 업로드):
1. Supabase에서 프로젝트 생성 → Connect → Transaction pooler 연결 문자열 복사.
   Streamlit Cloud에서는 IPv4를 지원하는 pooler 주소를 권장합니다.
2. Streamlit Community Cloud에서 GitHub 저장소 연결, Main file path: app.py.
3. 앱 Settings → Secrets 에 다음 한 줄 추가(실제 비밀번호로 변경):
   DATABASE_URL = "postgresql://postgres.PROJECT:PASSWORD@POOLER_HOST:6543/postgres?sslmode=require"
   비밀번호의 @, # 등 특수문자는 URL 인코딩하세요. GitHub에 비밀정보를 올리지 마세요.
4. 최초 실행 시 family_play_rooms 테이블을 자동 생성합니다.
   DB 계정에 테이블 생성/조회/갱신 권한이 필요합니다.
5. 가족 방 생성 → 이름과 초대 코드 공유 → 각자 개인 비밀번호로 가입/로그인.

보안/운영:
- 초대 코드와 개인 비밀번호는 PBKDF2 해시만 저장합니다.
- 데이터는 PostgreSQL에 영구 저장됩니다. 앱 재시작에도 유지됩니다.
- 모든 변경은 방별 SELECT FOR UPDATE 트랜잭션으로 직렬화합니다.
- 브라우저에는 DB 접속 정보를 노출하지 않습니다. Supabase REST API는 사용하지
  않습니다. 테이블 RLS를 활성화하며 DB 소유자 연결만 사용합니다.
- 별도 비밀번호 찾기는 없습니다. 개인 비밀번호를 안전하게 보관하세요.
- 가족용 소규모 앱입니다. 공개 서비스 운영에는 로그인 시도 제한, 계정 복구,
  개인정보 처리방침, 백업 등을 별도 마련하세요. 이름 대신 별명을 권장합니다.
- 쿠폰은 가족 간 약속 기록이며 실제 송금/강제 업무 배정 기능은 아닙니다.
"""
import os
import time
import json
import math
import copy
import hmac
import hashlib
import secrets
import html
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb
import streamlit as st

st.set_page_config(page_title="우리 가족 플레이룸", page_icon="🏡", layout="centered")
GAMES = {"gomoku":"⚫ 오목", "ttt":"❎ 삼목", "connect4":"🔴 사목", "reversi":"⚪ 뒤집기", "memory":"🧠 짝 맞추기", "dice":"🎲 주사위 달리기", "bomb":"💣 숫자 폭탄", "nim":"🪵 마지막 돌 피하기", "rps":"✊ 가위바위보", "highlow":"🃏 카드 높낮이"}
SHOP = {"allowance":("💰 용돈 부탁하기",150), "dishes":("🍽️ 설거지 부탁하기",80), "clean":("🧹 방 청소 부탁하기",80), "other":("💌 기타 부탁하기",100)}

def password_hash(text, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", text.encode(), bytes.fromhex(salt), 210000).hex()
    return salt+":"+digest

def verify(text, stored):
    return hmac.compare_digest(password_hash(text, stored.split(":")[0]), stored)

def db_url():
    return os.environ.get("DATABASE_URL") or st.secrets.get("DATABASE_URL", "")

@contextmanager
def connection():
    with psycopg.connect(db_url(), connect_timeout=12, prepare_threshold=None) as conn:
        yield conn

@st.cache_resource
def init_db(url):
    with connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS family_play_rooms (
            id text PRIMARY KEY, name text UNIQUE NOT NULL, code_hash text NOT NULL,
            state jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())""")
        conn.execute("ALTER TABLE family_play_rooms ENABLE ROW LEVEL SECURITY")
    return True

def load_room():
    with connection() as conn:
        row = conn.execute("SELECT state FROM family_play_rooms WHERE id=%s", (st.session_state.room,)).fetchone()
    if not row:
        raise ValueError("방을 찾을 수 없습니다.")
    state = row[0]
    authorize(state)
    return state

def authorize(state):
    member = state["members"].get(st.session_state.get("member", ""))
    if not member or not hmac.compare_digest(member["password"], st.session_state.get("credential", "")):
        raise ValueError("다시 로그인해 주세요.")

def transaction(fn):
    with connection() as conn:
        row = conn.execute("SELECT state FROM family_play_rooms WHERE id=%s FOR UPDATE", (st.session_state.room,)).fetchone()
        if not row:
            raise ValueError("방이 없습니다.")
        state = row[0]
        authorize(state)
        fn(state, st.session_state.member)
        conn.execute("UPDATE family_play_rooms SET state=%s, updated_at=now() WHERE id=%s", (Jsonb(state),st.session_state.room))

def act(fn):
    try:
        transaction(fn)
        st.rerun()
    except (ValueError, KeyError) as exc:
        st.warning(str(exc))
    except psycopg.Error:
        st.error("저장하지 못했습니다. 연결을 확인한 뒤 다시 시도하세요.")

def login_screen():
    st.title("🏡 우리 가족 플레이룸")
    st.write("각자의 휴대폰으로 모여서 놀고, 포인트로 가족에게 부탁해요.")
    a,b = st.tabs(["🚪 가족 방 입장", "✨ 가족 방 만들기"])
    with a:
        st.caption("가족 추가도 이 화면에서! 새 별명은 가입, 기존 별명은 로그인됩니다.")
        with st.form("enter"):
            room_name=st.text_input("가족 방 이름",max_chars=40).strip()
            code=st.text_input("입장 코드",type="password").strip()
            name=st.text_input("내 별명",max_chars=16).strip()
            pw=st.text_input("개인 비밀번호 (8자 이상)",type="password",max_chars=128)
            submitted=st.form_submit_button("가입 / 로그인",use_container_width=True)
        if submitted:
            try:
                if not room_name or not name or len(pw)<8 or len(code)>128:
                    raise ValueError("방 이름, 별명과 8자 이상의 비밀번호를 확인해 주세요.")
                with connection() as conn:
                    row=conn.execute("SELECT id,code_hash,state FROM family_play_rooms WHERE name=%s FOR UPDATE",(room_name,)).fetchone()
                    if not row or not verify(code,row[1]):
                        raise ValueError("방 이름 또는 입장 코드가 올바르지 않습니다.")
                    state=row[2]
                    mid=next((k for k,v in state["members"].items() if v["name"]==name),None)
                    if mid:
                        if not verify(pw,state["members"][mid]["password"]):
                            raise ValueError("개인 비밀번호가 다릅니다. 별명이 같은지 확인하세요.")
                    else:
                        if len(state["members"])>=40:
                            raise ValueError("한 방에 최대 40명까지 가입할 수 있습니다.")
                        mid=secrets.token_hex(12)
                        state["members"][mid]={"name":name,"password":password_hash(pw),"points":0}
                        conn.execute("UPDATE family_play_rooms SET state=%s WHERE id=%s",(Jsonb(state),row[0]))
                    credential=state["members"][mid]["password"]
                st.session_state.update(room=row[0],member=mid,credential=credential,page="홈")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except psycopg.Error:
                st.error("데이터베이스 연결 오류입니다. 잠시 후 다시 시도하세요.")
    with b:
        with st.form("create"):
            rn=st.text_input("새 가족 방 이름",max_chars=40).strip()
            code=st.text_input("공유할 입장 코드 (8자 이상)",type="password",max_chars=128).strip()
            name=st.text_input("내 별명 (방장)",max_chars=16).strip()
            pw=st.text_input("나만의 개인 비밀번호 (8자 이상)",type="password",max_chars=128)
            submit=st.form_submit_button("가족 방 만들기",use_container_width=True)
        if submit:
            try:
                if not rn or not name or len(code)<8 or len(pw)<8:
                    raise ValueError("이름을 입력하고 코드와 비밀번호는 각각 8자 이상으로 정해주세요.")
                rid,mid=secrets.token_hex(12),secrets.token_hex(12)
                credential=password_hash(pw)
                state={"owner":mid,"members":{mid:{"name":name,"password":credential,"points":0}},"game":None,"coupons":[],"ledger":[]}
                with connection() as conn:
                    conn.execute("INSERT INTO family_play_rooms(id,name,code_hash,state) VALUES(%s,%s,%s,%s)",(rid,rn,password_hash(code),Jsonb(state)))
                st.session_state.update(room=rid,member=mid,credential=credential,page="홈")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except psycopg.errors.UniqueViolation:
                st.error("이미 사용 중인 방 이름입니다. 다른 이름을 정해주세요.")
            except psycopg.Error:
                st.error("방 생성 실패. 데이터베이스 설정을 확인해 주세요.")
    st.info("처음 만든 방에는 본인만 있습니다. 다른 가족이 같은 방 이름·입장 코드와 자신의 별명·개인 비밀번호로 가입하면 됩니다.")

def new_game(kind,n,creator):
    g={"id":secrets.token_hex(10),"kind":kind,"n":n,"players":[creator],"host":creator,"status":"waiting","turn":0,"version":0,"winner":None}
    return g

def start_game(g):
    k,n=g["kind"],g["n"]
    g.update(status="playing",turn=0,version=g["version"]+1,scores={p:0 for p in g["players"]})
    if k in ("gomoku","ttt","connect4","reversi"):
        rows=6 if k=="connect4" else n
        g["board"]=[[0]*n for _ in range(rows)]
        if k=="reversi":
            m=n//2
            for r,c,v in [(m-1,m-1,1),(m,m,1),(m-1,m,2),(m,m-1,2)]:g["board"][r][c]=v
    if k=="memory":
        cards=list(range(1,9))*2
        secrets.SystemRandom().shuffle(cards)
        g.update(cards=cards,matched=[],flipped=[],reveal_until=0)
    if k=="bomb":g.update(secret=secrets.randbelow(99)+1,lo=1,hi=99)
    if k=="nim":g["stones"]=21
    if k=="rps":g.update(choices={},round=1)
    if k=="highlow":g.update(card=secrets.randbelow(13)+1,round=0)

def finish(s,winner,reason):
    g=s["game"]
    if g["status"]!="playing":return
    g.update(status="finished",winner=winner,reason=reason,finished_at=time.time())
    rewards=g["players"] if winner is None else [winner]
    for p in rewards:
        amount=10 if winner is None else 30
        s["members"][p]["points"]+=amount
        s["ledger"].append({"at":time.time(),"member":p,"amount":amount,"note":GAMES[g["kind"]]+(" 무승부" if winner is None else " 승리")})
    s["ledger"]=s["ledger"][-300:]

def next_turn(g):g["turn"]=(g["turn"]+1)%len(g["players"])

def check_line(b,r,c,need):
    v=b[r][c]
    for dr,dc in [(1,0),(0,1),(1,1),(1,-1)]:
        count=1
        for sign in (-1,1):
            y,x=r+dr*sign,c+dc*sign
            while 0<=y<len(b) and 0<=x<len(b[0]) and b[y][x]==v:
                count+=1;y+=dr*sign;x+=dc*sign
        if count>=need:return True
    return False

def flips(b,r,c,piece):
    if b[r][c]:return []
    found=[]
    for dr,dc in [(a,b) for a in (-1,0,1) for b in (-1,0,1) if a or b]:
        line=[];y,x=r+dr,c+dc
        while 0<=y<len(b) and 0<=x<len(b) and b[y][x]==3-piece:
            line.append((y,x));y+=dr;x+=dc
        if line and 0<=y<len(b) and 0<=x<len(b) and b[y][x]==piece:found+=line
    return found

def legal_moves(b,piece):
    return [(r,c) for r in range(len(b)) for c in range(len(b)) if flips(b,r,c,piece)]

def settle_memory(g):
    if len(g.get("flipped",[]))==2 and time.time()>=g["reveal_until"]:
        g["flipped"]=[]

def move(s,mid,gid,version,value):
    g=s["game"]
    if not g or g["id"]!=gid or g["status"]!="playing" or g["version"]!=version:
        raise ValueError("화면이 갱신됐습니다. 최신 차례를 확인하세요.")
    if mid not in g["players"]:raise ValueError("참가자만 조작할 수 있습니다.")
    k=g["kind"]
    if k!="rps" and g["players"][g["turn"]]!=mid:raise ValueError("다른 가족의 차례입니다.")
    g["version"]+=1
    if k in ("gomoku","ttt","connect4","reversi"):
        b=g["board"];r,c=value
        if k=="connect4":
            if not 0<=c<len(b[0]):raise ValueError("열을 확인해주세요.")
            available=[i for i in range(len(b)) if b[i][c]==0]
            if not available:raise ValueError("가득 찬 열입니다.")
            r=max(available)
        if not (0<=r<len(b) and 0<=c<len(b[0])) or b[r][c]:raise ValueError("빈칸을 선택하세요.")
        piece=g["players"].index(mid)+1
        if k=="reversi":
            captures=flips(b,r,c,piece)
            if not captures:raise ValueError("상대 돌을 뒤집을 수 있는 칸만 선택하세요.")
            b[r][c]=piece
            for y,x in captures:b[y][x]=piece
            next_turn(g)
            if not legal_moves(b,3-piece):
                next_turn(g)
                if not legal_moves(b,piece):
                    a=sum(row.count(1) for row in b);z=sum(row.count(2) for row in b)
                    finish(s,None if a==z else g["players"][0 if a>z else 1],f"최종 돌 개수 {a} : {z}")
            return
        b[r][c]=piece
        if check_line(b,r,c,{"gomoku":5,"ttt":3,"connect4":4}[k]):finish(s,mid,"연속된 돌을 완성했어요!")
        elif all(all(row) for row in b):finish(s,None,"빈칸 없이 비겼어요.")
        else:next_turn(g)
    elif k=="memory":
        settle_memory(g)
        if len(g["flipped"])==2:raise ValueError("카드를 잠시 기억해 주세요.")
        i=int(value)
        if not 0<=i<16 or i in g["matched"] or i in g["flipped"]:raise ValueError("뒤집지 않은 카드를 선택하세요.")
        g["flipped"].append(i)
        if len(g["flipped"])==2:
            a,b=g["flipped"]
            if g["cards"][a]==g["cards"][b]:
                g["matched"]+=g["flipped"];g["flipped"]=[];g["scores"][mid]+=1
                if len(g["matched"])==16:
                    best=max(g["scores"].values());w=[p for p,v in g["scores"].items() if v==best]
                    finish(s,w[0] if len(w)==1 else None,"짝을 모두 찾았어요!")
            else:g["reveal_until"]=time.time()+2;next_turn(g)
    elif k=="dice":
        roll=secrets.randbelow(6)+1;g["scores"][mid]+=roll;g["last"]=f"주사위 {roll}!"
        if g["scores"][mid]>=30:finish(s,mid,"30칸에 먼저 도착했어요!")
        else:next_turn(g)
    elif k=="bomb":
        v=int(value)
        if not g["lo"]<=v<=g["hi"]:raise ValueError("표시된 범위 안의 숫자를 입력하세요.")
        if v==g["secret"]:
            # 폭탄을 고른 사람 탈락. 마지막 한 사람이 우승합니다.
            g.setdefault("out",[]).append(mid)
            alive=[p for p in g["players"] if p not in g["out"]]
            g["last"]=s["members"][mid]["name"]+"님이 폭탄을 골랐어요!"
            if len(alive)==1:finish(s,alive[0],"폭탄을 피해 마지막까지 살아남았어요!")
            else:g.update(secret=secrets.randbelow(99)+1,lo=1,hi=99)
        elif v<g["secret"]:g["lo"]=v+1;g["last"]="더 큰 숫자예요!"
        else:g["hi"]=v-1;g["last"]="더 작은 숫자예요!"
        if g["status"]=="playing":
            next_turn(g)
            while g["players"][g["turn"]] in g.get("out",[]):next_turn(g)
    elif k=="nim":
        v=int(value)
        if not 1<=v<=min(3,g["stones"]):raise ValueError("돌은 1~3개만 가져가세요.")
        g["stones"]-=v
        if g["stones"]==0:finish(s,g["players"][1-g["turn"]],"마지막 돌을 상대에게 남겼어요!")
        else:next_turn(g)
    elif k=="rps":
        if value not in ("가위","바위","보") or mid in g["choices"]:raise ValueError("이미 선택했거나 잘못된 선택입니다.")
        g["choices"][mid]=value
        if len(g["choices"])==len(g["players"]):
            kinds=set(g["choices"].values());w=[]
            if len(kinds)==2:
                winning=next(x for x,y in [("가위","보"),("바위","가위"),("보","바위")] if x in kinds and y in kinds)
                w=[p for p,v in g["choices"].items() if v==winning]
            g["last"]=" / ".join(s["members"][p]["name"]+": "+v for p,v in g["choices"].items())
            for p in w:g["scores"][p]+=1
            if any(g["scores"][p]>=3 for p in w):
                champions=[p for p in w if g["scores"][p]>=3]
                if len(champions)==1:finish(s,champions[0],"가위바위보 3점을 먼저 모았어요!")
                else:
                    # 동시 3점 이상이면 이 점수는 취소하고 재승부합니다.
                    for p in champions:g["scores"][p]-=1
                    g["last"]+=" · 공동 선두 재승부!"
            g["choices"]={};g["round"]+=1
    elif k=="highlow":
        if value not in ("높다","낮다"):raise ValueError("높다 또는 낮다를 선택하세요.")
        old=g["card"];new=secrets.randbelow(13)+1;g["card"]=new;g["round"]+=1
        correct=(value=="높다" and new>old) or (value=="낮다" and new<old)
        if correct:g["scores"][mid]+=1
        g["last"]=f"{old} → {new} · "+("정답!" if correct else "같거나 예상과 달라요.")
        if g["scores"][mid]>=5:finish(s,mid,"카드 예측 5점을 모았어요!")
        else:next_turn(g)

RULES={"gomoku":"두 명이 번갈아 돌을 놓고 가로·세로·대각선 5개를 이으면 승리! 금수 없는 자유 오목입니다.","ttt":"3×3에서 같은 표시 3개를 먼저 이어요.","connect4":"열을 고르면 돌이 아래로 떨어져요. 4개를 먼저 이으면 승리!", "reversi":"상대 돌을 내 돌 사이에 끼워 뒤집어요. 둘 다 둘 수 없으면 돌이 많은 사람이 승리. 둘 곳이 없으면 자동으로 차례를 넘겨요.","memory":"카드 2장을 열어 같은 그림을 찾아요. 성공하면 한 번 더! 마지막에 짝을 많이 찾으면 승리.","dice":"번갈아 주사위를 던져 30칸에 먼저 도착하세요.","bomb":"범위 안의 숫자를 골라요. 숨겨진 폭탄 숫자를 고르면 탈락! 마지막 생존자가 승리.","nim":"21개 돌에서 매번 1~3개를 가져가요. 마지막 돌을 가져가면 패배!", "rps":"각자 비밀리에 선택해요. 이길 때마다 1점, 혼자 먼저 3점을 모으면 승리. 공동 도달은 재승부!", "highlow":"다음 카드(1~13)가 현재보다 높을지 낮을지 예측! 같은 숫자는 오답, 먼저 5점이면 승리."}

def home(s,mid):
    st.header("오늘도 함께 놀아요 🎈")
    st.metric("내 포인트",f'{s["members"][mid]["points"]} P')
    with st.expander("👨‍👩‍👧‍👦 가족 구성원 / 가입 안내",expanded=True):
        st.write(" · ".join(html.escape(m["name"]) for m in s["members"].values()))
        st.caption("가족 추가: 다른 기기에서 이 앱을 열고 같은 방 이름·입장 코드로 가입하세요. 왼쪽 메뉴 없이 이 중앙 화면에서 진행합니다.")
    g=s["game"]
    if g and g["status"] in ("waiting","playing"):
        st.info("진행 중: "+GAMES[g["kind"]])
        if st.button("🎮 게임 화면으로",use_container_width=True):st.session_state.page="게임";st.rerun()
    else:
        kind=st.selectbox("하고 싶은 게임",list(GAMES),format_func=GAMES.get)
        st.caption(RULES[kind])
        default={"gomoku":9,"ttt":3,"connect4":7,"reversi":8}.get(kind,4)
        key="size_"+kind
        if key not in st.session_state:st.session_state[key]=default
        if kind in ("gomoku","connect4"):
            st.write(f'시작 판 크기: {st.session_state[key]}'+("×"+str(st.session_state[key]) if kind=="gomoku" else "열 × 6행"))
            c1,c2=st.columns(2)
            if c1.button("➕ 칸 늘리기",use_container_width=True):st.session_state[key]=min(19 if kind=="gomoku" else 12,st.session_state[key]+2);st.rerun()
            if c2.button("↩ 기본 크기",use_container_width=True):st.session_state[key]=default;st.rerun()
        if st.button("게임 대기방 만들기",type="primary",use_container_width=True):
            def create(s,m):
                if s["game"] and s["game"]["status"]!="finished":raise ValueError("이미 게임이 열려 있어요.")
                s["game"]=new_game(kind,st.session_state[key],m)
            st.session_state.page="게임";act(create)
    st.caption("최소 2명이 필요해요. 보드 게임·돌 게임은 2명, 그 외 게임은 2~8명이 참가할 수 있어요. 승리 +30P / 무승부 각 +10P.")
    with st.expander("📊 가족 포인트 순위"):
        for i,m in enumerate(sorted(s["members"].values(),key=lambda x:-x["points"]),1):st.write(f'{i}. {m["name"]} · {m["points"]} P')
    with st.expander("🧾 내 포인트 내역"):
        logs=[x for x in s["ledger"] if x["member"]==mid]
        if not logs:st.caption("아직 기록이 없어요.")
        for x in reversed(logs[-30:]):st.write(f'{x["amount"]:+} P · {x["note"]}')

def shop(s,mid):
    st.header("🎁 가족 부탁 상점")
    st.write(f'사용 가능: **{s["members"][mid]["points"]} P**')
    st.caption("구매한 쿠폰은 선택한 가족에게 전달됩니다. 받는 사람이 수락한 후 완료해요. 거절하면 포인트가 환불돼요. 실제 용돈 액수와 집안일은 서로 합의하세요.")
    targets=[p for p in s["members"] if p!=mid]
    if not targets:st.info("다른 가족이 먼저 가입해야 쿠폰을 보낼 수 있어요.");return
    with st.form("buy"):
        item=st.selectbox("부탁 선택",list(SHOP),format_func=lambda k:f"{SHOP[k][0]} · {SHOP[k][1]} P")
        recipient=st.selectbox("누구에게 보낼까요?",targets,format_func=lambda p:s["members"][p]["name"])
        note=st.text_input("부탁 내용 (기타 부탁은 필수)",max_chars=150,placeholder="예: 이번 토요일 저녁 설거지를 부탁해요")
        submitted=st.form_submit_button("포인트로 구매하고 보내기",use_container_width=True)
    if submitted:
        def buy(s,m):
            title,cost=SHOP[item]
            if recipient not in s["members"] or recipient==m:raise ValueError("받는 가족을 확인하세요.")
            if item=="other" and not note.strip():raise ValueError("기타 부탁 내용을 입력하세요.")
            if s["members"][m]["points"]<cost:raise ValueError("포인트가 부족해요. 게임에서 승리해 모아보세요!")
            if len(s["coupons"])>=2000:raise ValueError("방의 쿠폰 보관 한도에 도달했어요.")
            s["members"][m]["points"]-=cost
            s["coupons"].append({"id":secrets.token_hex(10),"from":m,"to":recipient,"title":title,"cost":cost,"note":note.strip(),"status":"대기","at":time.time()})
            s["ledger"].append({"at":time.time(),"member":m,"amount":-cost,"note":title+" 구매"})
            s["ledger"]=s["ledger"][-300:]
        act(buy)

def coupons(s,mid):
    st.header("💌 가족 쿠폰함")
    tab=st.radio("쿠폰 보기",["받은 부탁","보낸 부탁"],horizontal=True)
    entries=[c for c in s["coupons"] if c["to" if tab=="받은 부탁" else "from"]==mid]
    if not entries:st.info("아직 쿠폰이 없어요.")
    for c in reversed(entries):
        with st.container(border=True):
            st.subheader(c["title"])
            st.write(f'{s["members"][c["from"]]["name"]} → {s["members"][c["to"]]["name"]} · {c["cost"]} P · {c["status"]}')
            st.write(c["note"] or "자세한 내용은 가족끼리 이야기해 주세요.")
            def update(s,m,cid,new):
                q=next(x for x in s["coupons"] if x["id"]==cid)
                if q["to"]!=m:raise ValueError("받은 사람만 처리할 수 있어요.")
                if (q["status"],new) not in [("대기","수락"),("대기","거절"),("수락","완료")]:raise ValueError("이미 처리된 쿠폰이에요.")
                q["status"]=new
                if new=="거절":
                    s["members"][q["from"]]["points"]+=q["cost"]
                    s["ledger"].append({"at":time.time(),"member":q["from"],"amount":q["cost"],"note":q["title"]+" 거절 환불"})
                    s["ledger"]=s["ledger"][-300:]
            if c["to"]==mid:
                if c["status"]=="대기":
                    a,b=st.columns(2)
                    if a.button("수락",key="accept"+c["id"],use_container_width=True):act(lambda s,m:update(s,m,c["id"],"수락"))
                    if b.button("거절 / 환불",key="reject"+c["id"],use_container_width=True):act(lambda s,m:update(s,m,c["id"],"거절"))
                elif c["status"]=="수락" and st.button("부탁 완료!",key="done"+c["id"]):act(lambda s,m:update(s,m,c["id"],"완료"))

def game_screen(s,mid):
    g=s["game"]
    if not g:st.info("홈에서 게임을 선택해주세요.");return
    gid,version=g["id"],g["version"]
    if g["status"]=="finished":
        # 기기마다 첫 확인 순간부터 3.5초 표시한 뒤 홈으로 돌아갑니다.
        seen=st.session_state.setdefault("seen_results",{})
        if gid not in seen:seen[gid]=time.time()
        if time.time()-seen[gid]>=3.5:
            st.session_state.page="홈";st.rerun()
        winner=g["winner"]
        title=(s["members"][winner]["name"]+"님이 승리하셨어요!!") if winner else "멋진 승부! 무승부예요!"
        st.markdown('<div class="victory"><div>🏆</div><h1>'+html.escape(title)+'</h1><p>'+html.escape(g.get("reason",""))+'</p><p>'+('승리 포인트 +30 P' if winner else '참가자 모두 +10 P')+'</p><small>잠시 후 홈으로 이동합니다</small></div>',unsafe_allow_html=True)
        return
    st.header(GAMES[g["kind"]])
    st.caption(RULES[g["kind"]])
    st.write("참가자: "+" · ".join(s["members"][p]["name"] for p in g["players"]))
    if g["status"]=="waiting":
        capacity=2 if g["kind"] in ("gomoku","ttt","connect4","reversi","nim") else 8
        st.info(f"다른 기기에서 홈 → 게임 화면으로 들어와 참가하세요. 2~{capacity}명")
        if mid not in g["players"] and st.button("🙋 나도 참가",use_container_width=True):
            def join(s,m):
                q=s["game"]
                if q["id"]!=gid or q["status"]!="waiting" or len(q["players"])>=capacity:raise ValueError("참가할 수 없는 대기방입니다.")
                if m not in q["players"]:q["players"].append(m);q["version"]+=1
            act(join)
        if mid==g["host"] and st.button("🚀 게임 시작",disabled=len(g["players"])<2,type="primary",use_container_width=True):
            def start(s,m):
                q=s["game"]
                if q["id"]!=gid or q["status"]!="waiting" or q["host"]!=m or len(q["players"])<2:raise ValueError("최소 2명이 참가해야 시작할 수 있어요.")
                start_game(q)
            act(start)
    else:
        mine=mid in g["players"] and (g["kind"]=="rps" or g["players"][g["turn"]]==mid)
        if mid not in g["players"]:st.caption("👀 관전 중입니다. 다음 게임에 참가해보세요.")
        if g["kind"]!="rps":st.success("현재 차례: "+s["members"][g["players"][g["turn"]]]["name"]+(" (나)" if mine else ""))
        if g.get("last"):st.info(g["last"])
        if "scores" in g and g["kind"] not in ("gomoku","ttt","connect4","reversi","nim","bomb"):
            st.write(" | ".join(f'{s["members"][p]["name"]}: {v}' for p,v in g["scores"].items()))
        def play(value):act(lambda s,m:move(s,m,gid,version,value))
        k=g["kind"]
        if k in ("gomoku","ttt","connect4","reversi"):
            b=g["board"]
            st.caption("① "+s["members"][g["players"][0]]["name"]+" = 🔴 / ② "+s["members"][g["players"][1]]["name"]+" = 🔵")
            table='<div class="boardwrap"><table class="board"><tr><th></th>'+''.join(f'<th>{i+1}</th>' for i in range(len(b[0])))+'</tr>'
            for r,row in enumerate(b):table+=f'<tr><th>{r+1}</th>'+''.join('<td>'+["·","🔴","🔵"][v]+'</td>' for v in row)+'</tr>'
            st.markdown(table+'</table></div>',unsafe_allow_html=True)
            if k=="reversi" and mine:
                legal=legal_moves(b,g["players"].index(mid)+1)
                st.caption("둘 수 있는 (행, 열): "+", ".join(f"({r+1},{c+1})" for r,c in legal))
            with st.form("board_move_"+gid,clear_on_submit=False):
                a,z=st.columns(2)
                r=a.number_input("행 ↓",1,len(b),1,disabled=k=="connect4")
                c=z.number_input("열 →",1,len(b[0]),1)
                if st.form_submit_button("이 칸에 놓기" if k!="connect4" else "이 열에 떨어뜨리기",disabled=not mine,use_container_width=True):play((int(r)-1,int(c)-1))
            if k=="gomoku" and len(b)<19 and st.button("➕ 판에 칸 늘리기 (+2)",disabled=not mine,use_container_width=True):
                def expand(s,m):
                    q=s["game"]
                    if q["id"]!=gid or q["version"]!=version or q["status"]!="playing" or q["players"][q["turn"]]!=m:raise ValueError("현재 차례에만 늘릴 수 있어요.")
                    old=len(q["board"]);n=min(19,old+2)
                    for row in q["board"]:row.extend([0]*(n-old))
                    q["board"].extend([[0]*n for _ in range(n-old)]);q["n"]=n;q["version"]+=1
                act(expand)
        elif k=="memory":
            locked=len(g["flipped"])==2 and time.time()<g["reveal_until"]
            visible=g["matched"]+(g["flipped"] if len(g["flipped"])<2 or locked else [])
            icons=["","🍎","🍌","🍇","🍓","🍊","🥝","🍒","🍉"]
            for r in range(4):
                cols=st.columns(4)
                for c in range(4):
                    i=r*4+c
                    if cols[c].button(icons[g["cards"][i]] if i in visible else "❓",key=f"card{i}",disabled=not mine or locked or i in visible,use_container_width=True):play(i)
        elif k=="dice":
            if st.button("🎲 주사위 던지기",disabled=not mine,use_container_width=True):play(None)
        elif k=="bomb":
            st.subheader(f'{g["lo"]} ~ {g["hi"]}')
            with st.form("bomb"+str(g["version"])):
                v=st.number_input("폭탄을 피할 숫자",g["lo"],g["hi"],g["lo"])
                if st.form_submit_button("숫자 선택",disabled=not mine):play(v)
        elif k=="nim":
            st.subheader(f'🪨 남은 돌 {g["stones"]}개')
            for i,col in enumerate(st.columns(3),1):
                if col.button(f"{i}개 가져가기",disabled=not mine or i>g["stones"],use_container_width=True):play(i)
        elif k=="rps":
            st.write(f'{g["round"]}라운드 · 선택 완료 {len(g["choices"])}/{len(g["players"])}명')
            for text,col in zip(["가위","바위","보"],st.columns(3)):
                if col.button(text,disabled=not mine or mid in g["choices"],use_container_width=True):play(text)
        elif k=="highlow":
            st.subheader(f'현재 카드: {g["card"]}')
            for text,col in zip(["높다","낮다"],st.columns(2)):
                if col.button(text,disabled=not mine,use_container_width=True):play(text)
    if mid in (g["host"],s["owner"]):
        with st.expander("게임 관리"):
            confirm=st.checkbox("참가자들과 중단에 동의했어요",key="cancelok"+gid)
            if st.button("게임 중단 (포인트 없음)",disabled=not confirm):
                def cancel(s,m):
                    q=s["game"]
                    if not q or q["id"]!=gid or q["status"]=="finished" or m not in (q["host"],s["owner"]):raise ValueError("중단할 수 없습니다.")
                    s["game"]=None
                st.session_state.page="홈";act(cancel)

st.markdown('''<style>
.stApp {background:linear-gradient(160deg,#fffaf0,#eef6ff);color:#17243a}
.block-container {max-width:850px;padding-top:1.4rem;padding-bottom:3rem}
h1,h2,h3 {color:#243451}
.stButton>button,.stFormSubmitButton>button {min-height:46px;border-radius:14px}
.boardwrap {overflow-x:auto;margin:12px 0;border-radius:12px;background:#fff4d6;padding:8px}
.board {border-collapse:collapse;margin:auto;white-space:nowrap;color:#17243a;font-size:13px}
.board th {font-size:11px;padding:3px}.board td {border:1px solid #bfa97e;text-align:center;min-width:26px;height:27px;padding:0}
.victory {position:fixed;inset:0;z-index:999999;background:linear-gradient(130deg,#4936a8,#115e75);color:white;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px}
.victory>div {font-size:90px}.victory h1 {color:white;font-size:clamp(26px,6vw,60px)}
@media(max-width:600px){.block-container{padding-left:14px;padding-right:14px}.board td{min-width:22px;height:25px;font-size:12px}}
</style>''',unsafe_allow_html=True)

try:
    if not db_url():
        st.title("🏡 우리 가족 플레이룸")
        st.info("여러 기기 동기화를 위해 데이터베이스 연결이 필요합니다. app.py 맨 위의 배포 안내를 따라 Streamlit Secrets에 DATABASE_URL을 등록해 주세요.")
        st.code('DATABASE_URL = "postgresql://USER:PASSWORD@HOST:6543/postgres?sslmode=require"',language="toml")
        st.stop()
    init_db(db_url())
except (psycopg.Error,st.errors.StreamlitSecretNotFoundError):
    st.error("데이터베이스 설정이 필요합니다. Streamlit Secrets의 DATABASE_URL과 app.py 상단 안내를 확인하세요.")
    st.stop()

if not st.session_state.get("room"):
    login_screen()
    st.stop()

st.title("🏡 우리 가족 플레이룸")
nav=st.columns(4)
for col,label in zip(nav,["홈","게임","상점","쿠폰함"]):
    if col.button(label,use_container_width=True):st.session_state.page=label;st.rerun()
with st.expander("내 접속 정보"):
    st.caption("다른 가족은 같은 방 이름과 입장 코드로 가입할 수 있어요. 개인 비밀번호는 공유하지 마세요.")
    if st.button("로그아웃"):
        st.session_state.clear();st.rerun()

@st.fragment(run_every=0.5)
def live_page():
    try:
        s=load_room();mid=st.session_state.member
        st.caption("로그인: "+s["members"][mid]["name"]+" · 여러 기기 자동 동기화")
        page=st.session_state.get("page","홈")
        if page=="홈":home(s,mid)
        elif page=="게임":game_screen(s,mid)
        elif page=="상점":shop(s,mid)
        else:coupons(s,mid)
    except ValueError as exc:
        st.error(str(exc))
    except psycopg.Error:
        st.warning("동기화 연결이 잠시 끊겼어요. 잠시 후 자동으로 다시 확인합니다.")

live_page()
