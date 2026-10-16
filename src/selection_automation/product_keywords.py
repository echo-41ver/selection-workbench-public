"""Title-backed, category-independent attributes for product search queries."""
import re
import unicodedata


def normalize(value):
    value=unicodedata.normalize('NFKC',str(value)).lower()
    value=re.sub(r'[^a-z0-9.]+',' ',value)
    words=value.split()
    return ' '.join(w[:-1] if len(w)>3 and w.endswith('s') and not w.endswith(('ss','us','is')) else w for w in words)


def contains(phrase, text):
    return bool(re.search(r'(?<!\w)'+re.escape(normalize(phrase))+r'(?!\w)',normalize(text)))


def candidates(row, base_queries):
    title=str(row.get('title') or row.get('amazon_title') or row.get('Amazon标题') or '')
    text=unicodedata.normalize('NFKC',title)
    # Normalize only valid thousands groups. Malformed groups must not yield
    # suffix capacities such as 000mAh from 10,000mAh.
    text=re.sub(r'(?<![\w.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?![\d,])',
                lambda match:match[0].replace(',',''),text)
    text=re.sub(r'[‐‑‒–—−]','-',text)
    normalized=normalize(text)
    colors=('light blue','dark blue','navy blue','rose gold','blueberry','black','white','green','blue',
            'red','pink','grey','gray','yellow','purple','orange','brown','beige','clear','transparent')
    materials=('stainless steel','carbon steel','cast iron','aluminum','aluminium','ceramic','porcelain',
               'silicone','plastic','bamboo','cotton','polyester','wood','glass','leather')
    structures=('slow feeder','slow feeding','collapsible','adjustable','elevated','raised','tilted',
                'non slip','nonslip','no spill','spill proof','waterproof','cordless','rechargeable',
                'portable','foldable','magnetic','stackable','puzzle','treat dispensing','interactive',
                'maze','rubber base','silicone bottom','non skid')
    features=[]
    def add(value, kind, raw=None, **evidence):
        value=re.sub(r'\s+',' ',value.lower()).strip()
        if value and not any(f['value']==value for f in features):
            features.append({'value':value,'kind':kind,'raw':raw or value,**evidence})
    for kind, phrases in [('structure',structures),('material',materials),('color',colors)]:
        for phrase in phrases:
            if contains(phrase,normalized): add(phrase,kind)
    units=r'(?:fl\s*oz|us\s*cup|cups?|cm|mm|inches|inch|in|ml|liters?|litres?|l|oz|gallons?|gal|watts?|w|volts?|v|mah|ah|gb|tb|hz|khz|lbs?|kg)'
    for match in re.finditer(r'(?<![\w.,])(\d+(?:\.\d+)?(?:\s*(?:[x×]|-|to)\s*\d+(?:\.\d+)?)*)\s*[- ]?\s*('+units+r')(?!\w)',text,re.I):
        unit=match[2].lower()
        context=text[max(0,match.start()-45):min(len(text),match.end()+30)].lower()
        # Pet/person body weight is not the capacity or size of a bowl/accessory.
        if unit in ('lb','lbs','kg') and re.search(r'\b(dogs?|cats?|pets?|children|adults?|body|weighing)\b',context):
            continue
        unit={'cups':'cup','us cup':'cup','inches':'inch','liters':'liter','litres':'litre','volts':'v','watts':'w'}.get(unit,unit)
        add(match[1]+' '+unit,'parameter',match[0])
    from .rules import uncertain_alias_context
    def supported_quantity(match):
        prefix=text[max(0,match.start()-48):match.start()]
        promoted=bool(re.search(r'\b(?:free|bonus|buy|get|save|up to|compatible with|for use with)\s+(?:[a-z-]+\s+){0,3}$',prefix,re.I))
        # Inspect only an immediately attached exclusion, preserving affirmative
        # quantities elsewhere in the title (including separate comma clauses).
        suffix=text[match.end():match.end()+48]
        excluded=bool(re.match(r'\s*(?:\(\s*)?(?:(?:is|are)\s+)?(?:not\s+(?:included|supplied|provided)|excluded|not\s+included)\b',suffix,re.I))
        return not promoted and not excluded and not uncertain_alias_context(text,match.start(),match.end())
    packaging_varies=bool(re.search(r'packaging\s+may\s+vary',text,re.I))
    for match in re.finditer(r'\b(\d{1,3})\s*[- ]?\s*(?:pack|pieces?|pcs)\b|\bset\s+of\s+(\d{1,3})\b',text,re.I):
        if supported_quantity(match):
            add((match[1] or match[2])+' pack','count',match[0],scope='PACKAGE_QUANTITY',role='PACKAGING',packaging_may_vary=packaging_varies)
    # Literal quantity/container evidence is category independent. Keep its
    # packaging role separate from an intrinsic sold-body feature.
    containers=r'boxes|box|packs?|bags?|bottles?|jars?|tubes?|bundles?|cases?|containers?|canisters?|rolls?|pouches|pouch|trays?|cartons?|cubes?'
    geometries={'cube','cubical','flat','rectangular','rectangle','round','cylindrical'}
    package_modifiers=geometries|{'travel','family','mega','bulk','mini','small','large','assorted','mixed'}
    for match in re.finditer(r'(?<![\w.,])(\d{1,4})\s+(?:([a-z-]+)(?:\s+([a-z-]+))?\s+)?('+containers+r')(?!\w)',text,re.I):
        if not supported_quantity(match):continue
        if any(w and w.lower() not in package_modifiers for w in match.groups()[1:3]):continue
        modifiers=[w.lower() for w in match.groups()[1:3] if w and w.lower() in geometries]
        unit={'boxes':'box','pouches':'pouch'}.get(match[4].lower(),normalize(match[4]))
        add(' '.join([match[1],*modifiers,unit]),'packaging_quantity',match[0],scope='PACKAGE_QUANTITY',role='PACKAGING',packaging_may_vary=packaging_varies)
    for match in re.finditer(r'(?<![\w.,])(\d{1,6})\s*[- ]?\s*(?:count|ct)(?!\w)',text,re.I):
        if not supported_quantity(match):continue
        add(match[1]+' count','quantity',match[0],scope='EXPLICIT_COUNT')
    product_nouns={normalize(q).split()[-1] for q in base_queries if normalize(q)}
    for match in re.finditer(r'(?<![\w.,])(\d{1,4})\s*[x×]\s*(\d{1,6})\s+([a-z]+)\b',text,re.I):
        unit=match[3].lower()
        matches_noun=normalize(unit) in product_nouns or any(unit in {n+'s',n+'es',n[:-1]+'ies' if n.endswith('y') else n+'s'} for n in product_nouns)
        if not matches_noun or not supported_quantity(match):continue
        add(match[1]+' x '+match[2]+' count','quantity',match[0],scope='OBSERVED_MULTIPACK_EXPRESSION')
    brand=str(row.get('brand') or '').strip().lower()
    for token in re.findall(r'\b[A-Za-z][A-Za-z0-9-]{2,18}\b',text):
        # Preserve concrete alphanumeric model codes; not a guessed brand-only query.
        if (any(c.isdigit() for c in token) and sum(c.isalpha() for c in token)>=2
                and token.lower()!=brand and not re.fullmatch(r'\d*pack',token,re.I)
                and not (text.startswith(token) and token.isupper())):
            add(token,'model')
    matched=[normalize(q) for q in base_queries if contains(q,normalized)]
    if not matched:
        words=set(normalized.split())
        matched=[normalize(q) for q in base_queries if set(normalize(q).split())<=words]
    # Prefer an actually named product type to an absent umbrella category.
    base=min(matched,key=lambda q:(len(q.split()),len(q))) if matched else None
    if not base:
        leading=re.split(r'[,|:(]|\s-\s',text)[0]
        words=leading.split()
        while words and ((brand and words[0].lower() in brand.split()) or words[0].isupper()): words.pop(0)
        useful=[w for w in words if w.lower() not in {'for','with','and','the','a','an','new','best','premium'}]
        base=normalize(' '.join(useful[:5])) if len(useful)>=2 else normalize(base_queries[0])
    result=[];seen=set()
    def query(parts, reason):
        additions=[f for f in parts if not contains(f['value'],base)]
        value=' '.join([*(f['value'] for f in additions),base]).strip()
        if value not in seen and len(value)<=160 and (additions or not matched):
            seen.add(value);result.append({'query':value,'reason':reason,
                'features':parts,'title':title})
    structural=[f for f in features if f['kind'] in ('structure','material')]
    variants=[f for f in features if f['kind'] in ('parameter','color','count','model','quantity','packaging_quantity')]
    def variant_parts(items):
        result=[]
        quantity_kinds={'count','quantity','packaging_quantity'}
        for item in items:
            if item['kind'] in quantity_kinds and any(f['kind'] in quantity_kinds for f in result):continue
            result.append(item)
            if len(result)==2:break
        return result
    if structural: query(structural[:2],'标题中的结构或材质')
    if variants: query(variant_parts(variants),'标题中的颜色、尺寸、容量或型号')
    for feature in features: query([feature],'标题中的'+feature['kind'])
    if not matched: query([],'原标题中的商品类型，未强行套用类目词')
    return result
