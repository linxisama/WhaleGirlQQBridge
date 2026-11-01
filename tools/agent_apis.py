"""Private providers. Remote models receive explicit text packets, no filesystem tools."""
import asyncio
import json
import os
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]

def provider(role):
    path = ROOT / '.team-private/providers.json'
    configs = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    cfg = dict(configs.get(role, {}))
    for key in ('url', 'api_key', 'model'):
        cfg[key] = os.getenv(f'{role.upper()}_{key.upper()}', cfg.get(key, ''))
    if not all(cfg.get(key) for key in ('url', 'api_key', 'model')):
        raise ValueError(f'{role} 接口未配置完整')
    return cfg

async def call_agent(role, system, prompt, max_tokens=8192):
    cfg = provider(role)
    anthropic = cfg.get('api_style') == 'anthropic'
    headers = {'x-api-key':cfg['api_key'], 'anthropic-version':'2023-06-01'} if anthropic else {'Authorization':'Bearer '+cfg['api_key']}
    payload = {'model':cfg['model'], 'max_tokens':max_tokens, 'messages':[{'role':'user','content':prompt}]}
    if anthropic:
        payload['system'] = system
    else:
        payload['messages'].insert(0, {'role':'system','content':system})
    payload.update(cfg.get('extra_body', {}))
    async with httpx.AsyncClient(timeout=cfg.get('timeout',180)) as client:
        for attempt in range(3):
            try:
                response = await client.post(cfg['url'],headers=headers,json=payload)
            except httpx.RequestError:
                if attempt == 2:
                    raise RuntimeError(f'{role} 网络失败') from None
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code in (429,500,502,503,504) and attempt < 2:
                await asyncio.sleep(2**(attempt+1))
                continue
            if response.status_code >= 400:
                raise RuntimeError(f'{role} HTTP {response.status_code}，请核对接口、额度与模型')
            try:
                data = response.json()
                content = '\n'.join(b.get('text','') for b in data.get('content',[]) if b.get('type')=='text') if anthropic else data['choices'][0]['message'].get('content','')
                if not isinstance(content,str) or not content.strip():
                    finish = data.get('choices',[{}])[0].get('finish_reason') if not anthropic else data.get('stop_reason')
                    raise RuntimeError(f'{role} 未返回正文，结束原因={finish!r}；请检查输出预算或响应格式')
            except (KeyError,ValueError,TypeError,IndexError):
                raise RuntimeError(f'{role} 返回为空或格式不兼容') from None
            return {'text':content,'model':data.get('model',cfg['model']),'configured_model':cfg['model'],'usage':data.get('usage',{})}

def _sync(role,prompt,max_tokens):
    return asyncio.run(call_agent(role,'按任务工作，只报告可验证结果，不输出密钥。',prompt,max_tokens))['text']

def workbuddy_check(prompt,max_tokens=4096):
    return _sync('workbuddy',prompt,max_tokens)

def deepseek_implement(prompt,max_tokens=8192):
    return _sync('deepseek',prompt,max_tokens)

def deepseek_fast(prompt,max_tokens=4096):
    return _sync('deepseek',prompt,max_tokens)
