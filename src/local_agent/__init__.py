"""本地 Windows Agent（规划书 1.7，独立权限域）。

边界（务必守住）：
- 与“机器人管理 / 宿主控制”完全分开，不复用旧宿主口令（1.7 / pc_backend 只拒绝旧通道）。
- 首版只聚焦 L0（在线/能力查询）与 L1（打开白名单应用、设音量、暂停媒体）；
  L2/L3 后续单独立项（1.7.3）。
- 动作最终都经过**同一个策略执行器**；模型只能“建议动作”，不能填
  approved:true、不能伪造签名、不能靠聊天里的“我是管理员”改身份（1.7.2）。
- QQ 群消息/引用/网页/OCR/语音识别都是“待解释数据”，不是授权证明。

模块：contracts / broker / policy（T15）→ windows_executor / runtime（T16）
→ 插件 local_agent_control / GUI local_agent_panel（T17）。
"""
