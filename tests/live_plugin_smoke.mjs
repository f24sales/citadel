// Explicit, opt-in integration test inside the running Fedora/OpenClaw image.
// Adds one temporary HTTP service, tests the real plugin + Telegram renderer,
// deletes only its Serve route, resets Serve (not SSH), then restores by scan.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

assert(process.argv.includes('--execute'), 'Explicit --execute is required');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require=createRequire('/usr/local/lib/node_modules/openclaw/package.json');
const {createJiti}=require('jiti');
const jiti=createJiti('/usr/local/lib/node_modules/openclaw/package.json', {
  alias:{'openclaw/plugin-sdk/plugin-entry':require.resolve('openclaw/plugin-sdk/plugin-entry')},
});
const mod=await jiti.import(path.join(root, 'index.js'));
const {telegramPlugin}=await import('/usr/local/lib/node_modules/openclaw/dist/extensions/telegram/channel-plugin-api.js');
let command;
(mod.default ?? mod).register({registerCommand:value=>command=value});
assert(command.requireAuth);
const call=args=>command.handler({args,channel:'telegram'});
const read=name=>JSON.parse(fs.readFileSync(path.join(root,'CITADEL_DATA',name)));
const serve=()=>JSON.parse(execFileSync('tailscale',['serve','status','--json'],{encoding:'utf8'}));
const ssh=()=>JSON.parse(execFileSync('tailscale',['debug','prefs'],{encoding:'utf8'})).RunSSH;
const before=read('services.json');
const oldServe=serve();
const oldSSH=ssh();
const server=http.createServer((req,res)=>{
  res.writeHead(200,{'Content-Type':'text/html'});
  res.end('<html><title>Citadel acceptance probe</title></html>');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const port=server.address().port;
try {
  const result=await call('add tailscale');
  const after=read('services.json');
  assert(after.http_services.some(row=>row.port===port), result.text);
  for(const old of before.http_services)
    assert.deepEqual(after.http_services.find(row=>row.port===old.port),old);
  const routes=serve();
  assert(routes.TCP?.[port]?.HTTPS);
  for(const key of ['TCP','Web']) for(const [id,value] of Object.entries(oldServe[key] ?? {}))
    assert.deepEqual(routes[key][id],value);
  const caddy=fs.readFileSync(path.join(root,'CITADEL_DATA/CADDY/Caddyfile'),'utf8');
  assert(caddy.includes(`# backend ${port} -> HTTPS `));
  const cf=JSON.parse(fs.readFileSync(path.join(root,'cache/cloudflare-routes.json')));
  assert(cf.services[String(port)]);
  await call('add tailscale');
  assert.equal(fs.readFileSync(path.join(root,'CITADEL_DATA/CADDY/Caddyfile'),'utf8'),caddy);
  const payload=await call('tailscale edit');
  const rendered=await telegramPlugin.outbound.renderPresentation({payload,presentation:payload.presentation,ctx:{cfg:{},to:'123456789'}});
  assert(rendered.channelData.telegram.buttons.flat().some(b=>b.callback_data==='tgcmd:/citadel tailscale reset'));
  assert(rendered.channelData.telegram.buttons.flat().some(b=>b.callback_data===`tgcmd:/citadel tailscale del ${port}`));
  const snapshot=()=>{
    const files={};
    for(const dir of ['CITADEL_DATA','cache']) {
      const walk=relative=>{
        for(const entry of fs.readdirSync(path.join(root,relative),{withFileTypes:true})) {
          const name=path.join(relative,entry.name);
          if(entry.isDirectory()) walk(name);
          else if(entry.isFile()) files[name]=fs.readFileSync(path.join(root,name)).toString('base64');
        }
      };
      walk(dir);
    }
    return files;
  };
  const metadata=snapshot();
  const reply=await call(`tailscale del ${port}`);
  const deleted=serve();
  assert(!deleted.TCP?.[port],reply.text);
  for(const key of ['TCP','Web']) for(const [id,value] of Object.entries(oldServe[key] ?? {}))
    assert.deepEqual(deleted[key][id],value);
  assert.deepEqual(snapshot(),metadata);
  assert.equal(ssh(),oldSSH);
  // CLI shares the same operation; repeated deletion is a no-op.
  const cli=JSON.parse(execFileSync('bash',[path.join(root,'scan.sh'),'--del',String(port)],{encoding:'utf8'}));
  assert.equal(cli.port,port);
  assert.deepEqual(serve(),deleted);
  assert.deepEqual(snapshot(),metadata);
  const refreshed=await telegramPlugin.outbound.renderPresentation({payload:reply,presentation:reply.presentation,ctx:{cfg:{},to:'123456789'}});
  assert(!refreshed.channelData.telegram.buttons.flat().some(b=>b.callback_data===`tgcmd:/citadel tailscale del ${port}`));
  await call('tailscale reset');
  assert.equal(Object.keys(serve().TCP ?? {}).length,0);
  assert.equal(ssh(),oldSSH);
  console.log(JSON.stringify({add:'passed',delete:'passed',delete_cli:'passed',metadata:'unchanged',old_routes:'preserved',telegram_renderer:'passed',reset:'passed',ssh:'unchanged'}));
} finally {
  await new Promise(resolve=>server.close(resolve));
  // Remove only this test's allocation; leave all operator/other assignments.
  const ledger=path.join(root,'CITADEL_DATA/CADDY/ports.json');
  const value=JSON.parse(fs.readFileSync(ledger));
  delete value.ports[String(port)];
  fs.writeFileSync(ledger+'.test-cleanup',JSON.stringify(value,null,2)+'\n');
  fs.renameSync(ledger+'.test-cleanup',ledger);
  const result=await call('scan tailscale');
  assert(!read('services.json').http_services.some(row=>row.port===port),result.text);
  assert(!serve().TCP?.[port]);
  console.log(JSON.stringify({cleanup:'verified',routes:Object.keys(serve().TCP ?? {}).length}));
}
