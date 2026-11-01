"""Keyword rule codec: legacy key=value and versioned multiline records."""
import json

FORMAT='keyword_reply/v1'
PREFIX='{"format":"'+FORMAT+'"'


def read_rule_rows(raw):
    raw=str(raw or '')
    if raw.lstrip().startswith(PREFIX):
        try:
            document=json.loads(raw)
            rows=document['rules']
            if not isinstance(rows,list) or any(not isinstance(row,dict) or
                    not isinstance(row.get('keyword'),str) or not isinstance(row.get('reply'),str) for row in rows):
                raise ValueError()
            return [(row['keyword'],row['reply']) for row in rows]
        except (ValueError,KeyError,TypeError) as exc:
            raise ValueError('规则数据损坏，请重新填写或恢复默认') from exc
    rows=[]
    for line in raw.splitlines():
        line=line.strip()
        if line:
            keyword,separator,reply=line.partition('=')
            rows.append((keyword.strip(),reply.strip() if separator else ''))
    return rows


def validate_rule_rows(rows):
    result=[]
    seen=set()
    for index,(keyword,reply) in enumerate(rows,1):
        keyword,reply=keyword.strip(),reply.strip()
        if not keyword and not reply:
            continue
        if not keyword or not reply:
            raise ValueError(f'第 {index} 条规则请同时填写关键词和回复内容；不用的规则可以删除。')
        if '\n' in keyword or '\r' in keyword:
            raise ValueError(f'第 {index} 条的关键词不能换行。')
        if keyword.lower() in seen:
            raise ValueError(f'第 {index} 条关键词“{keyword}”重复，请合并或修改。')
        seen.add(keyword.lower())
        result.append((keyword,reply))
    return result


def write_rule_rows(rows):
    rows=validate_rule_rows(rows)
    if all('=' not in key and '\n' not in reply and '\r' not in reply for key,reply in rows):
        return '\n'.join(key+'='+reply for key,reply in rows)
    return json.dumps({'format':FORMAT,'rules':[{'keyword':key,'reply':reply} for key,reply in rows]},
                      ensure_ascii=False,separators=(',',':'))
