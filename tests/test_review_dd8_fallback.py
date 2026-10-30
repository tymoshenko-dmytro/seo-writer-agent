"""Observed unchanged Apply fallback limits; compare HEAD with base, not new findings."""
from review_dd8_support import opened,ask,apply


def test_existing_cross_block_shift_is_not_found(studio,output_dir):
    with opened(studio,output_dir,'First.\n\nSecond.\n','Replacement.') as (page,art):
        ask(page)
        page.evaluate("e.commands.insertContentAt(1,'Prefix ')")
        apply(page)
        assert 'not in the article any more' in page.locator('#ask-result').text_content()


def test_existing_duplicate_fallback_chooses_another_occurrence(studio,output_dir):
    with opened(studio,output_dir,'Duplicate.\n\nDuplicate.\n\nLong safe tail remains here.\n','Replacement.') as (page,art):
        ask(page,{'from':13,'to':23})
        page.evaluate("e.commands.deleteRange({from:12,to:24})")
        apply(page)
        assert page.locator('.ProseMirror').text_content().startswith('Replacement.')


def test_removed_target_is_safely_refused(studio,output_dir):
    with opened(studio,output_dir,'A long introductory paragraph.\n\nTarget.\n','Replacement.') as (page,art):
        bounds=page.evaluate("() => {let p;e.state.doc.descendants((n,pos)=>{if(n.isText&&n.text==='Target.')p=pos});return {from:p,to:p+7}}")
        ask(page,bounds)
        page.evaluate("e.commands.setContent('<p>Tiny.</p>')")
        errors=[]
        page.on('pageerror',lambda err:errors.append(str(err)))
        apply(page)
        assert not errors
        assert 'not in the article any more' in page.locator('#ask-result').text_content()
        assert page.locator('.ProseMirror').text_content()=='Tiny.'
