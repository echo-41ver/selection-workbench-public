"""Human-readable visual evidence; never changes scoring or cached model facts."""
RELATIONS={'SAME':'一致','DIFFERENT':'明确不同','SIMILAR':'相似但未证实','UNKNOWN':'未看清/未取得','VARIANT':'允许变体'}


def visual_evidence_text(category,facts):
    features=category.get('stage3',{}).get('identity_policy',{}).get('features',[])
    labels={f['id']:f.get('label',f['id']) for f in features}
    identity=facts.get('identity') or {}
    rows=['整体判断依据：'+identity.get('reason','')] if identity else []
    for key,row in facts.get('feature_comparisons',{}).items():
        rows.append(f"{labels.get(key,key)}（{RELATIONS.get(row['relation'],row['relation'])}）：Amazon {row['amazon']}；TEMU {row['temu']}")
    design=identity.get('design_comparison')
    if design:rows.append(f"整体款式（{RELATIONS.get(design['relation'],design['relation'])}）：Amazon {design['amazon']}；TEMU {design['temu']}")
    for key,label,field in (('matches','共同细节','feature'),('differences','差异','field')):
        for row in identity.get(key,[]):
            name=labels.get(row[field],row[field]);rows.append(f"{label}（{name}）：Amazon {row['amazon']}；TEMU {row['temu']}")
    if identity.get('uncertainties'):rows.append('未能确定：'+'；'.join(identity['uncertainties']))
    return '｜'.join(rows)


def review_explanation(category,facts,scored):
    reason=scored['reason']
    if scored['state'] not in {'待复核','待补充证据'}:return reason
    features=category.get('stage3',{}).get('identity_policy',{}).get('features',[])
    labels={f['id']:f.get('label',f['id']) for f in features}
    parts=[labels.get(row['field'],row['field']) for row in (facts.get('identity') or {}).get('differences',[]) if row['kind']=='UNRESOLVED']
    for feature in features:
        row=facts.get('feature_comparisons',{}).get(feature['id'],{})
        if feature.get('required_visual') and row.get('relation') in {None,'UNKNOWN','SIMILAR'}:parts.append(feature.get('label',feature['id']))
    if parts:reason+='；需要核对：'+'、'.join(dict.fromkeys(parts))
    return reason
