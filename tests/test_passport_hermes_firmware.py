"""Exercise the production C++ Hermes state machine with lost acceptance, restarts and bounded replies."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

class FirmwareHermesTests(unittest.TestCase):
 def test_native_protocol_recovery_and_pages(self):
  source=r'''
#include "hermes_remote.hpp"
#include "cJSON.h"
#include <cassert>
#include <cstring>
#include <string>
using namespace passport;
struct Fake {
 HermesTask disk{};bool failSave{},lost{},notFound{},auth{},malformed{},unknown{},done{};unsigned sequence{},posts{},gets{};std::string body,url;
 HermesHooks hooks(){return {this,
 [](void* c,const HermesTask& task){auto& f=*static_cast<Fake*>(c);assert(hermesTaskValid(task));if(f.failSave)return false;f.disk=task;return true;},
 [](void* c,char* out){auto& f=*static_cast<Fake*>(c);snprintf(out,33,"%032x",++f.sequence);},
 [](void* c,const char* url,const char* token,const char* body,char* out,size_t size){
  auto& f=*static_cast<Fake*>(c);assert(!strcmp(token,"abcdefghijklmnopqrstuvwxyzABCDEF"));f.url=url;
  if(body){++f.posts;f.body=body;assert(f.disk.phase==HermesTaskPhase::Pending);if(f.lost)return -1;}
  else ++f.gets;
  if(f.notFound)return 404;if(f.auth)return 401;
  if(f.malformed){strcpy(out,"{}");return 200;}
  auto* j=cJSON_CreateObject();cJSON_AddStringToObject(j,"task_id",f.disk.request);
  cJSON_AddStringToObject(j,"status",f.unknown?"unknown":f.done?"completed":"running");
  cJSON_AddStringToObject(j,"text",f.done?(f.disk.cursor?"第二页内容":"第一页内容"):"");
  cJSON_AddNumberToObject(j,"cursor",f.disk.cursor);
  if(f.done&&!f.disk.cursor)cJSON_AddNumberToObject(j,"next_cursor",5);else cJSON_AddNullToObject(j,"next_cursor");
  cJSON_AddBoolToObject(j,"clipped",false);auto* raw=cJSON_PrintUnformatted(j);assert(strlen(raw)<size);strcpy(out,raw);cJSON_free(raw);cJSON_Delete(j);return body?202:200;
 }};}
};
int main(){
 assert(hermesOrigin("https://badge.tail123.ts.net:8443"));
 for(auto* bad:{"http://badge.ts.net","https://bad.ts.net/","https://a.ts.net?key=x","https://a@badge.ts.net","https://a.ts.net:0","https://a.ts.net:65536","https://a..ts.net","https://a.ts.net.","https://127.0.0.1","https://a.ts.net\r\nX:evil"})assert(!hermesOrigin(bad));
 assert(!hermesToken("short"));assert(!hermesToken("abcdefghijklmnopqrstuvwxyzABCDEF\n"));
 HermesConfig config{};config.enabled=1;config.revision=12;strcpy(config.origin,"https://badge.ts.net:8443");strcpy(config.token,"abcdefghijklmnopqrstuvwxyzABCDEF");assert(hermesConfigValid(config));
 Fake f;HermesRemote remote;remote.restore(config,f.disk,true);
 remote.step("enter","",0,f.hooks());assert(!f.posts&&!f.gets);
 f.failSave=true;remote.step("send","不要丢失",1,f.hooks());assert(!f.posts);
 f.failSave=false;f.lost=true;remote.step("send","literal: $(touch x) `x`\n你好",1,f.hooks());assert(f.posts==1&&remote.pending());
 auto request=std::string(f.disk.request),session=std::string(f.disk.session);
 auto* input=cJSON_Parse(f.body.c_str());assert(!strcmp(cJSON_GetObjectItem(input,"text")->valuestring,"literal: $(touch x) `x`\n你好"));cJSON_Delete(input);
 // Power loss after a POST with a lost response: boot only GETs the original ID.
 HermesRemote reboot;reboot.restore(config,f.disk,true);f.lost=false;reboot.step("enter","",0,f.hooks());assert(f.posts==1&&f.gets==1&&f.url.find(request)!=std::string::npos);
 reboot.step("send","different input",2,f.hooks());assert(f.posts==1&&remote.pending());
 reboot.step("new","",0,f.hooks());assert(std::string(f.disk.session)==session);
 f.auth=true;reboot.step("refresh","",0,f.hooks());assert(!reboot.view().busy&&reboot.pending());f.auth=false;
 f.unknown=true;reboot.step("refresh","",0,f.hooks());assert(f.disk.phase==HermesTaskPhase::Unknown);f.unknown=false;
 f.done=true;reboot.step("refresh","",0,f.hooks());assert(!reboot.pending()&&reboot.view().truncated&&!strcmp(reboot.view().answer,"第一页内容"));
 reboot.step("next","",0,f.hooks());assert(!strcmp(reboot.view().answer,"第二页内容")&&!reboot.view().truncated&&f.posts==1);
 reboot.step("send","duplicate",1,f.hooks());assert(f.posts==1);
 reboot.step("new","",0,f.hooks());assert(std::string(f.disk.session)!=session&&!reboot.pending());
 f.notFound=true;reboot.step("send","new task",1,f.hooks());assert(f.posts==2&&f.disk.phase==HermesTaskPhase::Unknown&&!reboot.view().busy);f.notFound=false;
 HermesRemote lostRecord;auto broken=f.disk;memset(broken.heard,'X',sizeof(broken.heard));lostRecord.restore(config,broken,false);lostRecord.step("send","blocked",2,f.hooks());assert(f.posts==2);
 auto wrong=f.disk;wrong.revision=99;lostRecord.restore(config,wrong,true);lostRecord.step("send","blocked",2,f.hooks());assert(f.posts==2);
 // A malformed status must not release the pending lock or claim a completed task.
 Fake g;HermesRemote invalid;invalid.restore(config,g.disk,true);g.malformed=true;invalid.step("send","query",1,g.hooks());assert(invalid.pending()&&invalid.view().busy);g.malformed=false;g.done=true;invalid.step("refresh","",0,g.hooks());assert(!invalid.pending());
}
'''
  compiler=shutil.which('clang++') or shutil.which('g++')
  with tempfile.TemporaryDirectory() as tmp:
   tmp=Path(tmp);cpp=tmp/'test.cpp';cpp.write_text(source);binary=tmp/'test';obj=tmp/'cjson.o'
   inc=ROOT/'deps/espressif__cjson/cJSON';dev=ROOT/'firmware/passport_device/main'
   subprocess.run([shutil.which('clang') or 'cc','-fsanitize=address,undefined','-g','-Wno-deprecated-declarations','-c',str(inc/'cJSON.c'),'-o',str(obj)],check=True)
   subprocess.run([compiler,'-std=c++20','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-g','-I',str(inc),'-I',str(dev),'-I',str(ROOT/'firmware/passport_suite/main'),str(cpp),str(dev/'hermes_remote.cpp'),str(obj),'-o',str(binary)],check=True)
   subprocess.run([str(binary)],check=True)

class HermesStorageTests(unittest.TestCase):
 def test_actual_storage_revision_and_unfinished_lock(self):
  source=r'''
#include "hermes_remote.hpp"
#include "nvs.h"
#include <cassert>
#include <cstring>
#include <map>
#include <string>
#include <vector>
using namespace passport;
std::map<std::string,std::vector<unsigned char>> flash;
int nvs_open(const char* space,int mode,nvs_handle_t* h){assert(!strcmp(space,"hermes"));*h=1;return mode==0&&flash.empty()?ESP_ERR_NVS_NOT_FOUND:ESP_OK;}
int nvs_get_blob(nvs_handle_t,const char* key,void* out,size_t* size){auto it=flash.find(key);if(it==flash.end())return ESP_ERR_NVS_NOT_FOUND;if(*size<it->second.size())return -1;memcpy(out,it->second.data(),it->second.size());*size=it->second.size();return ESP_OK;}
int nvs_set_blob(nvs_handle_t,const char* key,const void* data,size_t n){auto* p=static_cast<const unsigned char*>(data);flash[key]={p,p+n};return ESP_OK;}
int nvs_commit(nvs_handle_t){return ESP_OK;}void nvs_close(nvs_handle_t){}
unsigned esp_random(){static unsigned n=10;return ++n;}
int main(){
 HermesConfig c{};HermesTask t{};assert(hermesReadConfig(c)&&!c.enabled&&hermesReadTask(t));
 c.enabled=1;strcpy(c.origin,"https://badge.ts.net:8443");strcpy(c.token,"abcdefghijklmnopqrstuvwxyzABCDEF");assert(hermesWriteConfig(c));
 assert(hermesReadTask(t)&&t.revision==c.revision);t.phase=HermesTaskPhase::Pending;strcpy(t.session,"11111111111111111111111111111111");strcpy(t.request,"22222222222222222222222222222222");strcpy(t.heard,"query");
 assert(hermesWriteTask(t));auto other=c;assert(!hermesWriteConfig(other));assert(!hermesResolveTask());
 t.phase=HermesTaskPhase::Unknown;assert(hermesWriteTask(t));assert(hermesResolveTask());
 HermesConfig fresh{};assert(hermesReadConfig(fresh)&&fresh.revision!=c.revision);assert(!hermesWriteTask(t));
 assert(hermesReadTask(t)&&t.phase==HermesTaskPhase::Idle);assert(hermesWriteConfig(fresh));
 flash["task_v1"].resize(3);assert(!hermesReadTask(t));assert(!hermesWriteConfig(fresh));
 assert(flash.size()==2); // No old SystemSettings namespace or config record was touched.
}
'''
  with tempfile.TemporaryDirectory() as tmp:
   tmp=Path(tmp);(tmp/'freertos').mkdir()
   (tmp/'nvs.h').write_text("#pragma once\n#include <cstddef>\nusing nvs_handle_t=int;\nconstexpr int ESP_OK=0,ESP_ERR_NVS_NOT_FOUND=1,NVS_READONLY=0,NVS_READWRITE=1;\nint nvs_open(const char*,int,nvs_handle_t*);int nvs_get_blob(nvs_handle_t,const char*,void*,size_t*);int nvs_set_blob(nvs_handle_t,const char*,const void*,size_t);int nvs_commit(nvs_handle_t);void nvs_close(nvs_handle_t);\n")
   (tmp/'esp_random.h').write_text('#pragma once\nunsigned esp_random();\n')
   (tmp/'freertos/FreeRTOS.h').write_text('#pragma once\nconstexpr int portMAX_DELAY=0;\n')
   (tmp/'freertos/semphr.h').write_text('#pragma once\nstruct StaticSemaphore_t{};using SemaphoreHandle_t=void*;inline void* xSemaphoreCreateMutexStatic(StaticSemaphore_t* s){return s;}inline void xSemaphoreTake(void*,int){}inline void xSemaphoreGive(void*){}\n')
   cpp=tmp/'test.cpp';cpp.write_text(source);binary=tmp/'test';obj=tmp/'cjson.o'
   inc=ROOT/'deps/espressif__cjson/cJSON';dev=ROOT/'firmware/passport_device/main'
   subprocess.run(['clang','-Wno-deprecated-declarations','-fsanitize=address,undefined','-g','-c',str(inc/'cJSON.c'),'-o',str(obj)],check=True)
   subprocess.run(['clang++','-std=c++20','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-g','-I',str(tmp),'-I',str(inc),'-I',str(dev),'-I',str(ROOT/'firmware/passport_suite/main'),str(cpp),str(dev/'hermes_remote.cpp'),str(dev/'hermes_storage.cpp'),str(obj),'-o',str(binary)],check=True)
   subprocess.run([str(binary)],check=True)

if __name__=='__main__':unittest.main()
