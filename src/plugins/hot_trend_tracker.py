import json,time,re,html,asyncio
from datetime import datetime,timedelta,timezone
from urllib.parse import quote
from . import PluginBase,PluginMeta,FieldSchema
from ..public_web import read_public
from ..chat_state import quoted_context

def parse_topics(source,text):
    if source=='bilibili':
        raw=json.loads(text)
        if raw.get('code')!=0:raise ValueError('来源返回失败')
        items=raw.get('data',{}).get('list',[])[:20]
        return [dict(title=str(v.get('title',''))[:100],description=str(v.get('desc',''))[:300],url='https://www.bilibili.com/video/'+str(v.get('bvid',''))) for v in items if v.get('title') and re.fullmatch(r'BV\w+',str(v.get('bvid','')))]
    if source=='baidu':
        # Embedded public hydration data: never evaluate page scripts.
        match=re.search(r'<!--s-data:(.*?)-->',text,re.S)
        raw=json.loads(match.group(1)) if match else {}
        items=raw.get('data',{}).get('cards',[{}])[0].get('content',[])
        if items and isinstance(items[0],dict) and isinstance(items[0].get('content'),list):items=items[0]['content']
        return [dict(title=str(v.get('word',''))[:100],description=str(v.get('desc',''))[:300],url='https://www.baidu.com/s?wd='+quote(str(v.get('word','')))) for v in items[:20] if v.get('word')]
    raw=json.loads(text);items=raw.get('data',{}).get('bang_topic',{}).get('topic_list',[])
    return [dict(title=str(v.get('topic_name',''))[:100],description=str(v.get('topic_desc',''))[:300],url='https://tieba.baidu.com/f?kw='+quote(str(v.get('topic_name','')))) for v in items[:20] if v.get('topic_name')]

class HotTrendTracker(PluginBase):
    META=PluginMeta(id='hot_trend_tracker',name='热梗与热点收集',category='utility',
        description='每日北京时间6点后采集公开热点，保存来源及时间；/热点 查看缓存，谈到热搜或热梗时提供参考。失败保留有效旧记录并显示更新时间。',
        config_schema={'enabled':FieldSchema(type='bool',label='启用'),
            'sourceBilibili':FieldSchema(type='bool',label='哔哩哔哩热门'),
            'sourceTieba':FieldSchema(type='bool',label='贴吧热点'),
            'sourceBaidu':FieldSchema(type='bool',label='百度热搜'),
            'keepDays':FieldSchema(type='int',label='保留天数',min=1,max=30)},
        default_settings={'enabled':True,'sourceBilibili':True,'sourceTieba':True,'sourceBaidu':True,'keepDays':7})
    async def refresh(self):
        now=time.time();old=self.load_state('topics.json',{});topics={v['key']:v for v in old.get('topics',[]) if isinstance(v,dict) and now-v.get('time',0)<int(self.get('keepDays',7))*86400};errors=[];successful=[]
        sources=[('sourceBilibili','bilibili','https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1'),('sourceTieba','tieba','https://tieba.baidu.com/hottopic/browse/topicList'),('sourceBaidu','baidu','https://top.baidu.com/board?tab=realtime')]
        async def fetch(source,url):
            try:
                parsed=parse_topics(source,await asyncio.wait_for(read_public(url,timeout=4),4))
                if not parsed:raise ValueError('未取得热点')
                successful.append(source)
                for v in parsed:
                    v.update(source=source,time=now,key=source+':'+v['title']);topics[v['key']]=v
            except Exception:errors.append(source)
        await asyncio.gather(*(fetch(source,url) for key,source,url in sources if self.get(key)))
        state={'topics':sorted(topics.values(),key=lambda v:v['time'],reverse=True)[:300],'attempt':now,'updated':now if successful else old.get('updated',0),'errors':errors}
        self.save_state('topics.json',state);return state
    async def tick(self,runtime):
        now=datetime.now(timezone(timedelta(hours=8)));s=self.load_state('topics.json',{})
        if now.hour>=6 and time.time()-s.get('attempt',0)>3600 and (s.get('errors') or datetime.fromtimestamp(s.get('updated',0),now.tzinfo).date()!=now.date()):await self.refresh()
    def on_ingest(self,message,runtime):
        if message.get('text','').strip()!='/热点':return
        s=self.load_state('topics.json',{});items=s.get('topics',[])[:10]
        if time.time()-s.get('attempt',0)>300:return
        return ('热点缓存（'+datetime.fromtimestamp(s.get('updated',0),timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')+'）：\n'+'\n'.join(v['title']+'\n'+v['url'] for v in items)) if items else '热点尚未采集成功，请在控制台查看来源状态，稍后再试。'
    async def on_prepare(self,message,runtime):
        if message.get('text','').strip() not in ('/热点','/热点 更新'):return
        state=self.load_state('topics.json',{})
        if time.time()-state.get('attempt',0)>60:
            if not hasattr(self,'_refresh_lock'):self._refresh_lock=asyncio.Lock()
            async with self._refresh_lock:
                state=self.load_state('topics.json',{})
                if time.time()-state.get('attempt',0)>60:state=await self.refresh()
        items=state.get('topics',[])[:8]
        stamp=datetime.fromtimestamp(state.get('updated',0),timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
        message['_direct_reply']=('热点（更新于 '+stamp+'，来源观点需自行核实）：\n'+'\n'.join(v['title']+' ['+v['source']+']\n'+v['url'] for v in items)) if items else '热点来源暂未返回可读内容，请稍后再试。'
        if items and state.get('errors'):message['_direct_reply']+='\n部分来源暂不可用，保留带时间的已有记录。'
    def on_pre_ai(self,message,conversation,runtime):
        if any(w in message.get('text','') for w in ['热搜','热点','热梗','什么梗']):
            s=self.load_state('topics.json',{});items=[{k:v[k] for k in ('title','description','source','time','url')} for v in s.get('topics',[])[:5]]
            if items:conversation.insert(max(0,len(conversation)-1),quoted_context('有时间和来源的热点参考（不能当作已核实事实）',items))
