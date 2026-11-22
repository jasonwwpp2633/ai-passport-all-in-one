#include "hermes_remote.hpp"
#include "cJSON.h"
#include <cmath>
#include <cstdio>
#include <cstring>

namespace passport {
namespace {
bool terminated(const char* p,size_t n){return memchr(p,0,n)!=nullptr;}
bool id(const char* p){if(strlen(p)!=32)return false;for(unsigned i=0;i<32;++i)if(!((p[i]>='0'&&p[i]<='9')||(p[i]>='a'&&p[i]<='f')))return false;return true;}
const cJSON* get(const cJSON* o,const char* k){return cJSON_GetObjectItemCaseSensitive(o,k);}
const char* str(const cJSON* o,const char* k){auto* p=get(o,k);return cJSON_IsString(p)?p->valuestring:"";}
bool integer(const cJSON* p,uint32_t& v){if(!cJSON_IsNumber(p)||!std::isfinite(p->valuedouble)||p->valuedouble<0||p->valuedouble>262144||std::trunc(p->valuedouble)!=p->valuedouble)return false;v=uint32_t(p->valuedouble);return true;}
}
bool hermesOrigin(const char* s){
    // HTTPS origin only. No userinfo, query, fragment, path, escapes, IP literals or control characters.
    if(!s||strncmp(s,"https://",8)||strlen(s)>=192)return false;
    s+=8;const char* host=s;unsigned labels=0;bool alpha=false;
    while(*s&&*s!=':'){
        const char* label=s;if(*s=='-')return false;
        while((*s>='a'&&*s<='z')||(*s>='A'&&*s<='Z')||(*s>='0'&&*s<='9')||*s=='-'){if((*s>='a'&&*s<='z')||(*s>='A'&&*s<='Z'))alpha=true;++s;}
        if(s==label||s-label>63||s[-1]=='-')return false;
        ++labels;if(*s!='.')break;++s;if(!*s)return false;
    }
    if(s==host||labels<2||!alpha)return false;
    if(*s==':'){++s;unsigned port=0,n=0;while(*s>='0'&&*s<='9'){if(++n>5)return false;port=port*10+unsigned(*s++-'0');}if(!n||!port||port>65535)return false;}
    return !*s;
}
bool hermesToken(const char* p){if(!p||strlen(p)<32||strlen(p)>96)return false;for(;*p;++p)if(!((*p>='a'&&*p<='z')||(*p>='A'&&*p<='Z')||(*p>='0'&&*p<='9')||*p=='_'||*p=='-'))return false;return true;}
bool hermesConfigValid(const HermesConfig& c){return c.version==1&&c.enabled<=1&&terminated(c.origin,sizeof(c.origin))&&terminated(c.token,sizeof(c.token))&&(!c.enabled||(c.revision&&hermesOrigin(c.origin)&&hermesToken(c.token)));}
bool hermesTaskValid(const HermesTask& t){return t.version==1&&terminated(t.session,sizeof(t.session))&&terminated(t.request,sizeof(t.request))&&terminated(t.heard,sizeof(t.heard))&&t.cursor<=262144&&t.phase<=HermesTaskPhase::Failed&&(!*t.session||id(t.session))&&(!*t.request||id(t.request))&&(t.phase==HermesTaskPhase::Idle||(id(t.session)&&id(t.request)&&*t.heard));}
void HermesRemote::state(const char* s){hermesText(state_,sizeof(state_),s);}
void HermesRemote::restore(const HermesConfig& c,const HermesTask& t,bool valid){
    config_=c;task_=t;broken_=!valid||!hermesTaskValid(t)||(t.revision&&t.revision!=c.revision);
    if(broken_)task_={};
    view_={};hermesText(view_.heard,sizeof(view_.heard),task_.heard);view_.turn=task_.turn;
    view_.busy=!broken_&&t.phase==HermesTaskPhase::Pending;
    state(broken_?"任务记录异常，请在电脑核查":pending()?"正在查询原任务":"远程模式 · 按确认说话");
}
bool HermesRemote::commit(const HermesTask& next,const HermesHooks& h){
    if(!h.save(h.context,next)){state("保存失败，请重启检查配置");return false;}
    task_=next;return true;
}
void HermesRemote::accept(const char* raw,const HermesHooks& h){
    auto* root=cJSON_Parse(raw);const auto* next=get(root,"next_cursor");uint32_t cursor=0,page=0;
    const bool hasNext=!cJSON_IsNull(next);const char* status=str(root,"status");const char* text=str(root,"text");
    const bool running=!strcmp(status,"queued")||!strcmp(status,"running")||!strcmp(status,"waiting_for_approval");
    const bool done=!strcmp(status,"completed"),failed=!strcmp(status,"failed")||!strcmp(status,"cancelled");
    const bool unknown=!strcmp(status,"unknown")||!strcmp(status,"interrupted");
    const bool valid=cJSON_IsObject(root)&&!strcmp(str(root,"task_id"),task_.request)&&strlen(text)<=1152&&integer(get(root,"cursor"),page)&&page==task_.cursor
        &&(!hasNext||(integer(next,cursor)&&cursor>page))&&(running||done||failed||unknown);
    if(!valid){cJSON_Delete(root);state("回复异常，继续查询原任务");return;}
    auto t=task_;t.phase=running?HermesTaskPhase::Pending:done?HermesTaskPhase::Done:failed?HermesTaskPhase::Failed:HermesTaskPhase::Unknown;
    if(t.phase!=task_.phase&&!commit(t,h)){cJSON_Delete(root);view_.busy=true;return;}
    view_.busy=running;view_.turn=task_.turn;hermesText(view_.heard,sizeof(view_.heard),task_.heard);
    if(!running){hermesText(view_.answer,sizeof(view_.answer),*text?text:unknown?"请在电脑确认原任务，避免重复安排。":done?"Hermes 未返回文字":"任务未完成，请在电脑查看。" );view_.scroll=0;next_=hasNext?cursor:0;view_.truncated=hasNext||cJSON_IsTrue(get(root,"clipped"));}
    state(!strcmp(status,"waiting_for_approval")?"等待电脑端批准":running?"Hermes 正在处理":done?"Hermes 已回复":unknown?"任务待确认，请在电脑核查":"任务未完成，请在电脑查看");
    cJSON_Delete(root);
}
void HermesRemote::step(const char* op,const char* input,uint32_t turn,const HermesHooks& h){
    if(!config_.enabled||broken_){state(broken_?"任务记录异常，请在电脑核查":"请先配置远程 Hermes");return;}
    if(!h.save||!h.uuid||!h.http)return;
    if(!strcmp(op,"new")){
        if(pending()){state("原任务待确认，不能新建对话");return;}
        HermesTask t{};t.revision=config_.revision;h.uuid(h.context,t.session);
        if(commit(t,h)){view_={};next_=0;state("远程模式 · 已开始新对话");}return;
    }
    const bool send=!strcmp(op,"send");char* body=nullptr;
    if(send){
        if(pending()){state("正在查询原任务");}
        else if(turn==task_.turn&&*task_.request){state("该消息已提交");return;}
        else{
            if(!input||!*input||strlen(input)>383){state("消息过长，请缩短后重试");view_.busy=false;return;}
            HermesTask t=task_;t.revision=config_.revision;t.turn=turn;t.cursor=0;t.phase=HermesTaskPhase::Pending;
            if(!*t.session)h.uuid(h.context,t.session);
            h.uuid(h.context,t.request);hermesText(t.heard,sizeof(t.heard),input);
            auto* json=cJSON_CreateObject();cJSON_AddStringToObject(json,"request_id",t.request);cJSON_AddStringToObject(json,"session_id",t.session);cJSON_AddStringToObject(json,"text",t.heard);
            body=cJSON_PrintUnformatted(json);cJSON_Delete(json);
            if(!body){state("内存不足，请稍后重试");view_.busy=false;return;}
            if(!commit(t,h)){cJSON_free(body);view_.busy=false;return;}
            view_={};view_.turn=turn;view_.busy=true;hermesText(view_.heard,sizeof(view_.heard),input);next_=0;
        }
    }
    if(!strcmp(op,"next")){
        if(pending()||!next_)return;
        auto t=task_;t.cursor=next_;if(!commit(t,h))return;
    }
    if(!*task_.request){state("远程模式 · 按确认说话");return;}
    // Once pending is persisted, a reboot, lost acceptance, refresh or retry only GETs the same ID.
    char url[280];if(body)snprintf(url,sizeof(url),"%s/device/tasks",config_.origin);
    else snprintf(url,sizeof(url),"%s/device/tasks/%s?cursor=%lu",config_.origin,task_.request,static_cast<unsigned long>(task_.cursor));
    view_.busy=true;
    char response[1536]{};const int code=h.http(h.context,url,config_.token,body,response,sizeof(response));cJSON_free(body);
    if(code==200||code==202){accept(response,h);return;}
    if(code==401||code==403||code==404||code==409){
        auto t=task_;t.phase=HermesTaskPhase::Unknown;
        if(commit(t,h)){view_.busy=false;state(code==401||code==403?"设备认证失败，请检查令牌":code==404?"原任务未找到，请在电脑核查":"任务待确认，请在电脑核查");}
    }else state("连接中断，继续查询原任务");
}
}
