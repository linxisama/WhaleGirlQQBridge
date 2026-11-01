"""Actual runtime prerequisites and limitations, independent of saved switches."""
from .plugins.manager import PLUGIN_MODULES

ONEBOT_ONLY={
    # 合并转发只有 OneBot 能取到节点（官方不投递这类事件）
    'proactive_chat':'主动群聊发言','silence_ping':'主动私信','affinity_proactive':'好感度主动发言',
    'icebreaker':'定时暖场','scheduled_announcer':'定时群消息','typing_indicator':'正在输入状态',
    'emoji_echo':'消息气泡反应跟贴','msg_reaction':'消息气泡贴表情','probability_reply':'头像戳事件和反戳',
    'speech_to_text_qq':'协议端语音转写',
}
TRIGGERS={
    'archive_isolation':'切换人设后生效，历史按角色隔离。',
    'conversation_memory':'/记住 内容、/记忆 关键词、/忘记 关键词；自动记忆需独立来信复证。',
    'cross_chat_link':'同一用户的已确认记忆；默认不把私聊带入群聊。',
    'long_image_reader':'随图片识别调用；需启用图片识别和可用视觉模型。',
    'image_understanding':'收到图片后识别；需要视觉模型，纯文字模型不能识图。',
    'sticker_memory':'缓存已经成功识别的表情含义，不会凭空生成含义。',
    'sticker_admin':'插件中心“管理表情库”管理收藏，不同步QQ个人收藏。',
    'sticker_steal':'自动收集实际收到的图片，默认限表情和小图；官方群聊需实际推送消息。',
    'sticker_cap':'/表情容量 查看用量；只管理索引登记的本地收藏，默认只统计。',
    'memes':'/表情列表、/表情详情 名称、/表情预览 名称、/表情制作 名称 文字（并附图片）、/随机表情。300种模板，本机子进程生成；首次下载固定版本素材。',
    'image_library':'/表情 查看标签，/表情 标签 发送真正图片。',
    'sticker_cadence':'每成功回复N次附图；已有图片不追加，失败不计次。',
    'hot_trend_tracker':'/热点 查看缓存；后台采集公开热点，外部来源失败不会编造数据。',
    'search_query_hygiene':'/搜索 关键词；只依据取得的网页摘要并附来源。',
    'share_card':'解析分享元数据；开启“读取公开网页正文”后读取公开HTML，不登录、不执行脚本。',
    'pixiv_illust':'/插图 关键词；使用配置的Lolicon兼容服务发送实际下载验证的全年龄图片。',
    'pc_control':'绑定管理员后，私聊“机器人 状态”等；只管理机器人控制台。',
    'keyword_reply':'关键词命中时直接回预设文字，无模型调用。',
    'context_chime':'非@群消息进入回复候选后做短判定；需先允许昵称触发或插话。判定本身消耗模型调用。',
    'early_finish':'已交付完毕、无遗留动作的这一轮，跳过"该不该接话"的判定调用和"让AI选表情"的调用；有工具失败或预告式结尾时不省。试运行只记日志。',
    'market_probe':'管理员私聊“市场 [关键词]”抓取配置的市场目录并按关键词筛出相关插件；只展示，不自动安装。',
    'minimal_mode':'管理员私聊“/极简 开|关|极限|敢死”切档；按档位裁定每轮带多少历史、输出封顶并加一段短话纪律。关掉立刻恢复原样。',
    'token_saver':'每轮请求只做减法：历史滑窗裁剪并把更早内容压成一行摘要，截断过长的单条历史。省不到字符时退化成只删；出错静默放行。',
    'cache_stability':'把每轮都会变的说明（心情值、本轮发言人摘要、跟聊提醒…）排到系统提示词最后，让稳定前缀逐字节相同以提高服务商前缀缓存命中；并记录真实命中率、提醒前缀变化。',
    'affinity':'自动累积群友好感度（发言/@/接话加分，每日上限防刷，长期不发言衰减，主人满级）；当前说话人的真实分数与等级作为易变说明注入提示词。口令：好感度 / 好感度 排行 [N] / 好感度 重置 [QQ号]（主人）/ 好感度 设置 键=值（主人）。',
    'member_aliases':'命中外号才加几行"称呼对照"到提示词末尾（谁在说话、谁被提到），帮模型认人。外号表口令：外号 / 外号 加 <QQ号> <外号> / 外号 删 <QQ号> [外号]（管理员）。',
    'group_cost_report':'群里 @机器人 说「金额统计」（可跟区间：昨天/本周/上月/最近7天/9月1日到9月30日/全部）→ 按"触发这次运行的人"列出本群 token 用量；规则触发、立即回复、不经过模型。填了单价才折算金额。',
    'reply_freshness':'把"群里最新一条是谁、你要回的那条还新不新鲜、是不是已经冷场"算清楚，追加在当前消息末尾（不动系统提示词）：晚回自然补一句衔接、冷场别硬接；可选的硬刹车默认关。',
    'proactive_chat':'目标群达到空闲条件后入队；只在发送成功后计次数。',
    'icebreaker':'到随机时间后对指定或最近活跃会话暖场；遵守安静时段和发送队列。',
    'repeat_echo':'不同群友连续发相同有效文字才触发，纯图片不复读。',
    'question_relay':'连续问号达到阈值才触发。',
    'burst_merge':'同一群友连发时合并待处理消息；只作用于群聊。',
    'at_mention':'普通QQ把可识别成员名转为真正@；官方QQ保留文字提及。',
    'quote_reply':'调整模型回复格式；它是提示词功能，不发送QQ原生引用气泡。',
    'sleep_mode':'在配置的北京时间窗口内屏蔽普通回复；按设置保留@回复。',
    'send_pacing':'拆句延迟发送；官方QQ使用递增回复序号，一条来信最多5次被动回复。',
    'face_decode':'翻译QQ内置表情数字；图片表情的识别由视觉插件负责。',
    'short_followup':'成功回复后开启同一人的跟聊窗口；官方群聊仍只能接收平台推送的@消息。',
}
LABELS={'ready':'已接入','limited':'部分可用','unavailable':'接入受限','needs_config':'待配置','needs_data':'待数据','external':'外部服务','unverified':'待验证'}

def apply_capabilities(items,runtime):
    onebot_mode = runtime.mode in {'onebot', 'qqbridge'}
    for item in items:
        pid=item['id'];settings=item.get('settings') or {}
        status='ready';note=TRIGGERS.get(pid,'运行钩子已接入，按配置条件触发；开关表示是否允许执行，不代表每条消息都会触发。')
        def set_status(value,reason):
            nonlocal status,note
            status=value;note=reason
        if pid not in PLUGIN_MODULES:
            set_status('unverified','第三方插件：已加载；效果和接入能力需单独验证。')
            instance=runtime.plugins._plugins.get(pid)
            if instance:
                hooks={'on_ingest','on_block','on_pre_ai','on_pre_ai_async','on_prepare','on_post_reply','on_sent','tick','observe','background','prepare_delivery','on_before_tool','on_after_tool'}
                defined={key for cls in type(instance).__mro__[:-2] for key in cls.__dict__}
                if not hooks.intersection(defined):set_status('unavailable','该第三方插件没有实现任何运行钩子，目前只有元数据/界面，不能提供实际聊天功能。')
        if pid in ONEBOT_ONLY:
            if not onebot_mode:
                set_status('unavailable',f'当前官方QQ接入未接通{ONEBOT_ONLY[pid]}；需要普通QQ/OneBot。配置保留，启用开关无法补足接入能力。')
            else:
                set_status('limited',note+' 协议端需支持对应接口；未对当前客户端做真人验收。')
        if pid=='poke_counter':
            item['gesture_available']=onebot_mode
            if not onebot_mode:set_status('limited','官方QQ不推送真实戳一戳；可用「/戳一戳」指令，或直接 @ 机器人但不说话（当作戳一下）。')
            else:set_status('ready','普通QQ支持真实戳一戳；客户端需上报通知。统计插件启用时优先于概率回应。')
        if pid=='forward_summary':
            set_status('limited','普通QQ和bridge按转发ID读取并限预算展开嵌套；官方QQ只能读取实际投递的聊天记录正文和图片，未投递时不会猜测。')
        if pid=='speech_to_text_qq' and runtime.mode=='qqbridge':
            set_status('limited','bridge 需支持 fetch_ptt_text；已有转写优先使用，总耗时受转写预算限制。')
        if pid=='quick_menu':
            set_status('limited','/菜单 与B站介绍可用；按钮富文本需平台开通权限并在配置中启用。/视频仅可用的公开单段MP4，失败保留原链接。')
        if pid=='at_mention' and not onebot_mode:
            set_status('limited','官方QQ保留文字@昵称，目前无法生成这里使用的真正成员@消息段。')
        if pid=='send_pacing' and not onebot_mode:
            set_status('limited','官方QQ支持拆句延迟发送，使用递增回复序号，一条来信最多5次被动回复；图库可用下一序号单独发送。')
        if pid=='short_followup' and not onebot_mode:
            set_status('limited',note)
        if pid=='memes':
            set_status('limited','300种模板使用本机子进程制作，常用模板已带素材，其余首次按需下载。图片与命令须同条发送；未适配NoneBot头像取图、交互追问与ORM管理。群聊先@。')
        if pid=='pixiv_illust':
            if settings.get('searchBackend')=='pixiv':set_status('unavailable','Pixiv直连/Cookie模式尚未验证，请选auto或lolicon；不把Cookie发给第三方。')
            elif not str(settings.get('loliconApiUrl','')).strip():set_status('needs_config','先填写Lolicon兼容服务地址，再用 /插图 关键词；当前仅支持全年龄图片。')
            else:set_status('external',note+' 外部服务未做实时可用性验证；直连、Cookie和成人分级的旧设置暂不执行。')
            inactive={'searchUrlTemplate','imageUrlTemplate','cookie','ratings','allowR18','ownerIds','adminIds','deadTtlDays'}
            item['inactive_fields']=sorted(inactive)
            item['schema']={key:value for key,value in item.get('schema',{}).items() if key not in inactive}
        if pid=='keyword_reply' and not str(settings.get('rules','')).strip():set_status('needs_config','尚未添加关键词规则；先在“配置”中添加关键词和回复。')
        if pid=='image_library' or pid=='sticker_cadence':
            lib=runtime.plugins._plugins.get('image_library')
            try:has_images=bool(lib and lib.entries(runtime))
            except Exception:has_images=False
            if not has_images:set_status('needs_data','图库暂无可发送图片。上传图库图片或启用自动收集；用 /表情 查看标签。')
            elif pid=='sticker_cadence' and (not lib or not lib.enabled):set_status('needs_config','配图需要启用“表情包与自定义图库”。')
        if pid in {'sticker_steal','sticker_admin','sticker_cap','sticker_cadence'} and getattr(runtime,'stickers',None) is None:
            set_status('unavailable','机器人本地表情库无法读取，请修复数据目录；不能只靠启用开关恢复。')
        if pid=='pc_control' and not str(settings.get('ownerIds','')).strip():set_status('needs_config','尚未绑定管理员，请在“机器人管理”页从最近私聊用户中绑定。')
        if pid=='sticker_cap':
            # 字段**保留在 schema 里**：直接删掉会让配置窗里既没有这一行、也没有
            # "选择文件夹…"按钮（AGENTS.md 要求目录用途必须提供文件夹选择）。
            # 运行时目前固定管理 data/sticker_library，所以标成"暂不生效"并说明原因。
            item['inactive_fields']=sorted(set(item.get('inactive_fields') or []) | {'stickerDir'})
        if status!='unavailable' and pid in {'proactive_chat','scheduled_announcer','silence_ping'}:
            field={'proactive_chat':'groups','scheduled_announcer':'groupIds','silence_ping':'targets'}[pid]
            if not str(settings.get(field,'')).strip():set_status('needs_config','尚未填写目标群或私聊用户；不会自动给任意用户发送消息。')
            elif pid=='proactive_chat' and not settings.get('useModel',True) and not str(settings.get('topics','')).strip():set_status('needs_config','关闭AI生成时需要先填写备用话题。')
            elif pid=='scheduled_announcer' and not str(settings.get('content','')).strip():set_status('needs_config','先填写要发送的固定文字或AI文案要求。')
        if pid=='icebreaker' and onebot_mode and not str(settings.get('chats','')).strip() and not runtime.recent_chats():
            set_status('needs_data','未指定目标且当前账号没有最近会话；先填写目标或与机器人聊一次。')
        if pid=='affinity_proactive' and onebot_mode and not str(settings.get('affinityScores','')).strip() and not str(settings.get('allowList','')).strip():
            set_status('needs_config','先填写好感度目标或允许会话名单；空名单不会主动发言。')
        if pid=='probability_reply' and onebot_mode:
            counter=runtime.plugins._plugins.get('poke_counter')
            if counter and counter.enabled:set_status('limited','戳统计插件优先处理真实戳通知；如只用概率回应，请关闭戳统计。反戳需协议端支持。')
        if pid=='long_image_reader':
            vision=runtime.plugins._plugins.get('image_understanding')
            if not vision or not vision.enabled:set_status('needs_config','先启用图片识别插件并配置视觉模型，长图分段才会执行。')
        if pid in {'image_understanding','context_chime','long_image_reader'} and not runtime.ai.configured:
            set_status('needs_config','模型/API尚未完整配置，请在设置中填写并测试。')
        if pid in {'hot_trend_tracker','search_query_hygiene'}:set_status('external',note+' 网络及网站结构变化可能影响结果；失败会明确说明。')
        # 官方 QQ 与普通 QQ 的**平台能力差异**要说清楚，否则"插件启用了却没反应"会被当成 bug。
        # 注意：只调"更轻"的状态，ONEBOT_ONLY 里的插件必须保持 unavailable（否则控制台会
        # 把它显示成可用，用户点了开启却什么都不会发生）。
        if runtime.mode == 'official' and status != 'unavailable':
            if pid == 'poke_counter':
                set_status('limited', '官方 QQ 不推送头像戳通知；可用「/戳一戳」指令，或 @ 机器人后什么都不说（当作戳一下，1.6.24 起支持）。')
            if pid == 'probability_reply':
                set_status('limited', '官方 QQ 没有真实戳一戳，/@ 空消息会当作戳来回应；反戳只有普通 QQ（OneBot）支持。')
            if pid in {'proactive_chat', 'scheduled_announcer', 'silence_ping', 'affinity_proactive',
                       'icebreaker'}:
                set_status('limited', '官方 QQ 的主动消息受配额限制（每天每会话有限），'
                                      '超额时平台会拒绝并在运行记录里写明原因。')
        item.update(capability_status=status,capability_label=LABELS[status],status_note=note,
                    available=status!='unavailable',availability_label=LABELS[status],
                    enabled_label=('指令模式' if pid=='poke_counter' and not onebot_mode else LABELS[status] if status in {'unavailable','needs_config','needs_data','unverified'} else '已启用'))
        item['runtime_state']='已停用' if not item.get('enabled') else '待启动' if not runtime.running else LABELS[status]
    return items
