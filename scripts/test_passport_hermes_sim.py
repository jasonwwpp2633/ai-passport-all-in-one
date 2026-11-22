#!/usr/bin/env python3
"""Opt-in real Hermes round trip through the ESP32-C3 interactive firmware (no tools/tasks executed)."""
import argparse,importlib.util,json,sys,time,threading,secrets
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'scripts'))
spec=importlib.util.spec_from_file_location('passport_simulator',root/'scripts/passport_simulator.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--remote',action='store_true',help='Use the authenticated HTTP device gateway with the real local Hermes CLI')
parser.add_argument('--remote-url',help='Use an already running device gateway')
parser.add_argument('--token-file',type=Path,help='Device credential for --remote-url')
parser.add_argument('--public-dns',action='store_true',help='Resolve a Funnel hostname with public DNS for this test process only')
args=parser.parse_args()
if bool(args.remote_url)!=bool(args.token_file):parser.error('--remote-url and --token-file are required together')
if args.public_dns and not args.remote_url:parser.error('--public-dns requires --remote-url')
if args.public_dns:
 import socket,ipaddress
 from urllib.parse import urlsplit,quote
 from urllib.request import build_opener,ProxyHandler
 host=urlsplit(args.remote_url).hostname
 if not host or not host.endswith('.ts.net'):parser.error('--public-dns only supports Tailscale Funnel hostnames')
 with build_opener(ProxyHandler({})).open('https://dns.google/resolve?name='+quote(host)+'&type=A',timeout=15) as r:
  dns=json.load(r)
 ips=[x['data'] for x in dns.get('Answer',[]) if x['type']==1 and ipaddress.ip_address(x['data']).is_global]
 if not ips:raise RuntimeError('No public Funnel address found')
 original_getaddrinfo=socket.getaddrinfo
 def public_getaddrinfo(name,port,*a,**kw):
  return original_getaddrinfo(ips[0] if name==host else name,port,*a,**kw)
 socket.getaddrinfo=public_getaddrinfo
 print('PUBLIC_FUNNEL_ROUTE',host,ips[0],flush=True)
server=store=None
if args.remote_url:
 args.remote=True
 m.OUT=root/'releases/hermes-public-test/simulator'
 sim=m.Simulator(args.remote_url,args.token_file)
elif args.remote:
 from passport_hermes_remote import TaskStore,DeviceServer,CliBackend,private_write
 directory=root/'releases/hermes-remote-test/runs'/secrets.token_hex(8)
 store=TaskStore(directory,CliBackend(directory/'sessions'))
 token=secrets.token_urlsafe(32)
 token_file=directory/'device-token';private_write(token_file,token)
 server=DeviceServer(('127.0.0.1',0),store,token)
 threading.Thread(target=server.serve_forever,daemon=True).start()
 m.OUT=directory/'simulator'
 sim=m.Simulator('http://127.0.0.1:'+str(server.server_port),token_file)
else:
 m.OUT=root/'releases/hermes-assistant-test/simulator'
 sim=m.Simulator()
sim.hermes.new_chat()
def wait_state(app,timeout=45):
 deadline=time.monotonic()+timeout
 while time.monotonic()<deadline:
  line=sim.states.get(timeout=max(.1,deadline-time.monotonic()))
  if 'app='+app+' ' in line:print(line,flush=True);return
 raise AssertionError(app)
def reply(marker,turn,timeout=150):
 deadline=time.monotonic()+timeout
 while time.monotonic()<deadline:
  snap=sim.hermes.snapshot()
  if snap['hermes']['turn']==turn and not snap['hermes']['busy'] and marker in snap['hermes']['text']:
   # Let the firmware consume the reply packet and emit a fresh complete frame.
   until=time.monotonic()+45
   while time.monotonic()<until:
    applied=sim.hermes_applied.get(timeout=max(.1,until-time.monotonic()))
    if f'turn={turn} busy=0' in applied:
     print(applied,flush=True);break
   else:raise AssertionError('Reply not applied by firmware')
   sim.frames.get(timeout=30);frame=sim.frames.get(timeout=30)
   return snap,frame
  if snap['hermes']['turn']==turn and not snap['hermes']['busy'] and snap['state']!='Hermes 已回复':raise RuntimeError(snap['state'])
  if sim.error:raise RuntimeError(sim.error)
  time.sleep(.2)
 raise TimeoutError(str(sim.hermes.snapshot()))
try:
 wait_state('home');sim.send('cuuc');wait_state('hermes')
 sim.send_text('这是胸牌会话测试。请记住本次对话的测试代号是海风73。不要调用工具，只回复：已记住海风73。')
 first,png=reply('海风73',1);(m.OUT/'hermes-first.png').write_bytes(png)
 print('FIRST_REPLY',first['hermes']['text'],flush=True)
 session=sim.hermes.session
 sim.send_text('刚才让你记住的测试代号是什么？不要调用工具，只回答代号。')
 second,png=reply('海风73',2);(m.OUT/'hermes-reply.png').write_bytes(png)
 assert session==sim.hermes.session
 print('CONTINUITY_REPLY',second['hermes']['text'],flush=True)
 sim.send_text('请帮我规划一项任务：检查胸牌的新功能。只给三步检查计划，必须包含确认菜单、发送消息、检查回复；不要实际执行、创建提醒或调用工具。')
 third,png=reply('菜单',3);(m.OUT/'hermes-task.png').write_bytes(png)
 print('TASK_REPLY',third['hermes']['text'],flush=True)
 sim.send('d');wait_state('hermes');sim.send('b');wait_state('menu')
 print(f'HERMES_E2E_PASS real_cli=1 remote_http={int(args.remote)} continuity=1 task_plan=1 firmware_pixels=1 return_menu=1',flush=True)
 (m.OUT/'verification.json').write_text(json.dumps({'status':'PASS','real_hermes':True,'session_continuity':True,'task_plan':True,'physical_device_tested':False,'remote_http':args.remote,'public_network_tested':args.public_dns},ensure_ascii=False,indent=2))
finally:
 sim.close()
 if server:
  server.shutdown();server.server_close();store.close()
