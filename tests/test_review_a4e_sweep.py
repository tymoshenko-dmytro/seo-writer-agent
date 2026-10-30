"""Exploration only: public Markdown entry points, candidate cases for full UI reproduction."""
import json
from review_1d6_support import opened

def test_sweep_refs(studio,output_dir):
    with opened(studio,output_dir,'Old.\n') as (page,art):
        result=page.evaluate(r'''() => {
          const defs=[...['/a\\\\*b','/a\\<b','/a\\>b','/a\\\\(b','</a\\\\>','<>','/a&amp;b','/a\\&amp;b','</a b>','/a\\\\\\>b'].map(x=>'[ref]: '+x),
          ...['a\\\\*b','a\\\\\\\\b','a\\"b','a &quot;b&quot;','C:\\\\','a\\\\\\"b','a\\&quot;b','a\\\\&quot;b','a\nb','a\n  b'].map(x=>'[ref]: /new "'+x+'"')];
          function attrs(ns) {const out=[];const walk=n=>{if(n.type==='image')out.push({src:n.attrs.src,title:n.attrs.title});for(const m of n.marks||[])if(m.type==='link')out.push({href:m.attrs.href,title:m.attrs.title});for(const c of n.content||[])walk(c)};ns.forEach(walk);return out;}
          const out=[];
          for(const def of defs) for (const ref of ['[source][ref]','![Diagram][ref]']) {
            const md='Definition.\n\n'+def+'\n\nRead '+ref+'.';
            const expected=attrs(e.markdown.parse(md).content||[]);
            const actual=attrs(MdBlocks.replyNodes(e,md,1,5));
            out.push({def,ref,lexer:TT.marked.lexer(md).links,expected,actual,same:JSON.stringify(expected)===JSON.stringify(actual)});
          }return out;
        }''')
        print(json.dumps(result,ensure_ascii=False))
        assert all(x['same'] for x in result)

def test_sweep_fences(studio,output_dir):
    with opened(studio,output_dir,'Old.\n') as (page,art):
        result=page.evaluate(r'''() => {
          const out=[];
          const contexts=[x=>x,x=>'- Parent\n\n'+x.split('\n').map(l=>'  '+l).join('\n'),x=>'- '+x.split('\n').map((l,i)=>i?'  '+l:l).join('\n'),x=>'- Parent\n\n  > Intro.\n  >\n'+x.split('\n').map(l=>'  > '+l).join('\n')];
          for (let ci=0;ci<contexts.length;ci++)for(const mid of ['ordinary','~~~','```not-a-close','    ```','```\u00a0','```\t','  ```','``','````','\t```','```\u2003']){
            const md='Intro.\n\n'+contexts[ci]('```text\nalpha\n'+mid+'\n3. literal\nomega\n```')+'\n\nEnd.';
            const nodes=MdBlocks.replyNodes(e,md,1,5);
            e.commands.setContent({type:'doc',content:nodes});
            const raw=e.markdown.serialize(e.getJSON());
            const actual=MdBlocks.save(e,{lead:'',byId:{},order:[]});
            out.push({ci,mid,md,raw,actual});
          }return out;
        }''')
        print(json.dumps(result,ensure_ascii=False))
