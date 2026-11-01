"""音频引擎适配器包（规划书 1.3/1.4）。

每个适配器**只负责合成**（返回音频字节），不负责 QQ 发送（1.2 边界）。
所有适配器实现 audio_contracts.ProviderAdapter，并 register_provider 登记，
路由策略（audio_contracts.select_provider）按能力挑主引擎。

- local_existing.py  T05 audio.cpp / GPT-SoVITS（保留旧参数与声音行为）
- qwen_webui.py      T06 Qwen WebUI（custom-voice / voice-clone / voice-design）
- cloud_tts.py       T07 云端 TTS + 预算与出网策略

注意：不要把这些引擎的训练/推理依赖装进机器人环境（3.2）；模型留在独立进程。
"""
