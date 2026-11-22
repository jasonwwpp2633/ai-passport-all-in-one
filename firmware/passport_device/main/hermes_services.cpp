#include "native_services.hpp"
#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_random.h"
#include <cstring>
#include <memory>

namespace passport {
void NativeServices::hermesInput(const char* text,uint32_t turn){hermesText(hermesInput_,sizeof(hermesInput_),text);hermesTurn_=turn;}
void NativeServices::hermes(){
    if(!syncClock()){publish(current_,"校时失败，请联网重试","ready");return;}
    HermesHooks hooks{this,
        [](void*,const HermesTask& task){return hermesWriteTask(task);},
        [](void*,char* out){uint8_t bytes[16];esp_fill_random(bytes,sizeof(bytes));constexpr char hex[]="0123456789abcdef";for(unsigned i=0;i<16;++i){out[i*2]=hex[bytes[i]>>4];out[i*2+1]=hex[bytes[i]&15];}out[32]=0;},
        [](void* context,const char* url,const char* token,const char* body,char* out,size_t size)->int{
            auto& self=*static_cast<NativeServices*>(context);
            struct Buffer{NativeServices* self;char* out;size_t size,used;bool overflow;} buffer{&self,out,size,0,false};
            esp_http_client_config_t config{};config.url=url;config.timeout_ms=12000;config.buffer_size=1024;config.buffer_size_tx=512;
            config.addr_type=HTTP_ADDR_TYPE_INET;config.crt_bundle_attach=esp_crt_bundle_attach;
            config.disable_auto_redirect=true;config.user_data=&buffer;
            config.event_handler=[](esp_http_client_event_t* event)->esp_err_t{
                auto& b=*static_cast<Buffer*>(event->user_data);
                if(b.self->cancelled())return ESP_FAIL;
                if(event->event_id==HTTP_EVENT_ON_DATA&&event->data_len>0){
                    if(b.used+event->data_len>=b.size){b.overflow=true;return ESP_FAIL;}
                    memcpy(b.out+b.used,event->data,event->data_len);b.used+=event->data_len;b.out[b.used]=0;
                }return ESP_OK;
            };
            if(body)config.method=HTTP_METHOD_POST;
            auto client=esp_http_client_init(&config);if(!client)return -1;
            char authorization[105];snprintf(authorization,sizeof(authorization),"Bearer %s",token);
            esp_http_client_set_header(client,"Authorization",authorization);
            if(body){esp_http_client_set_header(client,"Content-Type","application/json");esp_http_client_set_post_field(client,body,strlen(body));}
            const auto result=esp_http_client_perform(client);const int status=esp_http_client_get_status_code(client);
            esp_http_client_cleanup(client);memset(authorization,0,sizeof(authorization));
            return result==ESP_OK&&!buffer.overflow?status:-1;  // Never retry a POST here.
        }};
    remote_.step(current_.operation,hermesInput_,hermesTurn_,hooks);
    // Keep the service envelope bounded by escaped JSON bytes, prioritising the full reply page.
    auto* root=cJSON_CreateObject();if(!root)return;
    cJSON_AddNumberToObject(root,"app",static_cast<unsigned>(App::Hermes));cJSON_AddNumberToObject(root,"generation",current_.generation);cJSON_AddNumberToObject(root,"request",current_.request);
    cJSON_AddStringToObject(root,"phase","ready");cJSON_AddStringToObject(root,"state",remote_.state());
    auto* object=cJSON_AddObjectToObject(root,"hermes");const auto& view=remote_.view();
    char heard[384];hermesText(heard,sizeof(heard),view.heard);
    cJSON_AddStringToObject(object,"heard",heard);cJSON_AddStringToObject(object,"text",view.answer);
    cJSON_AddBoolToObject(object,"busy",view.busy);cJSON_AddBoolToObject(object,"truncated",view.truncated);
    cJSON_AddBoolToObject(object,"remote",true);cJSON_AddBoolToObject(object,"locked",remote_.pending()&&!view.busy);cJSON_AddNumberToObject(object,"turn",view.turn);
    for(;;){
        char* raw=cJSON_PrintUnformatted(root);if(!raw)break;
        if(strlen(raw)<2000){if(!cancelled())reply_(context_,raw);cJSON_free(raw);break;}
        cJSON_free(raw);const size_t n=strlen(heard);if(!n)break;hermesText(heard,unsigned(n>16?n-15:1),view.heard);
        cJSON_ReplaceItemInObjectCaseSensitive(object,"heard",cJSON_CreateString(heard));
    }
    cJSON_Delete(root);
}
}
