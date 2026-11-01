"""只根据已有状态说明QQ接入能力，不以一次空发送冒充验证。"""
def connection_status(status):
    status=status if isinstance(status,dict) else {}
    mode=status.get('mode','official');account=str(status.get('account') or '')
    if mode=='official':
        raw=status.get('official') or {};raw=raw if isinstance(raw,dict) else {}
        send=bool(raw.get('authenticated'));receive=bool(raw.get('ws_connected'))
        title=('官方 API 已鉴权 · 消息通道在线' if receive else '官方 API 已鉴权 · 等待消息通道') if send else '等待官方 API 鉴权'
        name='官方机器人 AppID '+account if account else '官方机器人 AppID 尚未同步'
    else:
        bridge=mode=='qqbridge';raw=status.get('qqbridge' if bridge else 'onebot') or {};raw=raw if isinstance(raw,dict) else {}
        account=str(raw.get('self_id') or account)
        connected=bool(raw.get('connected'));online=bool(raw.get('online') and account)
        receive=send=connected and online
        title=('QQ 直连在线' if bridge else 'QQ 在线') if receive else ('直连已连接，等待 QQ 登录' if connected else '等待 QQ 直连') if bridge else ('客户端已连接，等待 QQ 登录' if connected else '等待 QQ 客户端')
        nickname=str(raw.get('nickname') or '')
        name=(nickname+' · ' if nickname else '')+(account or '账号尚未同步')
    error=str(raw.get('error') or '')
    detail=name+'；'+('发送通道可用（未实际发送验证）' if send else '发送通道未就绪')
    if mode=='official' and not receive:detail+='；Webhook收件以入站记录为准'
    if error:detail+='；'+error
    return dict(title=title,detail=detail,account=account,receive_ready=receive,send_ready=send,ok=receive and send)
