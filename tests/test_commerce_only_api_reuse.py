from copy import deepcopy
import json
import sqlite3

import pytest

from selection_automation.stage3_progress import candidates
from selection_automation.vision import compatible_vision_keys, vision_revision, VisionRevisionContext
from test_vision_cache_identity import CAT, fixture


@pytest.mark.parametrize('change,can_reuse', [
    ('price', True), ('sales', True), ('title', False), ('quantity', False),
    ('size', False), ('image', False), ('questions', False), ('model', False)])
def test_paid_api_reuses_only_commerce_changes_from_real_product_history(tmp_path, change, can_reuse):
    cache = tmp_path/'stage12/image_cache'
    cache.mkdir(parents=True)
    original = fixture(cache)
    original.update(temu_title='Whisk set', temu_product_facts={
        'source_key':'frontend', 'title':'Whisk set',
        'fields':{'card_text':'Whisk set\n12 inches\nOriginal price $4.94\n31\nsold\n31sold'}})
    category = deepcopy(CAT)
    saved_key = vision_revision(category, original)
    with sqlite3.connect(tmp_path/'stage12/stage12.sqlite3') as connection:
        connection.execute('CREATE TABLE product_versions(source_key,product_id,record_json)')
        connection.execute('INSERT INTO product_versions VALUES(?,?,?)',
                           ('frontend','T',json.dumps(original['temu_product_facts'])))
    current = deepcopy(original)
    card = current['temu_product_facts']['fields']['card_text']
    current['temu_product_facts']['fields']['card_text'] = card.replace('$4.94','$5.50')
    if change=='sales':current['temu_product_facts']['fields']['card_text'] = card.replace('31','42')
    elif change=='title':current['temu_title']='Different whisk'
    elif change=='quantity':current['temu_product_facts']['fields']['card_text'] += '\n3 pack'
    elif change=='size':current['temu_product_facts']['fields']['size']='18 inches'
    elif change=='image':(cache/'temu.jpg').write_bytes(b'changed-image')
    elif change=='questions':category['stage3']['module_descriptions']['body'] += ' Changed actual question.'
    elif change=='model':category['stage3']['model']='different-model'
    candidate_file = tmp_path/'stage12/candidates.jsonl'
    candidate_file.write_text(json.dumps(current)+'\n',encoding='utf-8')
    compact = candidates(candidate_file)[0]
    for pair in (current, compact):
        keys = compatible_vision_keys(category,pair,context=VisionRevisionContext(category))
        assert (saved_key in keys) is can_reuse
    assert vision_revision(category,current) != saved_key
