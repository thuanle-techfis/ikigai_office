/* ikigai office – local runtime.
   Provides window.claude.use('db' | 'user' | 'room' | 'assets') on top of server.py,
   so the page behaves the same as on claude.ai: shared layout, live presence, chat,
   TV queue, chairman meeting and visitor tours, all across every browser on the network. */
(function(){
  'use strict';
  const rid=()=>Math.random().toString(36).slice(2,10)+Math.random().toString(36).slice(2,6);
  const peerId='p'+rid();
  let uid=null;
  try{uid=localStorage.getItem('vp8local:uid');}catch(e){}

  const api=async(method,url,body,headers)=>{
    const r=await fetch(url,{method,headers:headers||(body!==undefined?{'Content-Type':'application/json'}:{}),body:body===undefined?undefined:(body instanceof Blob?body:JSON.stringify(body))});
    const j=await r.json().catch(()=>({}));
    if(!r.ok){const e=new Error(j.error||('http_'+r.status));e.code=j.error||'unavailable';throw e;}
    return j;
  };
  const authHeaders=extra=>({...((extra)||{})});
  function setAuth(data){uid=data&&data.uid||null;try{if(uid)localStorage.setItem('vp8local:uid',uid);else localStorage.removeItem('vp8local:uid');}catch(e){}}
  function clearAuth(){setAuth(null);}

  /* ---------- live events from the server ---------- */
  const docSubs=new Map();   // doc path -> Set(fn)
  const colSubs=new Map();   // collection path -> Set(fn)
  const topicSubs=new Map(); // topic -> Set(fn)
  const peerSubs=new Set();
  let lastPeers=[],myPresence=null,es=null,streamOpened=false;
  function resync(){docSubs.forEach(set=>set.forEach(fn=>fn()));colSubs.forEach(set=>set.forEach(fn=>fn()));}
  /* The live connection (re)opened: announce ourselves again, and after a gap re-read
     everything, because changes made while we were away were never pushed to us. */
  function onStreamOpen(){
    if(myPresence!==null)api('POST','/api/room/presence',{peer:peerId,by:uid,presence:myPresence}).catch(()=>{});
    if(streamOpened)resync();
    streamOpened=true;
  }
  /* Two transports, same events: Server-Sent Events, or long-polling for proxies that hold
     text/event-stream back (Cloudflare quick tunnels do). The server picks the default. */
  function connect(){
    if(!uid||es)return;
    if(window.IKIGAI_CONFIG&&window.IKIGAI_CONFIG.transport==='poll')startPolling();else startSse();
  }
  function startSse(){
    const source=new EventSource('/api/events?peer='+peerId);let gotData=false;
    es={close(){source.close();}};
    source.onopen=()=>onStreamOpen();
    es.source=source;
    // The server sends the online list as soon as the stream opens. Nothing within a few
    // seconds means a proxy is holding the stream back: switch this tab to long-polling.
    setTimeout(()=>{if(!gotData&&es&&es.source===source){source.close();es=null;startPolling();}},8000);
    source.onmessage=ev=>{gotData=true;let m;try{m=JSON.parse(ev.data);}catch(e){return;}handleEvent(m);};
  }
  function startPolling(){
    let stopped=false,failures=0,ctrl=null;
    es={close(){stopped=true;try{ctrl&&ctrl.abort();}catch(e){}}};
    (async()=>{
      while(!stopped){
        try{
          ctrl=new AbortController();
          const r=await fetch('/api/poll?peer='+peerId,{cache:'no-store',signal:ctrl.signal});
          if(r.status===401){stopped=true;break;}
          if(!r.ok)throw new Error('http_'+r.status);
          const j=await r.json();
          // fresh: the server (re)registered this tab, e.g. first poll or after a sleep/network gap
          if(j.fresh||failures)onStreamOpen();
          failures=0;
          (j.events||[]).forEach(m=>{try{handleEvent(m);}catch(e){console.error(e);}});
        }catch(e){
          if(stopped)break;
          failures++;await new Promise(res=>setTimeout(res,Math.min(10000,800*failures)));
        }
      }
    })();
  }
  function handleEvent(m){
      if(!m||typeof m!=='object')return;
      if(m.type==='resync'){resync();return;} // we fell behind and the server dropped our backlog
      if(m.type==='db'){
        (docSubs.get(m.path)||[]).forEach(fn=>fn());
        const col=m.path.split('/').slice(0,-1).join('/');(colSubs.get(col)||[]).forEach(fn=>fn());
      }else if(m.type==='room'){
        (topicSubs.get(m.topic)||[]).forEach(fn=>{try{fn({topic:m.topic,peer:m.peer,by:m.by,data:m.data,sameTab:m.peer===peerId});}catch(e){console.error(e);}});
      }else if(m.type==='peers'){
        const list=(m.peers||[]).map(p=>({peer:p.peer,by:p.by,presence:p.presence,sameTab:p.peer===peerId,isMe:p.by===uid,kind:'viewer'}));
        const before=new Set(lastPeers.map(p=>p.peer)),now=new Set(list.map(p=>p.peer));
        const joined=list.filter(p=>!before.has(p.peer)),left=lastPeers.filter(p=>!now.has(p.peer));
        lastPeers=list;
        peerSubs.forEach(fn=>{try{fn({peers:list,joined,left});}catch(e){console.error(e);}});
      }
  }
  /* ---------- db ---------- */
  const snapDoc=(id,j)=>({id,exists:!!j.exists,data:()=>j.exists?JSON.parse(JSON.stringify(j.data)):undefined,version:j.version,metadata:{hasPendingWrites:false,fromCache:false}});
  function doc(path){
    const id=path.split('/').pop();
    const ref={
      id,path,
      get:async()=>snapDoc(id,await api('GET','/api/db/doc?path='+encodeURIComponent(path))),
      set:async data=>{await api('POST','/api/db/doc?path='+encodeURIComponent(path),{data},authHeaders());},
      update:async data=>{await api('POST','/api/db/doc?path='+encodeURIComponent(path),{data,merge:true},authHeaders());},
      delete:async()=>{await api('POST','/api/db/doc?path='+encodeURIComponent(path),{delete:true},authHeaders());},
      onSnapshot(cb,err){
        // Rapid changes trigger overlapping GETs that can resolve out of order;
        // only the newest request may deliver a snapshot.
        let live=true,issued=0,applied=0;
        const pull=()=>{const seq=++issued;return ref.get().then(s=>{if(live&&seq>applied){applied=seq;cb(s);}}).catch(e=>{if(live&&err&&seq>applied)err(e);});};
        if(!docSubs.has(path))docSubs.set(path,new Set());docSubs.get(path).add(pull);pull();
        return ()=>{live=false;docSubs.get(path).delete(pull);};
      }
    };
    return ref;
  }
  function collection(path){
    return {
      path,
      doc:id=>doc(path+'/'+id),
      onSnapshot(cb,err){
        let live=true,issued=0,applied=0;
        const pull=()=>{const seq=++issued;return api('GET','/api/db/collection?path='+encodeURIComponent(path)).then(j=>{
          if(!live||seq<=applied)return;applied=seq;const docs=j.docs.map(d=>snapDoc(d.id,{exists:true,data:d.data,version:d.version}));
          cb({docs,size:docs.length,empty:!docs.length,metadata:{hasPendingWrites:false,fromCache:false}});
        }).catch(e=>{if(live&&err&&seq>applied)err(e);});};
        if(!colSubs.has(path))colSubs.set(path,new Set());colSubs.get(path).add(pull);pull();
        return ()=>{live=false;colSubs.get(path).delete(pull);};
      }
    };
  }
  const dbNs=Object.freeze({doc,collection});

  /* ---------- user: the first account (or IKIGAI_SUPER_ADMIN_UID) is the super admin ---------- */
  const userNs=Object.freeze({
    id:async()=>uid,
    name:async()=>'', // the page asks for a display name itself
    canEdit:async()=>{try{const r=await api('GET','/api/user/role?id='+encodeURIComponent(uid));return r.superAdmin===true;}catch(e){return false;}},
    isOwner:async()=>{try{const r=await api('GET','/api/user/role?id='+encodeURIComponent(uid));return r.superAdmin===true;}catch(e){return false;}},
    profiles:async ids=>{try{return await api('GET','/api/users?ids='+encodeURIComponent(ids.join(',')));}catch(e){return {};}}
  });

  const authNs=Object.freeze({
    current:async()=>{try{const d=await api('GET','/api/auth/me',undefined,authHeaders());if(d&&d.uid){setAuth(d);if(!es)connect();}return d;}catch(e){clearAuth();return null;}},
    register:async data=>{const d=await api('POST','/api/auth/register',data);setAuth(d);return d;},
    login:async data=>{const d=await api('POST','/api/auth/login',data);setAuth(d);return d;},
    logout:async()=>{try{await api('POST','/api/auth/logout',{},authHeaders());}finally{if(es)es.close();clearAuth();}}
  });

  /* ---------- room ---------- */
  const roomNs=Object.freeze({
    presence:async p=>{myPresence=p;if(p&&typeof p.nick==='string')api('POST','/api/user',{id:uid,name:p.nick}).catch(()=>{});return api('POST','/api/room/presence',{peer:peerId,by:uid,presence:p});},
    onPeers:fn=>{peerSubs.add(fn);if(lastPeers.length)setTimeout(()=>fn({peers:lastPeers,joined:lastPeers,left:[]}),0);return ()=>peerSubs.delete(fn);},
    on:(topic,fn)=>{if(!topicSubs.has(topic))topicSubs.set(topic,new Set());topicSubs.get(topic).add(fn);return ()=>topicSubs.get(topic).delete(fn);},
    emit:async(topic,data)=>{await api('POST','/api/room/emit',{peer:peerId,by:uid,topic,data});}
  });
  addEventListener('pagehide',()=>{
    try{es&&es.close();}catch(e){}
    // Leave right away (frees the seat) instead of waiting for the server to notice.
    try{if(uid)navigator.sendBeacon('/api/room/leave',new Blob([JSON.stringify({peer:peerId})],{type:'text/plain'}));}catch(e){}
  });

  /* ---------- assets: uploads are stored by the server and served at /_blob/<id> ---------- */
  const assetsNs=Object.freeze({
    upload:async(file,opts)=>api('POST','/api/blob',file,{'X-Content-Type':(opts&&opts.type)||file.type||'application/octet-stream'})
  });
  const adminNs=Object.freeze({
    clearTv:async()=>api('POST','/api/tv/clear',{},authHeaders()),
    liveAction:async(feature,action,data)=>api('POST','/api/live/action',{feature,action,...(data||{})},authHeaders())
  });

  const table={auth:authNs,db:dbNs,user:userNs,room:roomNs,assets:assetsNs,admin:adminNs};
  window.claude=Object.freeze({use:async name=>table[name]||null});
})();
