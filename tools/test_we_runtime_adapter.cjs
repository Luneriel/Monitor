/* Native-free tests of the pinned bundle's real source contract and adapter.
 * No DOM renderer, browser, GPU, user wallpaper, network or performance sample.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const runtime = path.resolve(__dirname, '../monitor/assets/we-runtime');
global.window={addEventListener(){}};
global.document={addEventListener(){}};
let bundle;
try { bundle=require(path.join(runtime, 'webwallgl.global.min.js')); }
catch (error) { console.error('Bundle initialization:',error.message); process.exit(1); }
delete global.window; delete global.document;

async function testPinnedSources() {
  const original = global.fetch;
  const requests = [];
  let project = {type:'scene', file:'scene.pkg'};
  global.fetch = async url => {
    requests.push(String(url));
    if (String(url).endsWith('/project.json')) return {ok:true, json:async()=>project};
    if (String(url).endsWith('/scene.pkg')) return {ok:true, arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer};
    if (String(url).endsWith('/scenes/main.json')) return {ok:true, arrayBuffer:async()=>new TextEncoder().encode('{"objects":[]}').buffer};
    return {ok:false, status:404};
  };
  try {
    const base='http://127.0.0.1:12345/token/asset';
    const source=bundle.httpSource(base);
    assert.equal((await source.project()).type,'scene');
    assert.deepEqual([...new Uint8Array(await source.scenePkg())], [1,2,3]);
    assert.deepEqual(requests, [base+'/project.json',base+'/scene.pkg']);
    requests.length=0;
    project={type:'scene', file:'scenes/main.json'};
    const loose=bundle.httpSource(base);
    const directory=await loose.sceneDir();
    assert.equal(directory.entry,'scenes/main.json');
    assert.deepEqual(requests, [base+'/project.json',base+'/scenes/main.json']);
    console.log('PASS pinned httpSource project/pkg/loose URL contract');
  } finally { global.fetch=original; }
}

async function testAdapter() {
  const events = {}, frames = [], captures = [];
  const host={style:{}, appendChild(frame){frames.push(frame)}};
  const scene={on(){}, async load(source){captures.push(source)}, setVolume(){},
    pause(){captures.push('pause')}, resume(){captures.push('resume')}, destroy(){}, setFit(){}};
  const location=new URL('http://127.0.0.1:12345/token/runtime/index.html?kind=live_scene&entry=scene.pkg');
  const context={location, URL, URLSearchParams, console,
    document:{getElementById:()=>host, addEventListener(){}},
    setTimeout:()=>1, clearTimeout(){}, addEventListener(name,fn){events[name]=fn},
    WebWallGL:{createScene:()=>scene, httpSource:url=>({directoryURL:url})}};
  context.window=context; context.globalThis=context;
  vm.runInNewContext(fs.readFileSync(path.join(runtime,'renderer.js'),'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(captures[0].directoryURL,'http://127.0.0.1:12345/token/asset');
  assert.equal(context.__monitorState.status,'ready');
  context.__monitor.pause(true); context.__monitor.pause(false);
  assert.ok(captures.includes('pause') && captures.includes('resume'));
  context.__monitor.style({fit:'contain',brightness:1.2,contrast:.8,saturation:1.5,blur:3,x:20,y:80,scale:110});
  assert.match(host.style.filter,/contrast\(0.8\)/);
  assert.match(host.style.transform,/scale\(1.1\)/);
  console.log('PASS adapter uses real source directory and engine pause/resume/style');
}

async function testWebErrorsAndProperties() {
  const events={}, frameEvents={}, messages=[];
  const frame={style:{}, contentWindow:{postMessage:value=>messages.push(value)},
    setAttribute(key,value){this[key]=value}, addEventListener(name,callback){frameEvents[name]=callback}, remove(){this.removed=true}};
  const context={location:new URL('http://127.0.0.1:12345/token/runtime/index.html?kind=live_web&entry=web/index.html'),
    URL,URLSearchParams,console,setTimeout:()=>1,clearTimeout(){},addEventListener(name,fn){events[name]=fn},
    fetch:async()=>({json:async()=>({general:{properties:{color:{value:'1 0 0'}}}})}),
    document:{getElementById:()=>({appendChild(){},style:{}}),createElement:()=>frame,addEventListener(){}}};
  context.window=context; context.globalThis=context;
  vm.runInNewContext(fs.readFileSync(path.join(runtime,'renderer.js'),'utf8'),context);
  assert.equal(frame.sandbox,'allow-scripts');
  assert.equal(frame.src,'http://127.0.0.1:12345/token/asset/web/index.html');
  await frameEvents.load();
  assert.equal(context.__monitorState.status,'ready');
  assert.ok(messages.some(message=>message.op==='properties' && message.properties.color.value==='1 0 0'));
  events.message({source:{},data:{__monitorWallpaperError:true,error:'forged'}});
  assert.equal(context.__monitorState.status,'ready');
  events.message({source:frame.contentWindow,data:{__monitorWallpaperError:true,error:'fixture script failure'}});
  assert.equal(context.__monitorState.status,'failed');
  assert.ok(frame.removed);
  console.log('PASS opaque Web iframe, default properties, authenticated script errors');
}

async function testDocumentBridge() {
  const source=fs.readFileSync(path.resolve(__dirname,'../monitor/ui/live_wallpaper.py'),'utf8');
  const script=/lockdown\.setSourceCode\("""([\s\S]*?)"""\)/.exec(source)[1];
  const events={}, parent={}, sent=[], pauses=[];
  parent.postMessage=message=>sent.push(message);
  const context={navigator:{},queueMicrotask,window:{parent, addEventListener(name,fn){events[name]=fn},
    wallpaperPropertyListener:{setPaused:value=>pauses.push(value)}}};
  vm.runInNewContext(script,context);
  let silent;
  context.window.wallpaperRegisterAudioListener(value=>silent=value);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(silent.length,128); assert.ok(silent.every(value=>value===0));
  events.message({source:parent,data:{__monitorWallpaper:true,op:'pause',paused:true}});
  assert.deepEqual(pauses,[true]);
  events.error({message:'fixture'});
  assert.equal(sent[0].__monitorWallpaperError,true);
  assert.equal(context.window.RTCPeerConnection,undefined);
  console.log('PASS document bridge setPaused, silent audio API and script error reporting');
}
async function testLazySceneEngine() {
  for (const failed of [false, true]) {
    const scripts = [];
    const scene = {on(){}, async load(){}, setVolume(){}, destroy(){}};
    const context = {location:new URL('http://127.0.0.1:12345/token/runtime/index.html?kind=live_scene'),
      URL, URLSearchParams, console, addEventListener(){},
      document:{getElementById:()=>({style:{}}), addEventListener(){},
        createElement:tag=>{assert.equal(tag,'script');return {}},
        head:{appendChild(script){
          scripts.push(script.src);
          if (failed) script.onerror();
          else {
            context.WebWallGL = {createScene:()=>scene, httpSource:()=>({})};
            script.onload();
          }
        }}}};
    context.window = context; context.globalThis = context;
    vm.runInNewContext(fs.readFileSync(path.join(runtime,'renderer.js'),'utf8'),context);
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(scripts,['http://127.0.0.1:12345/token/runtime/webwallgl.global.min.js']);
    assert.equal(context.__monitorState.status,failed ? 'failed' : 'ready');
  }
  const html = fs.readFileSync(path.join(runtime,'index.html'),'utf8');
  assert.ok(!html.includes('webwallgl.global.min.js'));
  console.log('PASS Scene-only engine loading and load failure handling');
}
(async()=>{await testPinnedSources(); await testAdapter(); await testWebErrorsAndProperties(); await testDocumentBridge(); await testLazySceneEngine()})().catch(error=>{console.error(error);process.exitCode=1});
