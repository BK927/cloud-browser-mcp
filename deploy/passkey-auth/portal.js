"use strict";
const $ = id => document.getElementById(id);
let csrf = "", state = null, busy = false;
let ticket = location.hash.slice(1);
const flow = new URL(location.href).searchParams.get("flow") || "";
if (ticket) history.replaceState(null, "", location.pathname + location.search);
window.addEventListener("hashchange",()=>{
 if(!location.hash)return;
 ticket=location.hash.slice(1);
 history.replaceState(null,"",location.pathname+location.search);
 refresh().then(()=>status("패스키 등록 버튼을 눌러 시작하세요.")).catch(e=>status(e.message,true));
});
const messages = {
 authentication_failed:"본인 확인에 실패했습니다. 등록한 패스키를 선택해 다시 시도해 주세요.",
 choose_windows_hello:"동기화 패스키 대신 Windows Hello의 ‘이 Windows 기기’를 선택해야 합니다. 새 등록 링크를 발급해 주세요.",
 enrollment_link_expired:"등록 링크가 만료되었거나 이미 사용되었습니다. 새 링크를 발급해 주세요.",
 registration_failed:"패스키 등록을 완료하지 못했습니다. 새 등록 링크로 다시 시도해 주세요.",
 connection_failed_restart_from_chatgpt:"연결 요청이 만료되었거나 완료되지 않았습니다. ChatGPT에서 연결을 다시 시작해 주세요.",
 cannot_remove_last_device_or_not_authenticated:"마지막 패스키는 삭제할 수 없습니다. 로그인 시간이 만료되었다면 다시 본인 확인을 해 주세요.",
 authentication_not_available:"등록된 패스키가 없거나 로그인 시간이 만료되었습니다. 화면을 새로 열어 주세요."
};
const from64 = s => Uint8Array.from(atob(s.replace(/-/g,"+").replace(/_/g,"/") + "=".repeat((4-s.length%4)%4)), c=>c.charCodeAt(0));
const to64 = buffer => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
function decodeOptions(o, register) {
 o.challenge=from64(o.challenge);
 if(register) {o.user.id=from64(o.user.id);(o.excludeCredentials||[]).forEach(c=>c.id=from64(c.id));}
 else (o.allowCredentials||[]).forEach(c=>c.id=from64(c.id));
 return o;
}
function serialize(c) {
 const r=c.response;
 const response={clientDataJSON:to64(r.clientDataJSON)};
 if(r.attestationObject) {response.attestationObject=to64(r.attestationObject);response.transports=r.getTransports?r.getTransports():[];}
 else {response.authenticatorData=to64(r.authenticatorData);response.signature=to64(r.signature);response.userHandle=r.userHandle?to64(r.userHandle):null;}
 return {id:c.id,rawId:to64(c.rawId),type:c.type,response,clientExtensionResults:c.getClientExtensionResults(),authenticatorAttachment:c.authenticatorAttachment};
}
async function api(path, body) {
 const r=await fetch("/auth/api/"+path,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-Passkey-CSRF":csrf},body:JSON.stringify(body)});
 const data=await r.json();
 if(!r.ok) throw new Error(messages[data.error]||"작업을 완료하지 못했습니다. 화면을 다시 열어 주세요.");
 return data;
}
function status(text,error=false) {$('status').textContent=text;$('status').classList.toggle('error',error);}
async function refresh() {
 const r=await fetch("/auth/api/status"+(flow?"?flow="+encodeURIComponent(flow):""),{credentials:"same-origin"});
 if(!r.ok) throw new Error("로그인 화면을 새로 열어 주세요.");
 state=await r.json();csrf=state.csrf;
 const registered=!!state.registration_completed&&!ticket&&!flow;
 $('register').hidden=!ticket;$('login').hidden=!!ticket||(state.authenticated&&!registered)||!state.configured||!!flow;
 $('login').textContent=registered?"패스키 로그인 확인하기":"패스키로 로그인";
 $('registration-result').hidden=!registered;
 $('registered-key').textContent=registered&&state.current_passkey?"등록한 키: "+state.current_passkey.label:"";
 $('tag').textContent=registered?"등록 완료":state.authenticated?"로그인됨":"등록한 패스키로 로그인";
 $('connect').hidden=!!ticket||!flow||!state.configured;
 $('manage').hidden=!!ticket||!state.authenticated||!!flow;
 $('setup-needed').hidden=!!ticket||state.configured;
 if(ticket){$('title').textContent="패스키 등록";$('intro').textContent=state.device_bound?"Windows Hello의 ‘이 Windows 기기’를 선택하고 기존 PIN으로 등록하세요. 이 패스키로 공통 MCP 로그인에 접근할 수 있습니다.":"Google 비밀번호 관리자에 패스키를 저장하세요. 같은 Google 계정으로 패스키가 동기화된 기기에서는 다시 등록하지 않아도 됩니다. PIN이나 지문 등으로 본인 확인을 진행합니다.";}
 else if(flow){$('title').textContent=(state.target||"MCP")+" 연결";$('intro').textContent="등록한 패스키로 본인 확인 후 이 MCP의 ChatGPT 연결을 승인합니다.";}
 else if(registered){$('title').textContent="패스키 등록 완료";$('intro').textContent=state.device_bound?"등록을 완료했습니다. 이제 이 기기의 패스키로 MCP에 로그인할 수 있습니다.":"등록을 완료했습니다. 같은 Google 계정으로 패스키가 동기화된 다른 기기에서도 이 키로 로그인할 수 있습니다.";}
 else {$('title').textContent="MCP 로그인 관리";$('intro').textContent="기기의 PIN이나 지문으로 본인 확인을 합니다. MCP마다 비밀번호를 기억할 필요가 없습니다.";}
 $('devices').replaceChildren();
 for(const device of state.devices){
  const row=document.createElement("div");row.className="device";
  const label=document.createElement("span");label.className="label";label.textContent=device.label;
  const remove=document.createElement("button");remove.textContent="삭제";remove.className="danger";
  remove.disabled=state.devices.length<=1;
    remove.onclick=()=>run(async()=>{if(!confirm(device.label+"의 새 로그인을 차단할까요?"))return;await api("remove",{id:device.id});await refresh();status("패스키를 삭제했습니다.");});
  row.append(label,remove);$('devices').append(row);
 }
 if(!navigator.credentials||!window.PublicKeyCredential){$('register').disabled=true;$('login').disabled=true;$('connect').disabled=true;throw new Error("이 브라우저는 패스키를 지원하지 않습니다. 최신 Chrome 또는 Edge를 사용해 주세요.");}
}
async function authenticate() {
 const start=await api("auth/options",{});
 const credential=await navigator.credentials.get({publicKey:decodeOptions(start.options,false)});
 await api("auth/verify",{ceremony:start.ceremony,credential:serialize(credential)});
 await refresh();
}
async function run(action) {
 if(busy)return;busy=true;
 const buttons=[...document.querySelectorAll("button")];const previous=buttons.map(b=>b.disabled);buttons.forEach(b=>b.disabled=true);
 try {status("기기의 확인 창을 진행해 주세요.");await action();}
 catch(e){status(e.name==="NotAllowedError"?"기기 확인이 취소되었거나 시간이 만료되었습니다.":e.message,true);}
 finally{busy=false;buttons.forEach((b,i)=>b.disabled=previous[i]);}
}
$('login').onclick=()=>run(async()=>{await authenticate();status("본인 확인을 완료했습니다.");});
$('register').onclick=()=>run(async()=>{
 const start=await api("register/options",{ticket});ticket="";
 const credential=await navigator.credentials.create({publicKey:decodeOptions(start.options,true)});
 await api("register/verify",{ceremony:start.ceremony,credential:serialize(credential)});
 history.replaceState(null,"","/auth/");await refresh();status("패스키 등록을 완료했습니다.");
});
$('connect').onclick=()=>run(async()=>{
 if(!state.authenticated)await authenticate();
 const result=await api("connect",{flow});location.assign(result.redirect);
});
$('ticket').onclick=()=>run(async()=>{
 const label=$('label').value.trim();if(!label)throw new Error("등록할 기기의 이름을 입력해 주세요.");
 const result=await api("enroll-ticket",{label});$('ticket-result').hidden=false;$('ticket-url').value=result.url;
 status("새 패스키를 저장할 본인 기기에서 등록 링크를 여세요.");
});
$('logout').onclick=()=>run(async()=>{await api("logout",{});location.reload();});
refresh().then(()=>status(ticket?"패스키 등록 버튼을 눌러 시작하세요.":state.configured?"":"관리자가 발급한 등록 링크를 열어 주세요.")).catch(e=>status(e.message,true));
