"""우리집 플레이 — Streamlit / PostgreSQL
배포: requirements.txt와 함께 업로드한 후 Streamlit Secrets에 설정:
DATABASE_URL = "postgresql://..."  # Supabase Connect의 transaction pooler URI, 비밀번호 URL 인코딩
초기 실행 계정에 CREATE TABLE 권한이 필요합니다. 앱이 테이블을 자동 생성합니다.
DB URI는 GitHub나 앱 화면에 공개하지 마세요. Python 3.10+.
"""
import copy
import html
import time
import hashlib
import hmac
import json
import random
import secrets
import string
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
import psycopg
from psycopg.types.json import Jsonb
import streamlit as st

st.set_page_config(page_title="우리집 플레이", page_icon="🏡", layout="centered")
POINTS = 30
SHOP = {"용돈 부탁":150,"설거지 부탁":90,"방 청소 부탁":120,"간식 선택권":60,"영화 선택권":60,"기타 부탁":100}
GAMES = {"오목":(9,9,5),"삼목":(3,3,3),"사목":(6,7,4),"주사위 대결":(1,1,0),"가위바위보":(1,1,0),"업다운 숫자 맞히기":(1,1,0),"31 피하기":(1,1,0),"동전 맞히기":(1,1,0)}

def stamp(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def uid(): return uuid.uuid4().hex

def digest(value, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ":" + hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt), 260000).hex()

def verify(value, stored):
    try: return hmac.compare_digest(digest(value,stored.split(":")[0]),stored)
    except (ValueError,TypeError,IndexError): return False

@st.cache_resource
def setup(url):
    with psycopg.connect(url, connect_timeout=10) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS family_play_rooms_v2 (
        name TEXT PRIMARY KEY, data JSONB NOT NULL)""")
    return True

def dburl(): return st.secrets["DATABASE_URL"]

@contextmanager
def transaction(name):
    with psycopg.connect(dburl(),connect_timeout=10) as db:
        record=db.execute("SELECT data FROM family_play_rooms_v2 WHERE name=%s FOR UPDATE",(name,)).fetchone()
        if record is None: raise ValueError("가족 방을 찾을 수 없어요.")
        room=record[0]
        yield room
        db.execute("UPDATE family_play_rooms_v2 SET data=%s WHERE name=%s",(Jsonb(room),name))

def read_room(name):
    with psycopg.connect(dburl(),connect_timeout=10) as db:
        row=db.execute("SELECT data FROM family_play_rooms_v2 WHERE name=%s",(name,)).fetchone()
    return row[0] if row else None

def new_room(name, code, person, password):
    mid=uid()
    data={"code":digest(code),"members":{mid:{"name":person,"hash":digest(password),"points":0,"wins":0}},"matches":{},"gifts":{},"ledger":[],"created":stamp()}
    with psycopg.connect(dburl(),connect_timeout=10) as db:
        db.execute("INSERT INTO family_play_rooms_v2 VALUES(%s,%s)",(name,Jsonb(data)))
    return mid

def line(board,r,c,need):
    piece=board[r][c]
    if not piece: return False
    for dr,dc in ((1,0),(0,1),(1,1),(1,-1)):
        n=1
        for sign in (1,-1):
            rr,cc=r+dr*sign,c+dc*sign
            while 0<=rr<len(board) and 0<=cc<len(board[0]) and board[rr][cc]==piece:
                n+=1; rr+=dr*sign; cc+=dc*sign
        if n>=need: return True
    return False

def new_match(game,owner,opponent):
    h,w,_=GAMES[game]
    return {"id":uid(),"game":game,"players":[owner,opponent],"status":"초대","board":[[0]*w for _ in range(h)],"turn":0,"moves":0,"scores":[0,0],"rolls":[],"winner":None,"created":stamp()}

def expand_board(room, match_id, actor, expected):
    m=room["matches"].get(match_id)
    if not m or m["status"]!="진행" or m["game"] not in ("오목","삼목","사목"):
        raise ValueError("진행 중인 보드 게임에서만 칸을 늘릴 수 있어요.")
    if m["moves"]!=expected or m["players"][m["turn"]]!=actor:
        raise ValueError("내 차례에 최신 화면에서 눌러 주세요.")
    b=m["board"];h,w=len(b),len(b[0])
    limit={"오목":(19,19),"삼목":(5,5),"사목":(10,11)}[m["game"]]
    if h>=limit[0] and w>=limit[1]: raise ValueError("최대 크기예요.")
    nh,nw=min(h+2,limit[0]),min(w+2,limit[1])
    # 연결 관계와 사목의 중력을 보존: 기존 보드 왼쪽/오른쪽과 위쪽에 빈 칸 추가.
    left=(nw-w)//2
    m["board"]=[[0]*nw for _ in range(nh-h)]+[[0]*left+row+[0]*(nw-w-left) for row in b]
    m["moves"]+=1  # 동시 클릭/오래된 착수 차단용 버전. 차례는 그대로 유지.


def log(room,member,delta,why):
    room["ledger"].append({"member":member,"delta":delta,"why":why,"time":stamp()})
    room["ledger"]=room["ledger"][-1000:]

def finish(room,m,winner,reason):
    if m["status"]!="진행": return
    m.update(status="종료",winner=winner,reason=reason,ended=stamp())
    if winner:
        room["members"][winner]["points"]+=POINTS
        room["members"][winner]["wins"]+=1
        log(room,winner,POINTS,m["game"]+" 승리")

def move(room,match_id,actor,expected,col=None,row=None):
    m=room["matches"].get(match_id)
    if not m or m["status"]!="진행": raise ValueError("이미 끝났거나 시작 전인 게임이에요.")
    if m["moves"]!=expected: raise ValueError("화면이 갱신되었어요. 현재 차례를 확인해 주세요.")
    if actor not in m["players"]: raise ValueError("참가 선수만 플레이할 수 있어요.")
    if m["game"]=="가위바위보":
        if type(col) is not int or col not in (0,1,2): raise ValueError("가위·바위·보 중 골라 주세요.")
        choices=m.setdefault("choices",{})
        if actor in choices: raise ValueError("이미 선택했어요. 상대방을 기다려 주세요.")
        choices[actor]=col;m["moves"]+=1
        if len(choices)==2:
            a,b=[choices[p] for p in m["players"]]
            result=None if a==b else (0 if (a-b)%3==1 else 1)
            if result is not None:m["scores"][result]+=1
            m.setdefault("rps_history",[]).append([a,b,result])
            m["choices"]={}
            if max(m["scores"])>=3:
                finish(room,m,m["players"][m["scores"].index(3)],"가위바위보 3승 달성")
        return
    if m["players"][m["turn"]]!=actor: raise ValueError("상대방 차례예요.")
    t=m["turn"]
    if m["game"]=="업다운 숫자 맞히기":
        low,high=m.get("low",1),m.get("high",100)
        if type(col) is not int or not low<=col<=high: raise ValueError("현재 범위 안의 숫자를 입력해 주세요.")
        target=m.setdefault("target",secrets.randbelow(100)+1)
        m["moves"]+=1
        hint="정답" if col==target else ("UP ↑" if col<target else "DOWN ↓")
        m.setdefault("guesses",[]).append([actor,col,hint])
        if col==target:finish(room,m,actor,"비밀 숫자 정답!")
        elif col<target:m["low"]=col+1
        else:m["high"]=col-1
    elif m["game"]=="31 피하기":
        count=m.get("count",0)
        if type(col) is not int or not 1<=col<=min(3,31-count):raise ValueError("부를 숫자 개수는 1~3개예요.")
        m["count"]=count+col;m["moves"]+=1
        m.setdefault("count_history",[]).append([actor,list(range(count+1,count+col+1))])
        if m["count"]==31:finish(room,m,m["players"][1-t],"상대가 31을 불렀어요!")
    elif m["game"]=="동전 맞히기":
        if type(col) is not int or col not in (0,1):raise ValueError("앞면 또는 뒷면을 골라 주세요.")
        outcome=secrets.randbelow(2)
        m["scores"][t]+=int(col==outcome);m["moves"]+=1
        m.setdefault("coin_history",[]).append([actor,col,outcome])
        if m["moves"]==10:
            a,b=m["scores"]
            finish(room,m,None if a==b else m["players"][0 if a>b else 1],"각자 5회 동전 맞히기 종료")
    elif m["game"]=="주사위 대결":
        dice=[secrets.randbelow(6)+1,secrets.randbelow(6)+1]
        m["scores"][t]+=sum(dice); m["rolls"].append([actor,dice]); m["moves"]+=1
        if m["moves"]==6:
            a,b=m["scores"]
            finish(room,m,None if a==b else m["players"][0 if a>b else 1],"주사위 합산 결과")
    else:
        b=m["board"]
        if type(col) is not int or not 0<=col<len(b[0]): raise ValueError("잘못된 위치예요.")
        if m["game"]=="사목":
            available=[r for r in range(len(b)) if b[r][col]==0]
            if not available: raise ValueError("가득 찬 열이에요.")
            row=available[-1]
        if type(row) is not int or not 0<=row<len(b) or b[row][col]: raise ValueError("빈 칸을 골라 주세요.")
        b[row][col]=t+1; m["moves"]+=1
        if line(b,row,col,GAMES[m["game"]][2]): finish(room,m,actor,"연결 성공")
        elif all(all(r) for r in b): finish(room,m,None,"무승부")
    if m["status"]=="진행": m["turn"]=1-t

def active(room,person):
    return any(person in m["players"] and m["status"] in ("초대","진행") for m in room["matches"].values())

def invitation(room,mid,actor,action):
    m=room["matches"].get(mid)
    if not m or m["status"]!="초대": raise ValueError("이미 처리된 초대예요.")
    if actor==m["players"][1] and action in ("수락","거절"):
        m["status"]="진행" if action=="수락" else "취소"
    elif actor==m["players"][0] and action=="취소": m["status"]="취소"
    else: raise ValueError("처리할 권한이 없어요.")

def buy(room,actor,receiver,item,note):
    if receiver==actor or receiver not in room["members"]: raise ValueError("다른 가족을 선택해 주세요.")
    if item not in SHOP: raise ValueError("존재하지 않는 쿠폰이에요.")
    price=SHOP[item]
    if room["members"][actor]["points"]<price: raise ValueError("포인트가 부족해요.")
    if item=="기타 부탁" and not note.strip(): raise ValueError("기타 부탁의 내용을 적어 주세요.")
    room["members"][actor]["points"]-=price
    gid=uid();room["gifts"][gid]={"sender":actor,"receiver":receiver,"item":item,"cost":price,"note":note.strip()[:200],"status":"대기","time":stamp()}
    log(room,actor,-price,item+" 구매")

def gift_action(room,gid,actor,action):
    g=room["gifts"].get(gid)
    if not g: raise ValueError("쿠폰을 찾을 수 없어요.")
    valid=(g["status"]=="대기" and ((actor==g["receiver"] and action in ("수락","거절")) or (actor==g["sender"] and action=="취소"))) or (g["status"]=="수락" and actor==g["receiver"] and action=="완료")
    if not valid: raise ValueError("이미 처리되었거나 권한이 없는 쿠폰이에요.")
    g["status"]=action
    if action in ("거절","취소"):
        room["members"][g["sender"]]["points"]+=g["cost"]
        log(room,g["sender"],g["cost"],g["item"]+" 환불")

def perform(fn,*args):
    try:
        with transaction(st.session_state.room) as room:
            if st.session_state.me not in room["members"]: raise ValueError("다시 로그인해 주세요.")
            fn(room,*args)
        return True
    except ValueError as e: st.warning(str(e))
    except psycopg.Error: st.error("연결하지 못했어요. 잠시 후 다시 시도해 주세요.")
    return False

def route(page,match=None):
    st.session_state.page=page
    if match: st.session_state.match=match
    st.rerun()

st.markdown('''<style>
.stApp {background:#F7F6FC;}
.block-container {max-width:760px;padding-top:1.5rem;}
h1,h2,h3 {color:#30274E;}
.stButton>button,.stFormSubmitButton>button {border-radius:14px;min-height:46px;}
[data-testid="stMetric"] {background:#fff;border:1px solid #E5E1EF;border-radius:16px;padding:12px;}
@media(max-width:600px){.block-container{padding-left:.7rem;padding-right:.7rem;}
[data-testid="stHorizontalBlock"]{gap:.15rem;}
[data-testid="stHorizontalBlock"] [data-testid="stColumn"]{min-width:0!important;flex:1 1 0!important;}
.stButton>button {padding:.2rem;min-width:0;width:100%;}}
</style>''',unsafe_allow_html=True)
st.title("🏡 우리집 플레이")

try:
    url=dburl()
    setup(url)
except Exception:
    st.info("가족 게임방을 준비 중이에요. 운영자가 공용 데이터 저장소 연결을 마치면 이용할 수 있어요.")
    st.stop()

if "me" not in st.session_state:
    st.caption("우리 가족만의 방에서, 떨어져 있어도 같이 놀아요.")
    tab1,tab2=st.tabs(["🚪 가족 방 입장","✨ 새 가족 방 만들기"])
    with tab1:
        with st.form("enter"):
            room_name=st.text_input("가족 방 이름",max_chars=30).strip()
            code=st.text_input("입장 코드",type="password",max_chars=64)
            kind=st.radio("이용 방법",["로그인","새 가족 구성원 가입"],horizontal=True)
            name=st.text_input("내 이름 / 별명",max_chars=16).strip()
            password=st.text_input("개인 비밀번호",type="password",max_chars=128)
            if st.form_submit_button("우리 가족 방으로",type="primary",use_container_width=True):
                if not room_name or not code or not name or len(password)<8:
                    st.warning("방 이름·코드·내 이름을 입력하고, 개인 비밀번호는 8자 이상 입력해 주세요.")
                else:
                    try:
                        with transaction(room_name) as room:
                            if not verify(code,room["code"]): raise ValueError("방 이름 또는 입장 코드가 맞지 않아요.")
                            found=next((k for k,v in room["members"].items() if v["name"]==name),None)
                            if kind=="로그인":
                                if not found or not verify(password,room["members"][found]["hash"]): raise ValueError("이름 또는 개인 비밀번호가 맞지 않아요.")
                                mid=found
                            else:
                                if found: raise ValueError("이미 등록된 이름이에요. 로그인하거나 다른 별명을 사용하세요.")
                                mid=uid(); room["members"][mid]={"name":name,"hash":digest(password),"points":0,"wins":0}
                        st.session_state.update(room=room_name,me=mid,page="홈");st.rerun()
                    except ValueError as e: st.error(str(e))
                    except psycopg.Error: st.error("입장하지 못했어요. 연결 상태를 확인해 주세요.")
    with tab2:
        with st.form("create"):
            rn=st.text_input("새 가족 방 이름",max_chars=30).strip()
            rc=st.text_input("공유할 입장 코드 (8자 이상)",type="password",max_chars=64)
            pn=st.text_input("내 이름",max_chars=16).strip()
            pp=st.text_input("내 개인 비밀번호 (8자 이상)",type="password",max_chars=128)
            pc=st.text_input("개인 비밀번호 확인",type="password",max_chars=128)
            st.caption("방 이름과 입장 코드만 가족에게 공유하세요. 개인 비밀번호는 공유하지 마세요.")
            if st.form_submit_button("가족 방 만들기",type="primary",use_container_width=True):
                if not rn or not pn or len(rc)<8 or len(pp)<8: st.warning("이름과 8자 이상의 코드·비밀번호를 입력해 주세요.")
                elif pp!=pc: st.warning("개인 비밀번호가 일치하지 않아요.")
                else:
                    try:
                        mid=new_room(rn,rc,pn,pp)
                        st.session_state.update(room=rn,me=mid,page="홈");st.rerun()
                    except psycopg.errors.UniqueViolation: st.warning("이미 사용 중인 방 이름이에요. 다른 이름을 골라 주세요.")
                    except psycopg.Error: st.error("방을 만들지 못했어요. 잠시 후 다시 시도해 주세요.")
    st.caption("비밀번호 찾기는 지원하지 않아요. 안전하게 보관하세요. 같은 가족 방은 여러 기기에서 사용할 수 있어요.")
    st.stop()

me=st.session_state.me
nav=st.columns([1,1,1,1])
for col,page,txt in zip(nav,["홈","상점","쿠폰함"],["🏠 홈","🛍️ 상점","📮 쿠폰함"]):
    if col.button(txt,use_container_width=True): route(page)
if nav[3].button("로그아웃",use_container_width=True):
    st.session_state.clear();st.rerun()

@st.fragment(run_every="2s")
def live():
    try: room=read_room(st.session_state.room)
    except psycopg.Error:
        st.warning("잠시 연결이 끊겼어요. 자동으로 다시 확인할게요.");return
    if not room or me not in room["members"]: st.error("다시 로그인해 주세요.");return
    people=room["members"]
    def who(mid): return people.get(mid,{}).get("name","가족")
    st.caption(f'{st.session_state.room} · {who(me)} · 내 포인트 {people[me]["points"]}P')
    # 종료를 확인한 각 기기에서 개별적으로 홈 이동. 홈을 떠나 있던 상대도 복귀 시 결과 확인.
    if st.session_state.get("page")=="게임":
        mid=st.session_state.get("match")
        m=room["matches"].get(mid)
        if not m or me not in m["players"]: route("홈")
        if m["status"] in ("종료","취소"):
            if m["status"]=="종료":
                st.session_state.result=(f'🎉 {who(m["winner"])} 승리! +{POINTS}P' if m["winner"] else "🤝 무승부! 다음 판에 다시 만나요.")
            else: st.session_state.result="대결이 취소되었어요."
            if m["status"]=="종료":
                key="victory_start_"+mid
                started=st.session_state.setdefault(key,time.monotonic())
                remaining=max(0,4-int(time.monotonic()-started))
                if remaining==0: route("홈")
                title=(who(m["winner"])+"님이 승리하셨어요!!") if m["winner"] else "무승부예요! 멋진 대결이었어요!"
                subtitle=f"승리 포인트 +{POINTS}P 지급 완료" if m["winner"] else "다음 판에서 다시 만나요"
                st.markdown(f'''<style>
                .victory-screen{{position:fixed;inset:0;z-index:999990;display:flex;flex-direction:column;
                align-items:center;justify-content:center;text-align:center;padding:24px 24px 130px;
                background:radial-gradient(circle at 50% 35%,#6743BD,#241343);color:white;pointer-events:none;}}
                .victory-screen h1{{color:white;font-size:clamp(30px,7vw,72px);word-break:keep-all;}}
                .victory-screen p{{font-size:20px;color:#F0E6FF;}}
                .st-key-victory_home{{position:fixed;bottom:55px;left:50%;transform:translateX(-50%);width:240px;z-index:999999;}}
                </style><section class="victory-screen"><div style="font-size:90px">🏆🎉</div>
                <h1>{html.escape(title)}</h1><p>{subtitle}</p><p>{remaining}초 후 홈으로 이동해요</p></section>''',unsafe_allow_html=True)
                if st.session_state.get("balloons_match")!=mid:
                    st.balloons();st.session_state.balloons_match=mid
                if st.button("🏠 지금 홈으로",key="victory_home",use_container_width=True): route("홈")
                return
            route("홈")
        st.subheader(f'🎮 {m["game"]}')
        st.write(f'⚫ {who(m["players"][0])} vs 🔴 {who(m["players"][1])}')
        if m["status"]=="초대":
            st.info("상대방의 수락을 기다리고 있어요. 상대방 홈에서 초대를 수락하면 시작돼요.");return
        myturn=(me not in m.get("choices",{})) if m["game"]=="가위바위보" else m["players"][m["turn"]]==me
        st.info("내 차례예요!" if myturn else f'{who(m["players"][m["turn"]])} 차례 · 자동 갱신 중')
        expected=m["moves"]
        if m["game"]=="가위바위보":
            st.caption("상대의 선택은 둘 다 고를 때까지 비밀! 먼저 3승하면 승리해요. 비기면 다시 골라요.")
            for i,c in enumerate(st.columns(2)):c.metric(who(m["players"][i]),f'{m["scores"][i]}승')
            for choice,c in enumerate(st.columns(3)):
                if c.button(["✌️ 가위","✊ 바위","🖐️ 보"][choice],disabled=not myturn,key=f"rps_{choice}",use_container_width=True):
                    if perform(move,mid,me,expected,choice):st.rerun()
            if not myturn:st.info("선택 완료! 상대방 선택을 기다려요.")
            for a,b,result in m.get("rps_history",[])[-5:]:
                st.write(f'{who(m["players"][0])}: {["가위","바위","보"][a]} / {who(m["players"][1])}: {["가위","바위","보"][b]} → {"무승부" if result is None else who(m["players"][result])+" 승"}')
        elif m["game"]=="업다운 숫자 맞히기":
            low,high=m.get("low",1),m.get("high",100)
            st.caption("1~100 사이 비밀 숫자를 번갈아 맞혀요. UP은 더 크게, DOWN은 더 작게! 먼저 맞히면 승리해요.")
            st.metric("현재 정답 범위",f"{low} ~ {high}")
            with st.form(f"guess_{mid}_{expected}"):
                number=st.number_input("추측 숫자",min_value=low,max_value=high,value=low,step=1)
                guess=st.form_submit_button("정답 도전",disabled=not myturn,use_container_width=True)
            if guess:
                if perform(move,mid,me,expected,int(number)):st.rerun()
            for player,number,hint in m.get("guesses",[])[-8:]:st.write(f"{who(player)}: {number} → {hint}")
        elif m["game"]=="31 피하기":
            st.caption("1부터 순서대로 한 번에 1~3개의 숫자를 불러요. 31을 부르면 패배!")
            count=m.get("count",0);st.metric("마지막으로 부른 숫자",count)
            for amount,c in enumerate(st.columns(3),1):
                end=count+amount
                if c.button(" · ".join(str(x) for x in range(count+1,end+1)),key=f"count_{amount}",disabled=not myturn or end>31,use_container_width=True):
                    if perform(move,mid,me,expected,amount):st.rerun()
            for player,numbers in m.get("count_history",[])[-6:]:st.write(f'{who(player)}: {", ".join(map(str,numbers))}')
        elif m["game"]=="동전 맞히기":
            st.caption("앞면? 뒷면? 번갈아 각자 5번 예측해서 더 많이 맞히면 승리! 동점은 무승부예요.")
            st.write(f'{m["moves"]} / 10회 진행')
            for i,c in enumerate(st.columns(2)):c.metric(who(m["players"][i]),f'{m["scores"][i]}점')
            for choice,c in enumerate(st.columns(2)):
                if c.button(["🪙 앞면","🪙 뒷면"][choice],key=f"coin_{choice}",disabled=not myturn,use_container_width=True):
                    if perform(move,mid,me,expected,choice):st.rerun()
            for player,guess,outcome in m.get("coin_history",[])[-6:]:
                st.write(f'{who(player)}: {["앞면","뒷면"][guess]} 선택 → 결과 {["앞면","뒷면"][outcome]} · {"정답!" if guess==outcome else "아쉬워요"}')
        elif m["game"]=="주사위 대결":
            st.caption("각자 주사위 2개를 세 번 던져요. 합계가 크면 승리! 동점은 무승부예요.")
            a,b=st.columns(2)
            for i,c in enumerate((a,b)): c.metric(who(m["players"][i]),m["scores"][i])
            if st.button("🎲 주사위 던지기",disabled=not myturn,use_container_width=True):
                if perform(move,mid,me,expected): st.rerun()
            for player,dice in m["rolls"]: st.write(f'{who(player)}: 🎲 {dice[0]} + {dice[1]}')
        else:
            st.caption({"오목":"5개 이상 연결 · 금수 없는 자유 규칙","삼목":"판이 커져도 가로·세로·대각선 3개 연결!","사목":"열을 누르면 아래로 쌓여요. 4개 연결!"}[m["game"]])
            board=m["board"]
            h,w=len(board),len(board[0])
            limits={"오목":(19,19),"삼목":(5,5),"사목":(10,11)}[m["game"]]
            st.write(f"현재 게임판: **{h} × {w}칸**")
            if st.button("➕ 칸 늘리기",key="expand_"+mid,disabled=not myturn or (h,w)==limits,use_container_width=True):
                if perform(expand_board,mid,me,expected): st.rerun()
            st.caption(f"내 차례에 두 칸씩 확장 · 최대 {limits[0]}×{limits[1]} · 기존 돌과 내 차례 유지 · 상대 화면도 함께 확장")
            if m["game"]=="사목":
                for c,column in enumerate(st.columns(len(board[0]),gap="small")):
                    if column.button("↓",key=f'drop_{mid}_{c}',disabled=not myturn or board[0][c]!=0,use_container_width=True):
                        if perform(move,mid,me,expected,c): st.rerun()
                # 읽기 전용 보드는 Markdown 표로, 작은 휴대폰에서도 열이 접히지 않게 유지.
                lines=["|"+"|".join(str(i+1) for i in range(len(board[0])))+"|","|"+"|".join(["---"]*len(board[0]))+"|"]
                lines += ["|"+"|".join(["·","⚫","🔴"][v] for v in row)+"|" for row in board]
                st.markdown("\n".join(lines))
            else:
                # CSS로 보드 셀의 모바일 가로 배치를 유지.
                for r,row in enumerate(board):
                    for c,column in enumerate(st.columns(len(row),gap="small")):
                        if column.button(["·","⚫","🔴"][row[c]],key=f'cell_{mid}_{r}_{c}',disabled=not myturn or row[c]!=0,use_container_width=True):
                            if perform(move,mid,me,expected,c,r): st.rerun()
        with st.expander("대결 그만하기"):
            st.caption("기권하면 상대가 승리하고 30P를 받아요. 홈으로 이동하는 것만으로는 기권되지 않아요.")
            if st.button("기권 확정",key="resign"):
                def resign(r):
                    match=r["matches"].get(mid)
                    if match and match["status"]=="진행" and me in match["players"]:
                        finish(r,match,next(p for p in match["players"] if p!=me),"상대 기권")
                if perform(resign): st.rerun()
        return
    page=st.session_state.get("page","홈")
    if page=="홈":
        if st.session_state.get("result"): st.success(st.session_state.result)
        st.subheader("같이 한 판 할까요? 🎈")
        st.caption("승리 +30P · 패배/무승부 0P · 가족 두 명부터 대결 가능")
        pending=[m for m in room["matches"].values() if me in m["players"] and m["status"] in ("초대","진행")]
        for m in pending:
            with st.container(border=True):
                st.write(f'{m["game"]} · {who(m["players"][0])} vs {who(m["players"][1])} · {m["status"]}')
                if m["status"]=="진행":
                    if st.button("게임 화면으로",key="resume_"+m["id"],use_container_width=True): route("게임",m["id"])
                else:
                    actions=["수락","거절"] if me==m["players"][1] else ["취소"]
                    for action in actions:
                        if st.button(action,key=m["id"]+action):
                            if perform(invitation,m["id"],me,action):
                                if action=="수락": route("게임",m["id"])
                                st.rerun()
        others=[p for p in people if p!=me]
        if others:
            st.markdown("### 🎮 하고 싶은 게임을 골라 주세요")
            game=st.selectbox("🎮 8가지 게임 중 선택",list(GAMES),key="selected_game")
            st.info({"오목":"⚫ 5개를 연결하는 오목 · 9×9에서 시작 / 확장 가능", "삼목":"⭕ 간단한 3개 연결 게임 · 3×3에서 시작 / 확장 가능", "사목":"🔴 돌을 아래로 떨어뜨려 4개 연결 · 확장 가능", "주사위 대결":"🎲 한 번씩 버튼만 누르면 OK! 각자 3회 주사위 합계 대결", "가위바위보":"✌️ 상대 선택은 비밀! 먼저 3승하면 승리", "업다운 숫자 맞히기":"🔢 1~100 비밀 숫자를 힌트를 보고 먼저 맞히기", "31 피하기":"💣 숫자를 1~3개씩 불러요. 31을 부르면 패배!", "동전 맞히기":"🪙 앞면과 뒷면 예측! 각자 5번 중 더 많이 맞히기"}[game])
            with st.form("challenge"):
                opponent=st.selectbox("함께할 가족",others,format_func=who)
                st.write(f"선택한 게임: **{game}**")
                send=st.form_submit_button("대결 초대 보내기",type="primary",use_container_width=True)
            if send:
                def invite(r):
                    if opponent not in r["members"] or opponent==me: raise ValueError("상대 가족을 다시 골라 주세요.")
                    if active(r,me) or active(r,opponent): raise ValueError("나 또는 상대방에게 진행 중인 대결/초대가 있어요. 먼저 완료하거나 취소해 주세요.")
                    # 완료 기록은 최대 100개 보관. 포인트·승수는 유지.
                    old=[k for k,v in r["matches"].items() if v["status"] not in ("초대","진행")]
                    for k in old[:-99]: del r["matches"][k]
                    match=new_match(game,me,opponent);r["matches"][match["id"]]=match
                    st.session_state.new_match_id=match["id"]
                if perform(invite): route("게임",st.session_state.new_match_id)
        else: st.info("다른 가족이 가입하면 시작할 수 있어요. 아래의 가입 방법을 공유해 주세요.")
        with st.expander("👨‍👩‍👧‍👦 가족 구성원 · 가입 안내",expanded=not others):
            st.write(f'방 이름: **{st.session_state.room}**')
            st.write("다른 가족에게 이 앱 주소·방 이름·처음 정한 입장 코드를 알려 주세요. 입장 화면에서 '새 가족 구성원 가입'을 선택하면 됩니다.")
            st.caption("같은 휴대폰에서 다른 가족을 가입시키려면 로그아웃 후 가입하세요. 개인 비밀번호는 각자 정해요.")
            for p in sorted(people,key=lambda x:people[x]["points"],reverse=True):
                st.write(f'🙂 {who(p)} · {people[p]["points"]}P · {people[p]["wins"]}승')
        with st.expander("최근 대결 결과"):
            ended=[m for m in room["matches"].values() if m["status"]=="종료"]
            for m in ended[-10:][::-1]: st.write(f'{m["game"]} · {who(m["winner"])+" 승리" if m["winner"] else "무승부"}')
    elif page=="상점":
        st.subheader("🛍️ 다정한 부탁 상점")
        st.info("쿠폰은 명령이 아닌 부탁이에요. 거절·대기 중 취소는 전액 환불! 실제 송금은 하지 않아요.")
        for item,cost in SHOP.items(): st.write(f'🎟️ **{item}** · {cost}P')
        others=[p for p in people if p!=me]
        if others:
            with st.form("shop"):
                recipient=st.selectbox("받을 가족",others,format_func=who)
                item=st.selectbox("쿠폰",list(SHOP),format_func=lambda x:f'{x} · {SHOP[x]}P')
                note=st.text_area("부탁 내용 (기타 부탁은 필수)",max_chars=200,placeholder="예: 토요일에 함께 산책해 주세요")
                agree=st.checkbox("금액·범위·시기는 서로 동의해서 정할게요.")
                submitted=st.form_submit_button("구매해서 보내기",type="primary",use_container_width=True)
            if submitted:
                if not agree: st.warning("쿠폰 약속에 동의해 주세요.")
                elif perform(buy,me,recipient,item,note):
                    st.session_state.result="쿠폰을 보냈어요!";route("쿠폰함")
        else: st.info("다른 가족이 가입하면 쿠폰을 보낼 수 있어요.")
    else:
        st.subheader("📮 내 쿠폰함")
        st.caption("다른 기기에서도 2초 간격으로 확인해요. 문자·카카오톡 알림은 보내지 않아요.")
        mode=st.radio("보기",["받은 쿠폰","보낸 쿠폰"],horizontal=True)
        field="receiver" if mode=="받은 쿠폰" else "sender"
        gifts=[(gid,g) for gid,g in room["gifts"].items() if g[field]==me]
        gifts.sort(key=lambda entry: (entry[1]["time"],entry[0]), reverse=True)
        if not gifts: st.info("아직 쿠폰이 없어요.")
        for gid,g in gifts:
            with st.container(border=True):
                st.write(f'🎟️ **{g["item"]}** · {g["cost"]}P · {g["status"]}')
                st.write(f'{who(g["sender"])} → {who(g["receiver"])}')
                if g["note"]: st.write(g["note"])
                actions=[]
                if g["status"]=="대기": actions=["수락","거절"] if me==g["receiver"] else ["취소"]
                elif g["status"]=="수락" and me==g["receiver"]: actions=["완료"]
                for action in actions:
                    if st.button(action,key=gid+action):
                        if perform(gift_action,gid,me,action): st.rerun()
        with st.expander("내 포인트 내역"):
            for e in room["ledger"][::-1]:
                if e["member"]==me: st.write(f'{e["delta"]:+}P · {e["why"]}')

live()
