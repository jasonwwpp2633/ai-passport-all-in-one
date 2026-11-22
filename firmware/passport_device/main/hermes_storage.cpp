#include "hermes_remote.hpp"
#include "nvs.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

namespace passport {
namespace {
SemaphoreHandle_t mutex(){static StaticSemaphore_t storage;static auto value=xSemaphoreCreateMutexStatic(&storage);return value;}
struct Lock{Lock(){xSemaphoreTake(mutex(),portMAX_DELAY);}~Lock(){xSemaphoreGive(mutex());}};
template<class T>bool read(const char* key,T& value){
    value={};nvs_handle_t handle{};const auto opened=nvs_open("hermes",NVS_READONLY,&handle);
    if(opened==ESP_ERR_NVS_NOT_FOUND)return true;
    if(opened!=ESP_OK)return false;
    size_t size=sizeof(T);const auto result=nvs_get_blob(handle,key,&value,&size);nvs_close(handle);
    if(result==ESP_ERR_NVS_NOT_FOUND){value={};return true;}
    return result==ESP_OK&&size==sizeof(T);
}
}
bool hermesReadConfig(HermesConfig& out){Lock lock;return read("config_v1",out)&&hermesConfigValid(out);}
bool hermesReadTask(HermesTask& out){Lock lock;return read("task_v1",out)&&hermesTaskValid(out);}
bool hermesWriteTask(const HermesTask& task){
    Lock lock;HermesConfig config{};if(!read("config_v1",config)||config.revision!=task.revision||!hermesTaskValid(task))return false;
    nvs_handle_t handle{};if(nvs_open("hermes",NVS_READWRITE,&handle)!=ESP_OK)return false;
    const bool ok=nvs_set_blob(handle,"task_v1",&task,sizeof(task))==ESP_OK&&nvs_commit(handle)==ESP_OK;nvs_close(handle);return ok;
}
bool hermesWriteConfig(HermesConfig& config){
    Lock lock;HermesTask old{};HermesConfig current{};
    if(!read("task_v1",old)||!hermesTaskValid(old)||old.phase==HermesTaskPhase::Pending||old.phase==HermesTaskPhase::Unknown||!read("config_v1",current))return false;
    do{config.revision=esp_random();}while(!config.revision||config.revision==current.revision);
    if(!hermesConfigValid(config))return false;
    HermesTask fresh{};fresh.revision=config.revision;
    nvs_handle_t handle{};if(nvs_open("hermes",NVS_READWRITE,&handle)!=ESP_OK)return false;
    const bool ok=nvs_set_blob(handle,"config_v1",&config,sizeof(config))==ESP_OK&&nvs_set_blob(handle,"task_v1",&fresh,sizeof(fresh))==ESP_OK&&nvs_commit(handle)==ESP_OK;nvs_close(handle);return ok;
}
bool hermesResolveTask(){
    Lock lock;HermesConfig config{};HermesTask old{};
    if(!read("config_v1",config)||!read("task_v1",old)||old.phase!=HermesTaskPhase::Unknown)return false;
    do{config.revision=esp_random();}while(!config.revision||config.revision==old.revision);
    HermesTask fresh{};fresh.revision=config.revision;
    nvs_handle_t handle{};if(nvs_open("hermes",NVS_READWRITE,&handle)!=ESP_OK)return false;
    const bool ok=nvs_set_blob(handle,"config_v1",&config,sizeof(config))==ESP_OK&&nvs_set_blob(handle,"task_v1",&fresh,sizeof(fresh))==ESP_OK&&nvs_commit(handle)==ESP_OK;nvs_close(handle);return ok;
}
}
