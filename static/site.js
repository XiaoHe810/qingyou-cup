"use strict";

const projects = {M:"男子团体", W:"女子团体", X:"混合双打"};
const states = {waiting:"待开放",open:"抽签中",paused:"已暂停",locked:"已锁定",pending:"待比赛",playing:"进行中",done:"已结束"};
const stages = {pre:"附加淘汰赛",quarter:"八强淘汰赛",round:"四强循环赛"};
const reports = {draw:"抽签结果",M:"男子团体完整对阵",W:"女子团体完整对阵",X:"混合双打完整对阵",matchups:"三项目完整交叉对打表",schedule:"两天比赛日程",referees:"裁判工作安排",scores:"比赛成绩汇总",standings:"四强循环赛积分表",awards:"最终获奖名单"};
const auditNames = {order:"设置顺序",open:"开放 / 继续",pause:"暂停",lock:"锁定并生成对阵",redraw:"重抽"};
const tieRules = {head_to_head:"两队直接交锋",rubber_diff:"净胜单项",game_diff:"净胜局",point_diff:"净胜分"};
const main = document.querySelector("#main");
const room = location.pathname.match(/^\/r\/([a-f0-9]+)$/)?.[1];
const root = room ? "/api/r/" + room : "/api";
let me = {}, state, admin, rooms = [], audit = [], route = "home", project = "M", online = true;
let renderedVersion, drawKeys = {}, revealed = new Set(), busy = false, refreshing = false, toastTimer;

const escape = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const teamName = id => state?.teams[id]?.college || "—";
const timeLabel = time => time ? time.slice(0,16).replace("T"," ") : "—";
const chip = key => '<span class="tag '+escape(key)+'">'+escape(states[key] || key)+'</span>';
const msg = (s, error=false) => {
  const box = document.querySelector("#toast");
  box.textContent = s;
  box.className = error ? "error" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.textContent = ""; }, 6000);
};
const field = (label,name,value="",type="text",extra="") => '<label>'+escape(label)+'<input name="'+name+'" type="'+type+'" value="'+escape(value)+'" '+extra+'></label>';
const options = (items, selected) => Object.entries(items).map(([k,v]) =>
  '<option value="'+escape(k)+'" '+(String(k)===String(selected)?"selected":"")+'>'+escape(v)+'</option>').join("");
const select = (label,name,items,selected) => '<label>'+escape(label)+'<select name="'+name+'">'+options(items,selected)+'</select></label>';
const button = (label, action, value="", cls="subtle") => '<button type="button" class="'+cls+'" data-action="'+action+'" data-value="'+escape(value)+'">'+escape(label)+'</button>';
const header = (name, extra="") => '<div class="bar"><h1>'+escape(name)+'</h1>'+extra+'</div>';
const empty = text => '<p class="muted">'+escape(text)+'</p>';
const panel = contents => '<section class="panel">'+contents+'</section>';
const table = (headers, rows, wide=false) => '<div class="scroll"><table class="'+(wide?"wide-table":"")+'"><thead><tr>'+
  headers.map(x=>'<th scope="col">'+escape(x)+'</th>').join("")+'</tr></thead><tbody>'+
  rows.map(row=>'<tr>'+row.map(x=>'<td>'+x+'</td>').join("")+'</tr>').join("")+'</tbody></table></div>';
const tabs = () => '<div class="tabs">'+Object.entries(projects).map(([p,name]) =>
  '<a class="'+(p===project?"active":"")+'" href="#'+route+'/'+p+(route==="live" && location.hash.endsWith("/screen")?"/screen":"")+'">'+name+'</a>').join("")+'</div>';
const sides = m => ["a","b"].map(s => m[s] ? teamName(m[s]) : m[s+"_source"]+" 胜者");
const scoreText = m => !m.score ? "—" : m.score.forfeit ? (m.score.forfeit==="a"?"甲":"乙")+"方弃权" :
  (m.project==="X"?m.score.games:m.score.total).join(" : ");
const copy = async text => {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const box=document.createElement("textarea");
  box.value=text; document.body.append(box); box.select();
  const ok=document.execCommand("copy"); box.remove();
  if(!ok) window.prompt("复制以下内容",text);
};

async function api(path, data) {
  const init = data === undefined ? {} : {method:"POST",headers:{"X-CSRF-Token":me.csrf}};
  if (data instanceof FormData) init.body=data;
  else if (data !== undefined) { init.body=JSON.stringify(data); init.headers["Content-Type"]="application/json"; }
  const response = await fetch(path, init);
  if (!response.ok) {
    const fail=await response.json().catch(()=>({}));
    throw new Error(fail.error || "服务请求失败（"+response.status+"）");
  }
  return response.json();
}

function nav() {
  const items = [["home","赛事总览"],["live","公开抽签大屏"],["join","学院抽签入口"],["slots","签位表"],
                 ["matches","比赛对阵"],["calendar","比赛日程"],["standings","积分榜"],["awards","最终排名"],["export","导出"]];
  if (me.admin && room) items.push(["host","主持控制"],["roster","名单核验"],["planning","日程管理"],
                                  ["refs","裁判排班"],["score","成绩录入"],["audit","操作日志"]);
  else if (room) items.push(["refs","裁判排班"],["score","裁判入口"]);
  document.querySelector("#nav").innerHTML=items.map(([key,label]) =>
    '<a class="'+(key===route?"active":"")+'" href="#'+key+'">'+label+'</a>').join("");
  document.querySelector("#identity").textContent=me.admin?"管理员 · "+me.admin:"管理员登录";
  document.body.classList.toggle("projection",route==="live" && location.hash.endsWith("/screen"));
}

function home() {
  if (!state) return '<section class="hero"><p class="eyebrow">东北石油大学 · 研究生体育赛事</p><h1>第四届“青油杯”<br>研究生乒乓球比赛</h1><p>2026年11月14–15日 · 东北石油大学乒乓球馆</p></section>'+
    panel('<h2>赛事房间</h2>'+(rooms.length?'<ul class="list">'+rooms.map(r=>'<li><a href="/r/'+r.id+'">'+escape(r.title)+'</a></li>').join("")+'</ul>':empty("暂无赛事房间"))+
      '<form id="room-code" class="narrow">'+field("房间邀请码","room_code","","text","required")+'<div><button>进入房间</button></div></form>')+
    (me.admin?panel('<h2>创建抽签房间</h2><form id="new-room" class="narrow">'+field("房间名称","title","东北石油大学第四届“青油杯”研究生乒乓球比赛")+
      field("报名工作簿","workbook","","file",'accept=".xlsx" required')+'<div><button>导入名单并创建</button></div></form>'):"");
  return '<section class="hero"><p class="eyebrow">东北石油大学 · 研究生体育赛事</p><h1>第四届“青油杯”<br>研究生乒乓球比赛</h1><p>'+escape(state.dates.join(' / '))+' · '+escape(state.venue)+'</p>'+
    '<div class="actions"><a class="button" href="#live">观看公开抽签</a><a class="outline" href="#join">学院负责人入口</a></div></section>'+
    '<div class="grid">'+Object.entries(state.projects).map(([p,x])=>panel('<div class="bar"><h2>'+projects[p]+'</h2>'+chip(x.state)+'</div>'+
      '<div class="metric">'+x.draws.length+' / '+x.order.length+'</div><p class="muted">已抽签 / 报名数</p><a class="subtle button" href="#slots/'+p+'">查看签位</a>')).join("")+'</div>'+
    panel('<h2>比赛阶段</h2><p>附加淘汰赛 → 八强淘汰赛 → 四强单循环赛</p><div class="actions"><a class="button subtle" href="#matches">查看比赛对阵</a><a class="button subtle" href="#calendar">查看已发布日程</a></div>');
}

function live() {
  const p=state.projects[project], d=p.draws.at(-1), key=d?[p.revision,d.seq,d.slot].join("-"):"";
  const animate=key && (revealed.has(project) || drawKeys[project]!==key);
  drawKeys[project]=key; revealed.delete(project);
  const remaining=p.order.filter(id=>!p.draws.some(d=>d.team===id));
  const next=p.ordered?remaining[0]:null;
  return header("公开抽签大屏",'<div class="actions"><span class="online" id="connection">实时连接</span><a class="button subtle" href="#live/'+project+(location.hash.endsWith("/screen")?'">标准视图':'/screen">投屏视图')+'</a></div>')+
    tabs()+'<div class="live"><p class="eyebrow">'+projects[project]+' · '+states[p.state]+'</p><div class="live-card '+(animate?"reveal":"")+'">'+
      '<div class="ball">'+(d?String(d.slot).padStart(2,"0"):"·")+'</div><div><h2>'+(d?escape(d.college):"等待抽签")+'</h2>'+
      '<p class="muted">'+(d?"第 "+d.seq+" 次抽签 · "+timeLabel(d.drawn_at):"签位将在负责人抽签后实时显示")+'</p></div></div></div>'+
    '<div class="split">'+panel('<h2>抽签进度 <span class="tag">'+p.draws.length+' / '+p.order.length+'</span></h2><div class="meter"><span style="width:'+100*p.draws.length/p.order.length+'%"></span></div>'+
      (p.draws.length?'<ul class="list">'+[...p.draws].reverse().map(x=>'<li><span>'+x.seq+' · '+escape(x.college)+'</span><strong>签位 '+x.slot+'</strong></li>').join("")+'</ul>':empty("尚无抽签记录")))+
    panel('<h2>尚未抽签</h2>'+(remaining.length?(next?'<p class="muted">下一位 · '+escape(teamName(next))+'</p>':"")+
      '<ul class="list">'+remaining.map(t=>'<li>'+escape(teamName(t))+'</li>').join("")+'</ul>':empty("所有学院均已完成")))+ '</div>';
}

function join() {
  const rep=me.rep?.room===room && me.rep.college?me.rep:null;
  const colleges=Object.fromEntries([...new Set(Object.values(state.teams).map(t=>t.college))].map(x=>[x,x]));
  if (!rep) return header("学院负责人抽签入口")+panel('<form id="represent" class="narrow">'+
    '<label>学院名称<select name="college" required>'+options({"":"请选择学院",...colleges},"")+'</select></label>'+
    field("联系人姓名","name","","text","required maxlength=50 autocomplete=name")+'<div><button>进入抽签</button></div></form>');
  const entries=Object.values(state.teams).filter(t=>t.college===rep.college);
  return header(rep.college,button("重新登记","change-rep"))+
    '<p class="muted">联系人 '+escape(rep.name)+'</p><div class="grid">'+entries.map(team=>{
      const p=state.projects[team.project], d=p.draws.find(x=>x.team===team.id);
      const available=!d && p.state==="open" && (!p.ordered || p.order[p.draws.length]===team.id);
      const animate=d && revealed.has(team.project);
      revealed.delete(team.project);
      return panel('<div class="bar"><h2>'+projects[team.project]+'</h2>'+chip(p.state)+'</div>'+
        (d?'<div class="live-card '+(animate?"reveal":"")+'"><div class="ball">'+String(d.slot).padStart(2,"0")+'</div>'+
          '<div><strong>已抽签</strong><p class="small muted">第 '+d.seq+' 位 · '+timeLabel(d.drawn_at)+'</p></div></div>'+
          '<p><a class="button subtle" href="#matches/'+team.project+'">查看对阵</a></p>':
          '<p>'+ (available?"现在可以抽签":p.state==="open"?"等待轮到本学院":"等待主持人开放抽签")+'</p>'+
          '<button type="button" data-action="draw" data-value="'+team.project+'" '+(available?"":"disabled")+'>开始抽签</button>'));
    }).join("")+'</div>';
}

function slots() {
  const p=state.projects[project], slots=Object.fromEntries(p.draws.map(d=>[d.slot,d]));
  return header("签位表",chip(p.state))+tabs()+panel('<div class="bracket">'+Array.from({length:8},(_,i)=>
    '<div class="pair">'+[i*2+1,i*2+2].map(n=>'<div class="slot '+(p.byes.includes(n)?"bye":"")+'"><b>'+
      String(n).padStart(2,"0")+'</b>'+escape(slots[n]?.college || (p.byes.includes(n)?"轮空 BYE":"待抽取"))+'</div>').join("")+
      (p.byes.includes(i*2+1)||p.byes.includes(i*2+2)?'<div class="slot bye small">自动轮空晋级</div>':"")+'</div>').join("")+'</div>');
}

function matchTable(items, scheduled=false) {
  return items.length?table(["编号","阶段","甲方","乙方","状态","开始 / 球台","裁判","比分","获胜方","晋级位置"],items.map(m=>{
    const s=m.schedule;
    return [m.code,stages[m.stage],...sides(m).map(escape),chip(m.status),s?timeLabel(s.start)+' / '+s.court+'号台':"—",
            escape(s?.official||"—"),escape(scoreText(m)),escape(teamName(m.winner)),escape(m.next.join(" / ")||"—")];
  }),true):empty(scheduled?"比赛日程待管理员确认发布":"抽签锁定后生成比赛对阵");
}

function matches() {
  return header("完整比赛对阵",'<div class="actions"><a class="button subtle" href="'+root+'/export/matchups/xlsx">全部对阵 Excel</a>'+
    '<a class="button subtle" href="'+root+'/export/matchups/pdf">全部对阵 PDF</a></div>')+tabs()+Object.entries(stages).map(([stage,label])=>panel('<h2>'+label+'</h2>'+
    matchTable(state.matches.filter(m=>m.project===project && m.stage===stage)))).join("");
}

function calendar() {
  const items=state.matches.filter(m=>m.schedule).sort((a,b)=>a.schedule.start.localeCompare(b.schedule.start)||a.schedule.court-b.schedule.court);
  const days=[...new Set(items.map(m=>m.schedule.start.slice(0,10)))];
  return header("比赛日程",'<span class="tag">'+(items.length?"已发布":"待确认")+'</span>')+
    '<p class="muted">'+escape(state.venue)+' · 北京时间</p>'+
    (days.length?days.map(d=>panel('<h2>'+d+'</h2>'+matchTable(items.filter(m=>m.schedule.start.startsWith(d)),true))).join(""):panel(matchTable([],true)));
}

function rankings(awards=false) {
  const p=state.projects[project], list=awards?p.ranking:p.standings;
  const decision=awards && me.admin && p.complete && !p.ranking.length ?
    panel('<h2>裁判组同分裁决</h2><form id="tie-decision" class="narrow">'+p.standings.map((r,i)=>
      select("第 "+(i+1)+" 名","rank_"+i,Object.fromEntries(p.standings.filter(s=>s.tie_group===r.tie_group)
        .map(s=>[s.team,teamName(s.team)])),r.team)).join("")+
      field("裁判组裁定依据","basis","","text","required maxlength=1000")+
      '<label class="actions"><input style="width:auto;min-height:0" type="checkbox" name="confirmed" required>确认公布该裁决</label><div><button>保存最终名次</button></div></form>'):"";
  return header(awards?"最终成绩排名":"四强循环积分榜")+tabs()+panel(
    (p.complete && !p.ranking.length?'<p class="warnbox">循环赛已结束。同积分名次待确认判定规则或裁判组裁决。</p>':"")+
    (list.length?table(awards?["名次","学院"]:["学院","场次","胜","负","弃权","积分","名次"],list.map(r=>
      awards?[r.rank,escape(teamName(r.team))]:[escape(teamName(r.team)),r.played,r.wins,r.losses,r.forfeits,r.points,r.rank||"待定"])):empty(awards?"四强循环赛全部完成且名次确认后公布":"四强产生后显示循环赛积分")))+decision;
}

function exportPage() {
  return header("Excel / PDF 导出")+panel(table(["报表","Excel","PDF"],Object.entries(reports).map(([key,label])=>[label,
    '<a class="button subtle" href="'+root+'/export/'+key+'/xlsx">下载 Excel</a>',
    '<a class="button subtle" href="'+root+'/export/'+key+'/pdf">下载 PDF</a>'])));
}

function loginPage() {
  return header("管理员登录")+panel(me.admin?'<p>当前账号 · '+escape(me.admin)+'</p>'+button("退出登录","logout")+
    ' <a class="button subtle" href="/">创建或切换赛事房间</a>':
    '<form id="login" class="narrow">'+field("账号","username","","text","required autocomplete=username")+
      field("密码","password","","password","required autocomplete=current-password")+'<div><button>登录</button></div></form>');
}

function host() {
  const p=state.projects[project];
  const registered=Object.fromEntries(admin.participants.map(x=>[x.team,x.name]));
  const mode=p.ordered?"按指定顺序":"开放后自由抽签";
  return header("主持控制",chip(p.state))+tabs()+
    panel('<div class="bar"><h2>抽签流程</h2><span class="tag">第 '+p.revision+' 轮 · '+p.draws.length+' / '+p.order.length+'</span></div>'+
    '<div class="actions">'+(p.state==="locked"?[]:p.state==="open"?["pause"]:["open"]).concat(p.state==="waiting"?[]:["redraw"])
    .map(a=>button(auditNames[a],"host",a,a==="redraw"?"danger":"subtle")).join("")+
    button("复制统一抽签入口","join-link")+'</div>'+
    '<p class="small muted">'+mode+' · 抽齐后自动锁定并生成对阵</p>')+
    panel('<h2>学院及联系人</h2><ol class="list" id="order">'+p.order.map(id=>'<li class="round-row" data-team="'+id+'"><span>'+
      escape(teamName(id))+(registered[id]?' <small class="muted">· '+escape(registered[id])+'</small>':"")+'</span>'+
      '<span class="actions">'+(p.state==="waiting"?button("↑","order-move","up")+button("↓","order-move","down"):"")+
      (registered[id]?button("重置登记","reset-rep",id):"")+'</span></li>').join("")+'</ol>'+
      (p.state==="waiting"?'<div class="actions">'+button("保存并按此顺序","order-save")+button("使用自由抽签","order-free")+'</div>':""));
}

function rosterPage() {
  const teams=admin.teams.filter(t=>t.project===project);
  return header("报名名单核验")+tabs()+panel('<p>已核验 '+teams.filter(t=>t.verified).length+' / '+teams.length+'</p>'+
    '<p class="small muted">来源 SHA-256 '+escape(admin.source.sha256)+'</p>'+
    (admin.source.identity_issues.length?'<p class="warnbox">源表存在 '+admin.source.identity_issues.length+' 条身份核验记录，请查看导入核验报告。</p>':"")+
    teams.map(t=>'<details class="details"><summary>'+escape(t.college)+' · '+(t.verified?"已核验":"待核验")+'</summary>'+
      '<form data-form="roster" data-team="'+t.id+'">'+t.players.map(p=>'<div class="form-row">'+field("原表第 "+p.row+" 行姓名","name_"+p.row,p.name,"text","required")+
        select("队员类别","role_"+p.row,t.project==="X"?{"混双选手":"混双选手"}:{"正式队员":"正式队员","替补队员":"替补队员"},p.role)+
        (t.project==="X"?select("性别","sex_"+p.row,{"":"待核验","男":"男","女":"女"},p.sex):"")+'</div>'+
        (p.sex_source?'<small class="muted">'+escape(p.sex_source)+'</small>':"")).join("")+
      field("核验依据 / 修改原因","reason","","text","required")+'<div><button>保存核验</button></div></form></details>').join(""));
}

function settingsForm() {
  const s=admin.settings;
  return '<form id="settings"><div class="form-row">'+field("比赛地点","venue",s.venue,"text","required")+field("球台数量","courts",s.courts,"number","min=1 max=64 required")+
    field("每场预计用时（分钟）","minutes",s.minutes,"number","min=1 required")+field("选手休息（分钟）","rest",s.rest,"number","min=0 required")+'</div>'+
    '<label>场次（每行 日期 开始 结束）<textarea name="sessions" required>'+s.sessions.map(x=>escape([x.date,x.start,x.end].join(" "))).join("\n")+'</textarea></label>'+
    '<label class="actions"><input style="width:auto;min-height:0" type="checkbox" name="final_day" '+(s.final_day?"checked":"")+'>四强循环赛优先安排在最后一个比赛日</label>'+
    '<div class="form-row">'+field("胜场积分","win",s.points.win,"number","min=0 required")+field("负场积分","loss",s.points.loss,"number","min=0 required")+
    field("弃权积分","forfeit",s.points.forfeit,"number","min=0 required")+'</div>'+
    '<h3>同积分判定顺序</h3><div class="form-row">'+Array.from({length:4},(_,i)=>select("第 "+(i+1)+" 项","tie_"+i,{"":"不使用",...tieRules},s.tie[i]||"")).join("")+'</div>'+
    field("规则变更依据","tie_basis")+'<label class="actions"><input style="width:auto;min-height:0" type="checkbox" name="confirm_tie">确认同分规则变更</label>'+
    '<h3>计分格式</h3>'+Object.entries(projects).map(([p,name])=>'<div class="form-row">'+
      select(name+" · 最多单项数","rubbers_"+p,p==="X"?{1:1}:{1:1,3:3,5:5,7:7},s.formats[p].rubbers)+
      select(name+" · 每单项最多局数","games_"+p,{1:1,3:3,5:5,7:7},s.formats[p].games)+'</div>').join("")+
    '<div><button>保存赛事设置</button></div></form>';
}

function planning() {
  const officials={"":"未安排",...Object.fromEntries(admin.officials.map(o=>[o.id,o.name]))};
  const byCode=Object.fromEntries(state.matches.map(m=>[m.code,m]));
  return header("比赛日程管理")+panel('<h2>编排参数</h2><p class="warnbox">初始球台、比赛时长及场次结束时间为排程默认值，请按现场条件核对。</p>'+settingsForm())+
    panel('<div class="bar"><h2>日程草案 <span class="tag warn">待确认</span></h2><div class="actions">'+button("生成 / 重新编排","plan","build")+button("确认并发布","plan","publish","")+'</div></div>'+
    (admin.draft.length?'<form id="draft"><div class="scroll"><table class="wide-table"><thead><tr><th>编号</th><th>对阵</th><th>开始</th><th>球台</th><th>裁判</th></tr></thead><tbody>'+
      admin.draft.map(x=>{const m=byCode[x.code]; const disabled=m.status==="pending"?"required":"disabled";
        return '<tr data-code="'+x.code+'"><td>'+x.code+'</td><td>'+sides(m).map(escape).join(" / ")+'</td><td><input aria-label="'+x.code+' 开始时间" name="start_'+x.code+'" type="datetime-local" value="'+escape(x.start.slice(0,16))+'" '+disabled+'></td>'+
          '<td><input aria-label="'+x.code+' 球台" name="court_'+x.code+'" type="number" min="1" max="'+admin.settings.courts+'" value="'+x.court+'" '+disabled+'></td>'+
          '<td><select aria-label="'+x.code+' 裁判" name="ref_'+x.code+'" '+(m.status==="pending"?"":"disabled")+'>'+options(officials,x.referee||"")+'</select></td></tr>';
      }).join("")+'</tbody></table></div><div><button>保存调整并检查冲突</button></div></form>':empty("暂无待确认日程")));
}

function referees() {
  const items=state.matches.filter(m=>m.schedule).sort((a,b)=>(a.schedule.official||"").localeCompare(b.schedule.official||"")||a.schedule.start.localeCompare(b.schedule.start));
  return header("裁判排班表")+(me.admin?panel('<h2>裁判登记</h2><form id="officials" class="narrow"><label>姓名（每行一人）<textarea name="names" required></textarea></label><div><button>加入裁判名单</button></div></form>'+
    '<ul class="list">'+admin.officials.map(o=>'<li>'+escape(o.name)+button("复制登录凭证","ref-invite",o.id)+'</li>').join("")+'</ul>'):"")+
    panel('<h2>已发布安排</h2>'+matchTable(items,true));
}

function scores() {
  const ref=me.referee?.room===room?me.referee:null;
  if (!me.admin && !ref) return header("裁判登录")+panel('<form id="ref-login" class="narrow">'+
    field("裁判专属凭证","invite","","password","required")+'<div><button>进入成绩录入</button></div></form>');
  const matches=state.matches.filter(m=>m.project===project && (me.admin || m.schedule?.referee===ref.id));
  return header("比赛成绩录入")+tabs()+panel(matches.length?matches.map(m=>{
    const pending=admin?.pending[m.code];
    const canEdit=m.status==="playing" || m.status==="done" && me.admin;
    const old=pending||m.score;
    const text=old?.rubbers.map(r=>r.map(game=>game.join("-")).join(", ")).join("\n")||"";
    const info=sides(m).map(escape).join(" / ");
    return '<details class="details"><summary>'+m.code+' · '+info+' · '+states[m.status]+(pending?" · 成绩待确认":"")+'</summary>'+
      '<p class="small muted">'+escape(m.schedule?.official||"裁判待安排")+' · '+escape(scoreText(m))+'</p>'+
      (m.status==="pending"?button("登记开赛","start",m.code):"")+
      (canEdit?'<form data-form="score" data-code="'+m.code+'">'+select("弃权","forfeit",{"":"正常计分",a:"甲方弃权",b:"乙方弃权"},old?.forfeit||"")+
        '<label>每局比分（每个单项一行，同一单项用逗号分隔各局）<textarea name="rubbers" placeholder="11-7, 11-8">'+escape(text)+'</textarea></label>'+
        (m.status==="done"?field("更正原因","reason","","text","required"):"")+
        (me.admin?'<label class="actions"><input style="width:auto;min-height:0" type="checkbox" name="confirmed" '+(m.status==="done"?"required":"")+'>确认成绩并更新晋级</label>':"")+
        '<div class="actions"><button>提交成绩</button>'+(pending?button("确认裁判提交","confirm-score",m.code):"")+'</div></form>':"")+'</details>';
  }).join(""):empty("此项目暂无可管理比赛"));
}

function logs() {
  return header("抽签记录与操作日志",button("下载数据库备份","backup"))+panel(audit.length?audit.map(x=>
    '<details class="details"><summary>'+timeLabel(x.at)+' · '+escape(x.actor)+' · '+escape(x.action)+'</summary><pre>'+
    escape(JSON.stringify(x.detail,null,2))+'</pre></details>').join(""):empty("暂无操作日志"));
}

const pages={home,live,join,slots,matches,calendar,standings:()=>rankings(),awards:()=>rankings(true),
             export:exportPage,login:loginPage,host,roster:rosterPage,planning,refs:referees,score:scores,audit:logs};
const adminPages=new Set(["host","roster","planning","audit"]);

function render() {
  nav();
  if (!state && !["home","login"].includes(route)) {
    main.innerHTML=header("请选择赛事房间")+'<a class="button" href="/">返回赛事房间</a>';
  } else if (adminPages.has(route) && !me.admin) {
    main.innerHTML=header("需要管理员权限")+'<a class="button" href="#login">管理员登录</a>';
  } else {
    main.innerHTML=pages[route]();
  }
  main.setAttribute("aria-busy","false");
  renderedVersion=state?.version;
  showConnection();
}

function showConnection() {
  const icon=document.querySelector("#connection");
  if (icon) { icon.classList.toggle("offline",!online); icon.textContent=online?"实时连接":"连接中断，正在重试"; }
}

async function refresh(force=false) {
  if (refreshing) {
    if (!force) return;
    while (refreshing) await new Promise(resolve=>setTimeout(resolve,30));
  }
  refreshing=true;
  try {
    if (!room) rooms=await api("/api/rooms");
    else state=await api(root);
    online=true;
    const passive=["home","live","slots","matches","calendar","standings","awards","refs","join","score","host"].includes(route);
    const editing=main.querySelector("details[open]") || main.contains(document.activeElement) && document.activeElement.matches("input,select,textarea");
    if (force || passive && renderedVersion!==state?.version && !editing && !busy) {
      if (me.admin && room) admin=await api(root+"/admin");
      if (me.admin && route==="audit" && room) audit=await api(root+"/audit");
      render();
    }
  } catch (err) {
    online=false;
    if (force) {
      if (!state && room) main.innerHTML=panel("<h1>无法连接赛事房间</h1>"+empty(err.message)+'<a class="button" href="/">返回赛事房间</a>');
      msg(err.message,true);
    }
  } finally { refreshing=false; showConnection(); }
}

function changeRoute() {
  const [key,p]=location.hash.slice(1).split("/");
  route=Object.hasOwn(pages,key)?key:"home";
  if(Object.hasOwn(projects,p)) project=p;
  else if(route==="score" && me.referee?.room===room && !me.admin) project="M";
  return refresh(true);
}

async function mutate(path, data, success="操作已保存") {
  await api(path,data);
  msg(success);
  await refresh(true);
}

document.addEventListener("click", async e=>{
  const b=e.target.closest("button[data-action]");
  if(!b || busy) return;
  const {action,value}=b.dataset;
  busy=true; b.disabled=true;
  try {
    if(action==="share") {await copy(location.origin+location.pathname);msg("已复制赛事链接");}
    else if(action==="logout") {await api("/api/logout",{});me=await api("/api/session");location.hash="home";await refresh(true);}
    else if(action==="draw") {
      const p=value;
      const result=await api(root+"/draw",{project:p,revision:state.projects[p].revision});
      revealed.add(p); msg("抽签成功 · 签位 "+result.slot);
      await refresh(true);
    } else if(action==="host"){
      const data={project,action:value};
      if(value==="redraw"){
        data.reason=prompt("请输入重抽原因");
        if(!data.reason?.trim()) return;
        data.confirm=prompt("本轮签位将归档。请输入「确认重抽」");
        if(data.confirm!=="确认重抽") return;
      }
      await mutate(root+"/admin/draw",data);
    } else if(action==="order-move"){
      const item=b.closest("li");
      if(value==="up"&&item.previousElementSibling) item.before(item.previousElementSibling);
      if(value==="down"&&item.nextElementSibling) item.after(item.nextElementSibling);
    } else if(action==="order-save" || action==="order-free"){
      await mutate(root+"/admin/draw",{project,action:"order",ordered:action==="order-save",
        order:[...document.querySelectorAll("#order li")].map(x=>x.dataset.team)});
    } else if(action==="join-link"){
      await copy(location.origin+location.pathname+"#join");msg("已复制统一抽签入口");
    } else if(action==="reset-rep"){
      const reason=prompt("填写重置 "+teamName(value)+" 联系人登记的原因");
      if(reason?.trim()) await mutate(root+"/admin/represent",{college:teamName(value),reason});
    } else if(action==="change-rep"){
      delete me.rep;render();
    } else if(action==="ref-invite"){
      await copy(admin.officials.find(x=>x.id===value).invite_code);msg("已复制裁判专属凭证");
    } else if(action==="plan"){
      if(value==="publish"&&!confirm("确认向公开页面发布当前日程草案？")) return;
      await mutate(root+"/admin/schedule",{action:value,confirmed:value==="publish"});
    } else if(action==="start" || action==="confirm-score"){
      await mutate(root+"/match",{code:value,action:action==="start"?"start":"confirm"});
    } else if(action==="backup") location.assign("/api/backup");
  } catch(err){msg(err.message,true);} finally {busy=false;b.disabled=false;}
});

document.addEventListener("submit", async e=>{
  e.preventDefault();
  if(busy)return;
  busy=true;
  const f=e.target, d=Object.fromEntries(new FormData(f)), submit=f.querySelector("button");
  if(submit)submit.disabled=true;
  try{
    if(f.id==="login"){await api("/api/login",d);me=await api("/api/session");location.hash=room?"host":"home";await refresh(true);}
    else if(f.id==="room-code"){const r=await api("/api/join",d);location.href="/r/"+r.room+"#join";}
    else if(f.id==="new-room"){const r=await api("/api/rooms",new FormData(f));location.href="/r/"+r.room+"#roster";}
    else if(f.id==="represent"){
      await api(root+"/represent",d);
      me=await api("/api/session");await refresh(true);msg("已登记");
    } else if(f.dataset.form==="roster"){
      const t=admin.teams.find(t=>t.id===f.dataset.team);
      await mutate(root+"/admin/roster",{team:t.id,reason:d.reason,players:t.players.map(p=>({
        row:p.row,name:d["name_"+p.row],role:d["role_"+p.row],sex:d["sex_"+p.row]||""}))});
    } else if(f.id==="settings"){
      await mutate(root+"/admin/settings",{venue:d.venue,courts:+d.courts,minutes:+d.minutes,rest:+d.rest,
        sessions:d.sessions.trim().split(/\n+/).map(line=>{const [date,start,end]=line.trim().split(/\s+/);return {date,start,end};}),
        final_day:!!d.final_day,points:{win:+d.win,loss:+d.loss,forfeit:+d.forfeit},tie:[d.tie_0,d.tie_1,d.tie_2,d.tie_3].filter(Boolean),
        confirm_tie:!!d.confirm_tie,tie_basis:d.tie_basis,
        formats:Object.fromEntries(Object.keys(projects).map(p=>[p,{rubbers:+d["rubbers_"+p],games:+d["games_"+p]}]))});
    } else if(f.id==="draft"){
      const items=[...f.querySelectorAll("tr[data-code]")].map(x=>x.dataset.code).filter(k=>d["start_"+k])
        .map(k=>({code:k,start:d["start_"+k],court:+d["court_"+k],referee:d["ref_"+k]}));
      await mutate(root+"/admin/schedule",{action:"edit",items});
    } else if(f.id==="tie-decision"){
      await mutate(root+"/admin/tie-decision",{project,order:[d.rank_0,d.rank_1,d.rank_2,d.rank_3],basis:d.basis,confirmed:!!d.confirmed});
    } else if(f.id==="officials"){
      await mutate(root+"/admin/officials",{names:d.names.split(/\n+/)});
    } else if(f.id==="ref-login"){
      await api(root+"/referee",d);me=await api("/api/session");await refresh(true);
    } else if(f.dataset.form==="score"){
      const rubbers=d.rubbers.trim().split(/\n+/).filter(Boolean).map(line=>line.trim().split(/[,，]/).map(game=>{
        const match=game.trim().match(/^(\d+)\s*[-:：]\s*(\d+)$/);
        if(!match)throw new Error("比分请使用 11-7 格式");
        return [+match[1],+match[2]];
      }));
      await mutate(root+"/match",{action:"submit",code:f.dataset.code,forfeit:d.forfeit,rubbers,
                                  confirmed:!!d.confirmed,reason:d.reason||""});
    }
  } catch(err){msg(err.message,true);} finally{busy=false;if(submit)submit.disabled=false;}
});

(async function boot(){
  try{
    me=await api("/api/session");
    await changeRoute();
    window.addEventListener("hashchange",changeRoute);
    window.addEventListener("online",()=>refresh());
    document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
    if(room)setInterval(()=>refresh(),1500);
  }catch(err){main.innerHTML=panel("<h1>无法连接赛事服务</h1>"+empty(err.message));}
})();
