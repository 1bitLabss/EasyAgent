import json, time, statistics as st, re
from b import *
SH2=SH
INIT2 = INIT + r"""
window.__lt=[]; window.__raf=[];
try{new PerformanceObserver(l=>{for(const e of l.getEntries()) __lt.push({t:e.startTime,d:e.duration})}).observe({type:'longtask',buffered:true});}catch(e){}
document.addEventListener('keydown',ev=>{ if(!window.__track) return; requestAnimationFrame(()=>{ __raf.push(performance.now()-ev.timeStamp); }); },true);
"""
R={}
def log(k,v): R[k]=v; print(k, json.dumps(v)[:600], flush=True)
with sync_playwright() as p:
    br=launch(p); ctx=br.new_context(viewport={"width":1280,"height":850}); ctx.add_init_script(INIT2); pg=ctx.new_page(); pg.goto(B); pg.wait_for_timeout(1500)
    tag="G"+str(int(time.time())%10000); bid=new_bot("Q"+tag); pg.reload(); pg.wait_for_timeout(1500); s=S(pg,bid,"Q"+tag); s.open(); ta=s.ta
    dock=lambda: pg.evaluate("()=>[document.querySelector('.composer-dock').getBoundingClientRect().height, document.querySelector('.composer').getBoundingClientRect().height]")
    gap=lambda: pg.evaluate("()=>{const s=document.querySelector('.chat-scroll');return Math.round(s.scrollHeight-s.scrollTop-s.clientHeight)}")
    docks={"idle":dock()}
    s.rec(True); s.send(f"LONG please {tag}")
    while s.last_bot_len()<1500: pg.wait_for_timeout(100)
    docks["running"]=dock(); stats=set()
    pg.evaluate("()=>{__lt.length=0;__raf.length=0;window.__track=1}"); t=pg.evaluate("performance.now()")
    ta.click(); ta.type("The quick brown fox jumps over the lazy dog 012345", delay=70); pg.wait_for_timeout(300)
    d=pg.evaluate("()=>({lt:__lt,raf:__raf})"); pg.evaluate("()=>{window.__track=0}"); raf=d["raf"]; lts=[x["d"] for x in d["lt"] if x["t"]>=t]
    log("typing",{"keys":len(raf),"median_ms":round(st.median(raf),2),"p95_ms":round(sorted(raf)[int(.95*len(raf))-1],2),"max_ms":round(max(raf),2),"longtasks":len(lts),"longtask_max":max(lts,default=0),"value_ok":ta.input_value().endswith("012345")})
    docks["typed"]=dock(); pg.screenshot(path=SH2+"G-typing.png"); ta.fill("")
    g=[]; 
    for i in range(12): g.append(gap()); stats.add(re.sub(r"\d+","#",s.status())); pg.wait_for_timeout(250)
    log("autoscroll_gaps",g); log("status_while_streaming",sorted(stats))
    pg.mouse.move(640,300); pg.mouse.wheel(0,-1500); pg.wait_for_timeout(800); top1=pg.evaluate("document.querySelector('.chat-scroll').scrollTop"); pg.wait_for_timeout(1500); top2=pg.evaluate("document.querySelector('.chat-scroll').scrollTop")
    jl=pg.locator("[data-testid=jump-latest]").count(); log("scroll_up",{"top_before":top1,"top_after":top2,"gap":gap(),"jump_latest":jl}); pg.screenshot(path=SH2+"G-scrolled-up.png")
    if jl: pg.locator("[data-testid=jump-latest]").click(); pg.wait_for_timeout(800)
    g=[]; 
    for i in range(5): g.append(gap()); pg.wait_for_timeout(300)
    log("after_jump_gaps",g)
    bh=pg.locator(".send-btn").element_handle()
    ta.click(); pg.keyboard.press("Escape"); t0=time.time()
    while s.btn()!="Send/dis" and time.time()-t0<10: pg.wait_for_timeout(50)
    log("esc",{"stopped_s":round(time.time()-t0,2),"btn":s.btn(),"status":s.status(),"focus":pg.evaluate("document.activeElement&&document.activeElement.id")}); docks["after_esc"]=dock(); pg.screenshot(path=SH2+"G-after-esc.png")
    s.wait_idle(30)
    # Stop button click
    s.send(f"MEDIUM stop click {tag}"); 
    while s.last_bot_len()<300: pg.wait_for_timeout(100)
    lab=s.btn(); pg.locator(".send-btn").click(); t0=time.time()
    while s.btn()!="Send/dis" and time.time()-t0<10: pg.wait_for_timeout(50)
    log("stop_click",{"label_running":lab,"stopped_s":round(time.time()-t0,2),"focus":pg.evaluate("document.activeElement&&document.activeElement.id"),"same_button":pg.evaluate("(e)=>e.isConnected&&e===document.querySelector('.send-btn')",bh)})
    s.wait_idle(30)
    # natural finish
    s.rec(True); s.send(f"MEDIUM finish {tag}")
    while s.last_bot_len()<300: pg.wait_for_timeout(100)
    q=pg.evaluate("()=>{const r=[...document.querySelectorAll('[data-testid=transcript] .message-row[data-role=assistant]')]; return qa(r[r.length-1])}")
    s.wait_idle(90); pg.wait_for_timeout(1500); F=s.frames()
    lens=[next((x[3] for x in f["rows"] if x[0]==q),None) for f in F]; started=[i for i,l in enumerate(lens) if l]; seg=lens[started[0]:] if started else []
    drops=sum(1 for i in range(1,len(seg)) if seg[i] is not None and seg[i-1] is not None and seg[i]<seg[i-1]-20)
    log("finish",{"row_missing_frames":sum(1 for l in seg if l is None),"len_drops":drops,"gap_max":max(f["gap"] or 0 for f in F),"final_len":seg[-1] if seg else None})
    pg.screenshot(path=SH2+"G-finished.png"); docks["after_finish"]=dock()
    # send click focus, up arrow, drafts
    ta.fill("SHORT click send MSG-cs"); pg.locator(".send-btn").click(); pg.wait_for_timeout(300); log("focus_after_send_click",pg.evaluate("document.activeElement&&document.activeElement.id")); s.wait_idle(30)
    ta.fill(""); ta.click(); pg.keyboard.press("ArrowUp"); log("up_arrow",ta.input_value()); ta.fill("")
    bid2=new_bot("Q"+tag+"x"); pg.reload(); pg.wait_for_timeout(2000); s.open()
    ta.click(); ta.type("draft for first"); pg.wait_for_timeout(700)
    s2=S(pg,bid2,"Q"+tag+"x"); s2.open(); v0=ta.input_value(); ta.click(); ta.type("draft for second"); pg.wait_for_timeout(700)
    s.open(); v1=ta.input_value(); pg.reload(); pg.wait_for_timeout(2500); s.open(); v2=ta.input_value(); s2.open(); v3=ta.input_value()
    log("drafts",{"second_initial":v0,"first_after_switch":v1,"first_after_reload":v2,"second_after_reload":v3}); pg.screenshot(path=SH2+"G-drafts.png")
    log("docks",docks); log("cmp",compare(pg,bid)); json.dump(R,open(SH2+"general.json","w"),indent=1); br.close()
