from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from funasr import AutoModel
import torch
import json
import numpy as np
import os
from datetime import datetime
from queue import Queue

app = FastAPI()

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 创建存储音频文件的目录
AUDIO_DIR = "recorded_audio"
if not os.path.exists(AUDIO_DIR):
    os.makedirs(AUDIO_DIR)

# 创建模型存储目录
MODEL_DIR = "model"
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)

# 全局变量
SAMPLE_RATE = 16000
WINDOW_SIZE = 512
VAD_THRESHOLD = 0.5

# VAD状态
vad_state = {
    "is_running": False,
    "active_websockets": set(),
    "model": None,
    "result_queue": Queue()
}

# 设置设备和数据类型
device = "cuda" if torch.cuda.is_available() else "cpu"
torch.set_default_dtype(torch.float32)

# 初始化模型状态
model_state = {
    "vad_model": None,
    "asr_model": None,
    "punc_model": None
}

# 使用 FastAPI 的生命周期事件装饰器
@app.on_event("startup")
async def startup_event():
    print("正在加载模型...")
    
    # 从网络加载VAD模型
    try:
        print("正在从网络加载VAD模型...")
        # 首先设置torch.hub.dir环境变量，指向我们的模型目录
        torch_hub_dir = os.path.join(MODEL_DIR, "torch_hub")
        if not os.path.exists(torch_hub_dir):
            os.makedirs(torch_hub_dir)
        
        # 保存原始的hub目录
        original_hub_dir = torch.hub.get_dir()
        # 设置新的hub目录
        torch.hub.set_dir(torch_hub_dir)
        
        model_state["vad_model"] = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            onnx=True,
            trust_repo=True
        )[0]
        
        # 恢复原始hub目录
        torch.hub.set_dir(original_hub_dir)
        print("VAD模型加载完成")
    except Exception as e:
        print(f"VAD模型加载失败: {str(e)}")
        raise e

    # 设置环境变量来指定模型下载位置
    # 尝试多个可能的环境变量名以提高兼容性
    asr_model_path = os.path.join(MODEL_DIR, "asr")
    if not os.path.exists(asr_model_path):
        os.makedirs(asr_model_path)
    
    # 保存原始环境变量
    original_modelscope_cache = os.environ.get('MODELSCOPE_CACHE', '')
    original_funasr_home = os.environ.get('FUNASR_HOME', '')
    
    # 设置环境变量
    os.environ['MODELSCOPE_CACHE'] = asr_model_path
    os.environ['FUNASR_HOME'] = MODEL_DIR
    
    # 加载ASR模型
    print("正在加载ASR模型...")
    model_state["asr_model"] = AutoModel(
        model="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        device=device,
        model_type="pytorch",
        dtype="float32"
    )
    print("ASR模型加载完成")
    
    # 加载标点符号模型
    print("正在加载标点符号模型...")
    model_state["punc_model"] = AutoModel(
        model="ct-punc",
        model_revision="v2.0.4",
        device=device,
        model_type="pytorch",
        dtype="float32"
    )
    
    # 恢复原始环境变量
    if original_modelscope_cache:
        os.environ['MODELSCOPE_CACHE'] = original_modelscope_cache
    else:
        os.environ.pop('MODELSCOPE_CACHE', None)
        
    if original_funasr_home:
        os.environ['FUNASR_HOME'] = original_funasr_home
    else:
        os.environ.pop('FUNASR_HOME', None)
    print("标点符号模型加载完成")
    
    vad_state["model"] = model_state["vad_model"]

@app.websocket("/v1/ws/vad")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    vad_state["active_websockets"].add(websocket)
    try:
        print("新的WebSocket连接")
        while True:
            try:
                data = await websocket.receive_bytes()
                audio = np.frombuffer(data, dtype=np.float32).copy()
                
                if len(audio) == WINDOW_SIZE:
                    audio_tensor = torch.FloatTensor(audio)
                    speech_prob = vad_state["model"](audio_tensor, SAMPLE_RATE).item()
                    result = {
                        "is_speech": speech_prob > VAD_THRESHOLD,
                        "probability": float(speech_prob)
                    }
                    await websocket.send_text(json.dumps(result))
            except WebSocketDisconnect:
                print("客户端断开连接")
                break
            except Exception as e:
                print(f"处理音频数据时出错: {str(e)}")
                break
    except Exception as e:
        print(f"WebSocket错误: {str(e)}")
    finally:
        if websocket in vad_state["active_websockets"]:
            vad_state["active_websockets"].remove(websocket)
        print("WebSocket连接关闭")
        try:
            await websocket.close()
        except:
            pass

@app.post("/v1/upload_audio")
async def upload_audio(file: UploadFile = File(...)):
    filename = "latest.wav"
    file_path = os.path.join(AUDIO_DIR, filename)
    
    try:
        # 保存音频文件
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)
            
        # 进行ASR处理
        with torch.no_grad():
            # 语音识别
            asr_result = model_state["asr_model"].generate(
                input=file_path,
                dtype="float32"
            )
            
            # 添加标点符号
            if asr_result and len(asr_result) > 0:
                text_input = asr_result[0]["text"]
                final_result = model_state["punc_model"].generate(
                    input=text_input,
                    dtype="float32"
                )
                
                return {
                    "status": "success",
                    "filename": filename,
                    "text": final_result[0]["text"] if final_result else text_input
                }
            else:
                return {
                    "status": "error",
                    "filename": filename,
                    "message": "语音识别失败"
                }
                
    except Exception as e:
        print(f"处理音频时出错: {str(e)}")
        return {
            "status": "error",
            "message": str(e)
        }

@app.get("/vad/status")
def get_status():
    closed_websockets = set()
    for ws in vad_state["active_websockets"]:
        try:
            if ws.client_state.state.name == "DISCONNECTED":
                closed_websockets.add(ws)
        except:
            closed_websockets.add(ws)
    
    for ws in closed_websockets:
        vad_state["active_websockets"].remove(ws)
    
    return {
        "is_running": bool(vad_state["active_websockets"]),
        "active_connections": len(vad_state["active_websockets"])
    }

# 添加静态文件路由
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=1000)
