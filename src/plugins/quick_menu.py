"""菜单与明确请求的视频分享均走既有被动回复队列。"""
from . import PluginBase,PluginMeta,FieldSchema
from ..bilibili_share import bili_source,read_video
from ..chat_state import quoted_context

COMMANDS=[('/菜单','查看机器人功能'),('/热点','获取当前热点'),('/搜索 关键词','联网查找资料'),
    ('/资讯 科技','查找最新资讯'),('/热梗 梗名','查找网络梗的来源'),('/表情列表','查看表情模板'),
    ('/随机表情','制作随机表情'),('/B站 BV号','查看B站视频介绍'),('/视频 BV号','发送可用的视频文件'),('/记忆','查看自己的记忆')]


class QuickMenu(PluginBase):
    META=PluginMeta(id='quick_menu',name='功能菜单与B站分享',category='utility',
        description='发送 /菜单 查看功能；B站链接和卡片读取公开简介；/视频 BV号 请求可用的低清MP4。官方按钮需平台权限；不开启时发送普通菜单。',
        config_schema={'enabled':FieldSchema('bool','启用'),
            'title':FieldSchema('string','菜单标题'),
            'cover':FieldSchema('string','菜单封面',file_picker='file',file_extensions=['.png','.jpg','.jpeg','.webp'],file_max_mb=2),
            'coverUrl':FieldSchema('string','富文本封面公开HTTPS地址',description='可选。富文本需要QQ可访问的图片URL；普通菜单使用上面的本地上传封面。'),
            'richMenu':FieldSchema('bool','已获官方Markdown与按钮权限',description='只有开放平台审核通过后开启；未开通时使用普通菜单。平台明确拒绝富文本时降为普通文字。'),
            'autoBilibili':FieldSchema('bool','自动介绍B站分享链接'),
            'sendVideo':FieldSchema('bool','允许用户用 /视频 请求视频',description='仅公开接口可返回的单段MP4，最长10分钟、最大25MB；失败发送原链接。')},
        default_settings={'enabled':True,'title':'鲸鱼娘，来聊点什么？','cover':'','coverUrl':'','richMenu':False,'autoBilibili':True,'sendVideo':True})
    def on_ingest(self,message,runtime):
        if message.get('text','').strip() not in ('/菜单','/帮助','/menu','/help'):return
        message['_quick_menu']=True
        return str(self.get('title','鲸鱼娘'))+'\n'+'\n'.join(command+' · '+desc for command,desc in COMMANDS)+'\n群聊请先 @机器人，再输入命令。'
    async def on_prepare(self,message,runtime):
        text=str(message.get('text') or '').strip()
        explicit=text.startswith(('/B站 ','/b站 ','/bili ','/视频 '))
        if text in ('/B站','/b站','/bili','/视频'):
            message['_direct_reply']='发送 /B站 BV号 查看介绍；/视频 BV号 请求视频文件，也可附上视频页或 b23.tv 链接。';return
        if not explicit and not self.get('autoBilibili',True):return
        source=bili_source(message)
        if not source:return
        want=text.startswith('/视频 ')
        if want and not self.get('sendVideo',True):
            message['_direct_reply']='视频文件分享已关闭，仍可用 /B站 查看介绍或打开原链接。';return
        try:
            data=await read_video(source,want);message['bilibili_data']=data
            if explicit:
                message['_direct_reply']=data['title']+'\nUP主：'+data['uploader']+'\n'+data['description'][:350]+'\n'+data['url']
                if data.get('video_error'):message['_direct_reply']+='\n'+data['video_error']
                if data.get('video'):message['_bili_video']=data['video']
        except Exception as exc:
            runtime.note('B站公开资料读取未完成：'+type(exc).__name__)
            if explicit:message['_direct_reply']='这条B站视频暂时没读到可用资料，可以打开原链接：'+source
            else:message['bilibili_data']={'url':source,'error':'本次未读到公开简介，不能编造视频画面'}
    def on_pre_ai(self,message,conversation,runtime):
        if message.get('bilibili_data'):
            conversation.insert(max(0,len(conversation)-1),quoted_context('B站公开标题与简介（没有观看视频画面）',message['bilibili_data']))
            return {'extra_system':'只能介绍取得的B站标题与简介，附原链接；没有取得视频画面或音轨，不可声称看完视频。'}
    async def prepare_delivery(self,message,reply,runtime):
        if message.get('_bili_video'):
            runtime.reply_meta=dict(runtime.reply_meta or {},video=message['_bili_video'])
        if message.get('_quick_menu'):
            meta=dict(runtime.reply_meta or {})
            if runtime.mode=='official' and self.get('richMenu',False):
                meta['menu']={'title':str(self.get('title','鲸鱼娘'))[:100], 'commands':COMMANDS[:6],'cover_url':str(self.get('coverUrl','') or '')}
            cover=str(self.get('cover','') or '')
            if cover and not meta.get('menu'):
                from ..image_input import image_source
                source=image_source(cover)
                if source:meta['media']=[{'type':'image','data':{'file':source}}]
            runtime.reply_meta=meta
        return reply
